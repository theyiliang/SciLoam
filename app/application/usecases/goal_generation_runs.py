"""Short-lived, actor-bound goal runs with real stages and guarded draft commits."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
import uuid

from app.domain.ai_ports import AnswerCommitGuard
from app.domain.errors import DomainError
from app.domain.goal_drafts import canonical_hash, version_ids
from app.domain.goal_generation_runs import FAILURES, TERMINAL_EVENTS
from app.domain.run_events import RunBackpressure


@dataclass
class _Run:
    id: str
    actor: str
    request_id: str
    fingerprint: str
    ids: list
    snapshot: dict
    authorize_actor: object = None
    status: str = 'queued'
    task: object = None
    lease: object = None
    deadline: object = None
    expiry: object = None
    completed: float | None = None
    subscribers: int = 0
    started: float = field(default_factory=time.monotonic)
    guard: AnswerCommitGuard = field(default_factory=AnswerCommitGuard)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reason: str | None = None
    result: dict | None = None
    stage: str = ''


class GoalGenerationRuns:
    def __init__(self, service, bus, *, max_concurrent=2, actor_limit=1, ttl_seconds=120,
                 first_attach_seconds=10, disconnect_grace_seconds=5, execution_timeout_seconds=120,
                 max_retained_runs=128):
        if min(max_concurrent, actor_limit, ttl_seconds, first_attach_seconds, disconnect_grace_seconds,
               execution_timeout_seconds, max_retained_runs) <= 0:
            raise ValueError('Invalid goal run limits')
        self.service, self.bus = service, bus
        self.max_concurrent, self.actor_limit = max_concurrent, actor_limit
        self.ttl_seconds, self.max_retained_runs = ttl_seconds, max_retained_runs
        self.first_attach_seconds, self.disconnect_grace_seconds = first_attach_seconds, disconnect_grace_seconds
        self.execution_timeout_seconds = execution_timeout_seconds
        self._runs, self._requests = {}, {}
        self._lock = asyncio.Lock()

    def _public(self, run):
        value = {'run_id': run.id, 'request_id': run.request_id, 'status': run.status, 'version_ids': list(run.ids)}
        if run.reason:
            value.update(reason=run.reason, message=FAILURES[run.reason])
        return value

    def _find(self, actor, run_id):
        run = self._runs.get(run_id)
        if run is None or run.actor != actor:
            raise DomainError(404, 'Goal run not found')
        return run

    async def _authorize(self, run, check=None):
        if check is not None:
            await check()
        if run.authorize_actor is not None:
            await run.authorize_actor()
        await asyncio.to_thread(self.service.check_goal_generation, run.actor, run.snapshot,
            authorization_check=getattr(run.authorize_actor, 'repository_check', None))

    async def start(self, actor, ids, request_id=None, *, regenerates_id=None, expected_revision=None, authorize_actor=None):
        ids = version_ids(ids)
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
            raise DomainError(422, 'Invalid request id')
        if regenerates_id is not None and (not isinstance(regenerates_id, str) or not 1 <= len(regenerates_id) <= 128):
            raise DomainError(422, 'Invalid regeneration draft id')
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1 or regenerates_id is None):
            raise DomainError(422, 'Invalid expected revision')
        if authorize_actor is not None:
            await authorize_actor()
        fingerprint = canonical_hash([ids, regenerates_id, expected_revision])
        async with self._lock:
            previous = self._requests.get((actor, request_id)) if request_id else None
            if previous in self._runs:
                run = self._runs[previous]
                await self._authorize(run, authorize_actor)
                if run.fingerprint != fingerprint:
                    raise DomainError(409, 'Request id already used for another goal extraction')
                return self._public(run)
            active = [r for r in self._runs.values() if r.completed is None]
            if len(active) >= self.max_concurrent or sum(r.actor == actor for r in active) >= self.actor_limit:
                raise DomainError(429, 'Goal extraction run limit reached')
            snapshot = await asyncio.to_thread(self.service.prepare_goal_generation, actor, ids,
                regenerates_id=regenerates_id, expected_revision=expected_revision)
            if len(self._runs) >= self.max_retained_runs:
                completed = sorted((r for r in self._runs.values() if r.completed is not None), key=lambda r: r.completed)
                if not completed:
                    raise DomainError(429, 'Goal extraction capacity reached')
                self._discard(completed[0])
            run = _Run(uuid.uuid4().hex, actor, request_id or uuid.uuid4().hex, fingerprint, ids, snapshot, authorize_actor)
            self._runs[run.id] = run
            self._requests[(actor, run.request_id)] = run.id
            self.bus.create(run.id, actor=actor, space='', conversation='', request_id=run.request_id)
            run.task = asyncio.create_task(self._execute(run), name='goal-generation')
            run.lease = asyncio.create_task(self._lease(run, self.first_attach_seconds))
            run.deadline = asyncio.create_task(self._deadline(run))
            return self._public(run)

    async def _publish(self, run, kind, data=None):
        await self.bus.publish(run.id, kind, {**(data or {}), 'version_ids': list(run.ids),
                                            'elapsed_ms': round((time.monotonic() - run.started) * 1000)})

    async def _finish(self, run, result):
        await self._authorize(run)
        # Re-read the persisted view rather than replay a cached quote snapshot.
        draft = await asyncio.to_thread(self.service.get_goal_draft, run.actor, result['draft']['id'])
        run.result = {'draft': {'id': draft['id']}}
        await self._publish(run, 'draft.save.completed', {'draft_id': draft['id']})
        run.status = 'completed'
        await self._publish(run, 'goal.completed', {'draft_id': draft['id']})

    def _failure(self, run, exc):
        if isinstance(exc, RunBackpressure):
            return 'backpressure'
        if isinstance(exc, DomainError):
            if exc.status_code in (401, 403, 404, 409):
                return 'source_changed'
            if 'No headings or numbered-list' in exc.message:
                return 'no_extractable_goals'
            if run.stage.startswith('evidence.') or exc.status_code == 422:
                return 'invalid_evidence'
            if exc.status_code in (502, 503):
                return 'model_unavailable'
        return 'run_unavailable'

    async def _terminal(self, run, reason):
        run.reason = reason
        run.status = 'cancelled' if reason in {'cancelled', 'consumer_timeout', 'shutdown'} else 'failed'
        run.result = None
        self.bus.purge_content(run.id)
        await self._publish(run, 'goal.cancelled' if run.status == 'cancelled' else 'goal.failed',
                            {'reason': reason, 'message': FAILURES[reason]})

    async def _execute(self, run):
        loop = asyncio.get_running_loop()

        async def check_async():
            run.guard.check()
            await self._authorize(run)
            run.guard.check()

        def check():
            run.guard.check()
            asyncio.run_coroutine_threadsafe(check_async(), loop).result()
            run.guard.check()

        async def emit_async(event):
            await check_async()
            run.stage = event['type']
            await self._publish(run, event['type'], {k: v for k, v in event.items() if k != 'type'})

        def emit(event):
            asyncio.run_coroutine_threadsafe(emit_async(event), loop).result()

        try:
            await check_async()
            run.status = 'running'
            await self._publish(run, 'goal.started')
            result = await asyncio.to_thread(self.service.execute_goal_generation, run.actor, run.snapshot,
                emit=emit, check=check, commit_guard=run.guard,
                authorization_check=getattr(run.authorize_actor, 'repository_check', None))
            await self._finish(run, result)
        except asyncio.CancelledError:
            if run.guard.committed:
                await self._recover(run)
            else:
                await self._terminal(run, run.reason or 'cancelled')
        except Exception as exc:
            if run.guard.committed:
                await self._recover(run)
            else:
                await asyncio.to_thread(run.guard.cancel)
                await self._terminal(run, self._failure(run, exc))
        finally:
            run.completed = time.monotonic()
            # Text snapshots are only needed during execution, not retention.
            run.snapshot['versions'] = []
            previous = run.snapshot['previous']
            if previous is not None:
                run.snapshot['previous'] = {'id': previous['id'], 'revision': previous['revision']}
            run.guard.result = None
            for timer in (run.lease, run.deadline):
                if timer is not None and timer is not asyncio.current_task():
                    timer.cancel()
            run.expiry = asyncio.create_task(self._expire(run))

    async def _recover(self, run):
        try:
            await self._finish(run, run.guard.result)
        except Exception:
            # COMMIT won the race: never claim cancellation erased the draft.
            run.status, run.result = 'completed', None
            run.guard.result = None
            self.bus.purge_content(run.id)
            await self._publish(run, 'goal.completed', {'persisted': True, 'reason': 'source_changed'})

    async def _stop(self, run, reason):
        async with run.stop_lock:
            if run.completed is not None:
                return
            cancelled = await asyncio.to_thread(run.guard.cancel)
            if cancelled:
                run.reason = reason
                run.task.cancel()
            try:
                await asyncio.shield(run.task)
            except asyncio.CancelledError:
                if not run.task.cancelled():
                    raise
                await self._terminal(run, reason)
                run.completed = time.monotonic()
                for timer in (run.lease, run.deadline):
                    if timer is not None and timer is not asyncio.current_task():
                        timer.cancel()
                run.expiry = asyncio.create_task(self._expire(run))

    async def _lease(self, run, delay):
        await asyncio.sleep(delay)
        if not run.subscribers and run.completed is None:
            await self._stop(run, 'consumer_timeout')

    async def _deadline(self, run):
        await asyncio.sleep(self.execution_timeout_seconds)
        await self._stop(run, 'execution_deadline')

    def _attach(self, run):
        run.subscribers += 1
        if run.lease is not None:
            run.lease.cancel()
            run.lease = None

    def _detach(self, run):
        run.subscribers -= 1
        if not run.subscribers and run.completed is None:
            run.lease = asyncio.create_task(self._lease(run, self.disconnect_grace_seconds))

    async def get(self, actor, run_id, *, authorize_actor=None):
        run = self._find(actor, run_id)
        await self._authorize(run, authorize_actor)
        value = self._public(run)
        if run.status == 'completed' and run.result is not None:
            value['result'] = {'draft': await asyncio.to_thread(self.service.get_goal_draft, actor, run.result['draft']['id'])}
            await self._authorize(run, authorize_actor)
        return value

    async def cancel(self, actor, run_id, *, authorize_actor=None):
        run = self._find(actor, run_id)
        await self._authorize(run, authorize_actor)
        await self._stop(run, 'cancelled')
        return self._public(run)

    async def subscribe(self, actor, run_id, after_seq=0, *, authorize_actor=None, include_heartbeats=False):
        run = self._find(actor, run_id)
        await self._authorize(run, authorize_actor)
        self._attach(run)
        reader = self.bus.listen(run_id, after_seq)
        try:
            async for event in reader:
                await self._authorize(run, authorize_actor)
                if event is not None and run.status in {'failed', 'cancelled'} and event.type not in TERMINAL_EVENTS:
                    continue
                if event is not None or include_heartbeats:
                    yield event
        finally:
            await reader.aclose()
            self._detach(run)

    async def _expire(self, run):
        await asyncio.sleep(self.ttl_seconds)
        self._discard(run)

    def _discard(self, run):
        self._runs.pop(run.id, None)
        self._requests.pop((run.actor, run.request_id), None)
        run.result = run.guard.result = None
        self.bus.discard(run.id)
        for timer in (run.lease, run.deadline, run.expiry):
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()

    async def aclose(self):
        await asyncio.gather(*(self._stop(r, 'shutdown') for r in list(self._runs.values()) if r.completed is None),
                             return_exceptions=True)
        for run in list(self._runs.values()):
            self._discard(run)
