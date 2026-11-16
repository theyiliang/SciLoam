"""Authorized, bounded research runs shared by SSE and legacy HTTP callers."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from app.domain.ai_ports import AnswerCommitGuard
from app.domain.answer_metadata import normalize_answer_metadata
from app.domain.errors import DomainError
from app.domain.run_events import RunBackpressure, RunBus, RunEvent


async def _call(target: Any, name: str, *args: Any, **kwargs: Any) -> Any:
    method = getattr(target, name)
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    value = await asyncio.to_thread(method, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


@dataclass
class _Run:
    id: str
    actor: str
    space: str
    conversation: str
    epoch: Any
    request_id: str
    fingerprint: str
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: str = "queued"
    task: asyncio.Task | None = None
    expiry: asyncio.Task | None = None
    lease: asyncio.Task | None = None
    deadline: asyncio.Task | None = None
    completed: float | None = None
    subscribers: int = 0
    versions: set[str] = field(default_factory=set)
    version_hashes: dict[str, str] = field(default_factory=dict)
    authorize_actor: Callable[[], Awaitable[None]] | None = None
    guard: AnswerCommitGuard = field(default_factory=AnswerCommitGuard)
    result: dict | None = None
    stop_reason: str | None = None
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started_at: float | None = None
    total_tokens: int = 0
    duration_ms: float | None = None


class RunManager:
    def __init__(self, service: Any, assistant: Any, bus: RunBus, *,
                 max_concurrent: int = 2, actor_limit: int = 1,
                 ttl_seconds: float = 120, max_retained_runs: int = 128,
                 execution_limiter: Any = None, first_attach_seconds: float = 10,
                 disconnect_grace_seconds: float = 5, execution_timeout_seconds: float = 120,
                 security_recorder=None):
        if (not 1 <= max_concurrent <= 2 or actor_limit < 1 or max_retained_runs < 1
                or min(ttl_seconds, first_attach_seconds, disconnect_grace_seconds, execution_timeout_seconds) <= 0):
            raise ValueError("Invalid run limits")
        self.service, self.assistant, self.bus = service, assistant, bus
        self.max_concurrent, self.actor_limit = max_concurrent, actor_limit
        self.ttl_seconds, self.max_retained_runs = ttl_seconds, max_retained_runs
        self.execution_limiter = execution_limiter
        self.first_attach_seconds = first_attach_seconds
        self.disconnect_grace_seconds = disconnect_grace_seconds
        self.execution_timeout_seconds = execution_timeout_seconds
        self.security_recorder = security_recorder
        self._runs: dict[str, _Run] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._space_locks: dict[str, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    def _security(self, detail: str, *, actor: str = "", space: str = "",
                  conversation: str = "", run_id: str = "") -> None:
        """记录进入管线之前就被拒绝的越权请求（未装配回调时静默跳过）。"""
        if self.security_recorder is None:
            return
        self.security_recorder("security.cross_space", detail=detail, actor_id=actor or "",
                               space_id=space or "", conversation_id=conversation or "",
                               run_id=run_id or "")

    @staticmethod
    def _public(run: _Run) -> dict:
        return {"run_id": run.id, "message_id": run.message_id, "status": run.status,
                "space_id": run.space, "conversation_id": run.conversation, "request_id": run.request_id}

    async def _authorize(self, run: _Run, actor: str, *, versions: bool = False) -> None:
        if actor != run.actor or self._runs.get(run.id) is not run:
            raise DomainError(404, "未找到该运行记录，请刷新列表")
        try:
            if run.authorize_actor is not None:
                await run.authorize_actor()
            value = await _call(self.service, "get_conversation", actor, run.conversation)
            if (value.get("space_id") != run.space or value.get("space_epoch", value.get("epoch")) != run.epoch
                    or value.get("owner_id", actor) != actor):
                raise DomainError(404, "未找到该运行记录，请刷新列表")
            if versions:
                for version_id in run.versions:
                    item = await _call(self.service, "get_version", actor, version_id)
                    if item.get("space_id") != run.space or item.get("index_status", "ready") != "ready":
                        raise DomainError(404, "未找到该运行记录，请刷新列表")
                    fingerprint = hashlib.sha256(json.dumps(item, ensure_ascii=False).encode()).hexdigest()
                    previous = run.version_hashes.setdefault(version_id, fingerprint)
                    if previous != fingerprint:
                        raise DomainError(404, "未找到该运行记录，请刷新列表")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if getattr(error, "status_code", 404) in (401, 403):
                self._security("运行授权被拒（会话/资料越权）", actor=actor, space=run.space,
                               conversation=run.conversation, run_id=run.id)
            raise DomainError(404, "未找到该运行记录，请刷新列表") from None

    def _find(self, actor: str, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None or run.actor != actor:
            raise DomainError(404, "未找到该运行记录，请刷新列表")
        return run

    async def start(self, actor: str, space: str, conversation: str, question: str,
                    mode: str = "qa", request_id: str | None = None, *, authorize_actor=None,
                    selected_version_id: str | None = None) -> dict:
        if authorize_actor is not None:
            try:
                await authorize_actor()
            except DomainError as error:
                if error.status_code in (401, 403):
                    self._security("提问前授权被拒（跨空间读取）", actor=actor, space=space,
                                   conversation=conversation)
                raise
        if not isinstance(question, str) or not question.strip() or len(question) > 8000:
            raise DomainError(422, "问题长度需在 1 到 8000 字符之间")
        if mode not in {"qa", "align", "progress"}:
            raise DomainError(422, "问答模式不正确")
        if request_id is not None and (not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200):
            raise DomainError(422, "请求 ID 不正确")
        value = await _call(self.service, "get_conversation", actor, conversation)
        if value.get("space_id") != space:
            self._security("会话不属于当前知识库", actor=actor, space=space, conversation=conversation)
            raise DomainError(403, "该会话不属于当前知识库")
        if value.get("owner_id", actor) != actor:
            raise DomainError(404, "未找到该会话，请刷新列表")
        epoch = value.get("space_epoch", value.get("epoch"))
        if selected_version_id is not None:
            if (not isinstance(selected_version_id, str) or not selected_version_id.strip()
                    or len(selected_version_id) > 200 or mode == 'progress'):
                raise DomainError(422, 'Invalid selected version id')
            selected = await _call(self.service, 'get_version', actor, selected_version_id)
            if selected.get('space_id') != space or selected.get('index_status', 'ready') != 'ready':
                self._security("所选资料超出可读范围", actor=actor, space=space,
                               conversation=conversation)
                raise DomainError(403, 'Selected document is outside the readable scope')
        fingerprint = hashlib.sha256(json.dumps([space, conversation, question, mode, selected_version_id], ensure_ascii=False).encode()).hexdigest()
        async with self._guard:
            if request_id is not None:
                previous = self._requests.get((actor, request_id))
                if previous in self._runs:
                    run = self._runs[previous]
                    await self._authorize(run, actor)
                    if run.fingerprint != fingerprint:
                        raise DomainError(409, "该请求 ID 已被其他问题使用")
                    return self._public(run)
            active = [run for run in self._runs.values() if run.completed is None]
            if len(active) >= self.max_concurrent or sum(run.actor == actor for run in active) >= self.actor_limit:
                raise DomainError(429, "研究运行次数已达上限，请稍后再试")
            if len(self._runs) >= self.max_retained_runs:
                completed = sorted((run for run in self._runs.values() if run.completed is not None), key=lambda r: r.completed)
                if not completed:
                    raise DomainError(429, "研究运行资源已满，请稍后再试")
                self._discard(completed[0])
            run = _Run(uuid.uuid4().hex, actor, space, conversation, epoch, request_id or uuid.uuid4().hex,
                       fingerprint, authorize_actor=authorize_actor)
            run.started_at = time.monotonic()
            self.bus.create(run.id, actor=actor, space=space, conversation=conversation, request_id=run.request_id)
            self._runs[run.id] = run
            self._requests[(actor, run.request_id)] = run.id
            self._space_locks.setdefault(space, asyncio.Lock())
            run.task = asyncio.create_task(self._execute(run, question, mode, selected_version_id), name="research-run")
            run.lease = asyncio.create_task(self._lease(run, self.first_attach_seconds), name="research-first-attach")
            run.deadline = asyncio.create_task(self._deadline(run), name="research-deadline")
            return self._public(run)

    async def _finish_answer(self, run: _Run, result: dict) -> None:
        result = {**result, "message_id": run.message_id, "run_id": run.id, "request_id": run.request_id}
        run.versions.update(citation["version_id"] for citation in result.get("citations", [])
                            if isinstance(citation, dict) and isinstance(citation.get("version_id"), str))
        await self._authorize(run, run.actor, versions=True)
        final = {key: result[key] for key in ("answer", "citations", "conversation_id", "mode", "fallback", "answer_kind",
                 "answer_status", "clarification_kind", "limitations", "missing_evidence", "suggested_actions", "evidence_scope",
                 "message_id", "run_id", "request_id") if key in result}
        final["replace"] = True
        await self.bus.publish(run.id, "answer.final", final)
        run.result = json.loads(json.dumps(result, ensure_ascii=False))
        run.status = "completed"

    async def _recover_committed(self, run: _Run) -> None:
        try:
            if run.guard.result is None:
                raise ValueError("Committed answer unavailable")
            await self._finish_answer(run, run.guard.result)
        except Exception:
            # Persistence is irreversible, access is not. Do not describe a
            # committed answer as failed, or replay formerly authorized prose.
            run.status, run.result = "completed", None
            run.guard.result = None
            self.bus.purge_content(run.id)
            run.duration_ms = (time.monotonic() - run.started_at) * 1000 if run.started_at is not None else None
            await self.bus.publish(run.id, "run.completed", {"replace": True, "answer": "",
                "message_id": run.message_id, "persisted": True, "reason": "access_changed",
                "total_tokens": run.total_tokens, "duration_ms": run.duration_ms})

    async def _execute(self, run: _Run, question: str, mode: str, selected_version_id=None) -> None:
        async def emit(event: dict) -> None:
            run.guard.check()
            await self._authorize(run, run.actor)
            kind = event.get("type")
            if kind == "answer.delta":
                text, ids = event.get("text"), event.get("version_ids", [])
                if not isinstance(text, str) or len(text) > 12000 or not isinstance(ids, list):
                    raise ValueError("Invalid answer fragment")
                run.versions.update(key for key in ids if isinstance(key, str))
                await self._authorize(run, run.actor, versions=True)
                data = {"text": text, "version_ids": sorted(run.versions), "tentative": True, "message_id": run.message_id}
            else:
                if kind == 'document.read':
                    key = event.get('version_id')
                    if not isinstance(key, str):
                        raise ValueError('Missing document read provenance')
                    run.versions.add(key)
                    await self._authorize(run, run.actor, versions=True)
                clean = normalize_answer_metadata({"events": [event]})["events"]
                if not clean or kind not in {"retrieval.started", "retrieval.completed", "retrieval.skipped", "retrieval.degraded",
                                              "rerank.started", "rerank.completed", "rerank.skipped", "rerank.degraded",
                                              "agent.started", "agent.completed", "document.read"}:
                    return
                data = {key: value for key, value in clean[0].items() if key != "type"}
            await self.bus.publish(run.id, kind, data)

        try:
            async with AsyncExitStack() as resources:
                await resources.enter_async_context(self._space_locks[run.space])
                if self.execution_limiter is not None:
                    await resources.enter_async_context(self.execution_limiter)
                run.guard.check()
                await self._authorize(run, run.actor)
                run.status = "running"
                await self.bus.publish(run.id, "run.started", {"message_id": run.message_id})
                parameters = inspect.signature(self.assistant.answer).parameters
                accepts_kwargs = any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
                async def check_session():
                    run.guard.check()
                    await self._authorize(run, run.actor)
                available = {"event_sink": emit, "authorization_check": check_session, "commit_guard": run.guard,
                             "message_id": run.message_id, "run_id": run.id, "request_id": run.request_id,
                             "save_authorization_check": getattr(run.authorize_actor, "repository_check", None),
                             "selected_version_id": selected_version_id}
                options = {key: value for key, value in available.items() if key in parameters or accepts_kwargs}
                result = await self.assistant.answer(run.actor, run.space, run.conversation, question, mode, **options)
                if isinstance(result, dict):
                    run.total_tokens = result.get("_meta", {}).get("total_tokens", 0)
                run.duration_ms = (time.monotonic() - run.started_at) * 1000 if run.started_at is not None else None
                await self._finish_answer(run, result)
        except asyncio.CancelledError:
            if run.guard.committed and run.guard.result is not None:
                # A durable answer wins even if cancellation reached the coroutine
                # while its synchronous worker was returning from COMMIT.
                await self._recover_committed(run)
            else:
                run.status = "failed" if run.stop_reason == "execution_deadline" else "cancelled"
                self.bus.purge_content(run.id)
                run.duration_ms = (time.monotonic() - run.started_at) * 1000 if run.started_at is not None else None
                kind = "run.failed" if run.status == "failed" else "run.cancelled"
                await self.bus.publish(run.id, kind, {"reason": run.stop_reason or "cancelled", "replace": True,
                                                     "answer": "", "message_id": run.message_id,
                                                     "total_tokens": run.total_tokens, "duration_ms": run.duration_ms})
        except Exception as exc:
            if run.guard.committed:
                await self._recover_committed(run)
            else:
                run.status = "failed"
                self.bus.purge_content(run.id)
                run.duration_ms = (time.monotonic() - run.started_at) * 1000 if run.started_at is not None else None
                reason = "backpressure" if isinstance(exc, RunBackpressure) else "run_unavailable"
                await self.bus.publish(run.id, "run.failed", {"reason": reason, "replace": True, "answer": "",
                                                             "message_id": run.message_id,
                                                             "total_tokens": run.total_tokens, "duration_ms": run.duration_ms})
        finally:
            run.completed = time.monotonic()
            for timer in (run.lease, run.deadline):
                if timer is not None and timer is not asyncio.current_task():
                    timer.cancel()
            run.expiry = asyncio.create_task(self._expire(run), name="research-run-expiry")
            if not any(item.space == run.space and item.completed is None for item in self._runs.values()):
                self._space_locks.pop(run.space, None)

    async def _stop(self, run: _Run, reason: str) -> None:
        async with run.stop_lock:
            if run.completed is not None or run.task is None:
                return
            cancelled = await asyncio.to_thread(run.guard.cancel)
            if cancelled:
                run.stop_reason = reason
                run.task.cancel()
            try:
                await asyncio.shield(run.task)
            except asyncio.CancelledError:
                if not run.task.cancelled():
                    raise
                # A task cancelled before its first instruction has no finally.
                run.status, run.completed = "cancelled", time.monotonic()
                self.bus.purge_content(run.id)
                run.duration_ms = (time.monotonic() - run.started_at) * 1000 if run.started_at is not None else None
                await self.bus.publish(run.id, "run.cancelled", {"replace": True, "answer": "",
                                                                  "message_id": run.message_id, "reason": reason,
                                                                  "total_tokens": run.total_tokens,
                                                                  "duration_ms": run.duration_ms})
                for timer in (run.lease, run.deadline):
                    if timer is not None and timer is not asyncio.current_task():
                        timer.cancel()
                run.expiry = asyncio.create_task(self._expire(run), name="research-run-expiry")
                if not any(item.space == run.space and item.completed is None for item in self._runs.values()):
                    self._space_locks.pop(run.space, None)

    async def _lease(self, run: _Run, delay: float) -> None:
        await asyncio.sleep(delay)
        if run.subscribers == 0 and run.completed is None:
            await self._stop(run, "consumer_timeout")

    async def _deadline(self, run: _Run) -> None:
        await asyncio.sleep(self.execution_timeout_seconds)
        await self._stop(run, "execution_deadline")

    def _attach(self, run: _Run):
        run.subscribers += 1
        if run.lease is not None:
            run.lease.cancel()
            run.lease = None

    def _detach(self, run: _Run):
        run.subscribers -= 1
        if run.subscribers == 0 and run.completed is None:
            run.lease = asyncio.create_task(self._lease(run, self.disconnect_grace_seconds), name="research-reconnect-lease")

    async def _expire(self, run: _Run) -> None:
        await asyncio.sleep(self.ttl_seconds)
        self._discard(run)

    def _discard(self, run: _Run) -> None:
        run.result = None
        run.guard.result = None
        self._runs.pop(run.id, None)
        self._requests.pop((run.actor, run.request_id), None)
        self.bus.discard(run.id)
        for timer in (run.expiry, run.lease, run.deadline):
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()

    async def get(self, actor: str, run_id: str, *, authorize_actor=None) -> dict:
        run = self._find(actor, run_id)
        if authorize_actor is not None:
            await authorize_actor()
        await self._authorize(run, actor, versions=True)
        return self._public(run)

    async def subscribe(self, actor: str, run_id: str, after_seq: int = 0, *,
                        authorize_actor=None, include_heartbeats=False) -> AsyncIterator[RunEvent | None]:
        run = self._find(actor, run_id)
        if authorize_actor is not None:
            await authorize_actor()
        await self._authorize(run, actor, versions=True)
        self._attach(run)
        reader = self.bus.listen(run_id, after_seq)
        try:
            async for event in reader:
                if authorize_actor is not None:
                    await authorize_actor()
                await self._authorize(run, actor, versions=True)
                # Authorization can yield while a cancellation purges the bus.
                # Never emit an old replay body after terminal replacement.
                content_cleared = run.status in {"cancelled", "failed"} or (run.guard.committed and run.status == "completed" and run.result is None)
                if event is not None and content_cleared and event.type not in {"run.cancelled", "run.failed", "run.completed"}:
                    continue
                if event is not None or include_heartbeats:
                    yield event
        finally:
            await reader.aclose()
            self._detach(run)

    async def wait_result(self, actor: str, run_id: str, *, authorize_actor=None) -> dict:
        run = self._find(actor, run_id)
        if authorize_actor is not None:
            await authorize_actor()
        await self._authorize(run, actor, versions=True)
        self._attach(run)
        try:
            # A waiter is a consumer but does not need a bounded SSE queue.
            while run.task is not None and not run.task.done():
                await asyncio.wait({run.task}, timeout=.25)
                if authorize_actor is not None:
                    await authorize_actor()
                await self._authorize(run, actor, versions=True)
            if authorize_actor is not None:
                await authorize_actor()
            await self._authorize(run, actor, versions=True)
            if run.status == "cancelled":
                raise DomainError(409, "研究运行已取消")
            if run.status != "completed" or run.result is None:
                raise DomainError(504 if run.stop_reason == "execution_deadline" else 503, "研究运行暂时不可用，请稍后重试")
            return json.loads(json.dumps(run.result, ensure_ascii=False))
        finally:
            self._detach(run)

    async def cancel(self, actor: str, run_id: str, *, authorize_actor=None) -> dict:
        run = self._find(actor, run_id)
        if authorize_actor is not None:
            await authorize_actor()
        await self._authorize(run, actor)
        await self._stop(run, "cancelled")
        return self._public(run)

    async def aclose(self) -> None:
        await asyncio.gather(*(self._stop(run, "shutdown") for run in list(self._runs.values())
                               if run.completed is None), return_exceptions=True)
        for run in list(self._runs.values()):
            self._discard(run)
        self._space_locks.clear()

    async def close(self) -> None:
        await self.aclose()
