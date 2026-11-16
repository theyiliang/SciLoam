"""Goal-only run unit tests; no network or persistent user database."""
import asyncio
import threading

import pytest

from app.application.usecases.goal_generation_runs import GoalGenerationRuns
from app.domain.errors import DomainError
from app.infrastructure.events.goal_generation_bus import GoalGenerationBus
from tests.test_sciloam_goal_drafts import world


def test_rule_stages_result_scope_and_dedup(world):
    service, repo, _ = world
    async def scenario():
        manager = GoalGenerationRuns(service, GoalGenerationBus())
        try:
            run = await manager.start('owner', ['v1'], 'same')
            assert (await manager.start('owner', ['v1'], 'same'))['run_id'] == run['run_id']
            with pytest.raises(DomainError) as error:
                await manager.start('owner', ['v1'], 'same', expected_revision=2, regenerates_id='different')
            assert error.value.status_code == 409
            events = [event async for event in manager.subscribe('owner', run['run_id'])]
            kinds = [e.type for e in events]
            assert kinds == ['goal.started', 'sources.validation.started', 'sources.validation.completed',
                'sources.read.started', 'sources.read.completed', 'generation.started', 'generation.completed',
                'evidence.validation.started', 'evidence.validation.completed', 'draft.save.started',
                'draft.save.completed', 'goal.completed']
            for seq, event in enumerate(events, 1):
                body = event.as_dict()
                assert body['seq'] == seq and body['request_id'] == 'same' and body['version_ids'] == ['v1']
                assert 'conversation_id' not in body and 'actor_id' not in body
                assert 'percent' not in str(body) and 'answer' not in str(body)
            result = await manager.get('owner', run['run_id'])
            assert result['result']['draft']['status'] == 'draft'
            assert 'draft' not in events[-1].data and repo.list('goal_nodes') == []
            with pytest.raises(DomainError) as error:
                await manager.get('other', run['run_id'])
            assert error.value.status_code == 404
            assert len(repo.list('goal_drafts')) == 1
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_no_extractable_source_has_specific_safe_failure(world):
    service, repo, _ = world
    repo.update('versions', 'v1', {'content': 'ordinary prose without goal requirements'})
    async def scenario():
        manager = GoalGenerationRuns(service, GoalGenerationBus())
        try:
            run = await manager.start('owner', ['v1'])
            events = [e async for e in manager.subscribe('owner', run['run_id'])]
            assert events[-1].type == 'goal.failed'
            assert events[-1].data['reason'] == 'no_extractable_goals'
            assert repo.list('goal_drafts') == []
        finally:
            await manager.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['cancel', 'first_attach', 'deadline', 'disconnect'])
def test_waiting_generation_is_stopped_without_writing(world, mode):
    service, repo, _ = world
    started, exited = threading.Event(), threading.Event()
    class Waiting:
        def generate(self, versions, *, check):
            started.set()
            try:
                while True:
                    check()
                    threading.Event().wait(.005)
            finally:
                exited.set()
    service._allow_experimental_goal_generator = True  # Explicit offline adapter experiment.
    service._goal_generator = Waiting()
    async def scenario():
        manager = GoalGenerationRuns(service, GoalGenerationBus(heartbeat_seconds=.01),
            first_attach_seconds=.5, disconnect_grace_seconds=.05,
            execution_timeout_seconds=.12 if mode == 'deadline' else 2)
        try:
            run = await manager.start('owner', ['v1'])
            assert await asyncio.to_thread(started.wait, 1)
            if mode == 'cancel':
                await manager.cancel('owner', run['run_id'])
            elif mode in {'deadline', 'disconnect'}:
                reader = manager.subscribe('owner', run['run_id'])
                async for event in reader:
                    if event.type == 'generation.started':
                        break
                if mode == 'disconnect':
                    await reader.aclose()
                else:
                    async for event in reader:
                        if event.type == 'goal.failed':
                            break
                    await reader.aclose()
            await asyncio.wait_for(manager._runs[run['run_id']].task, 1)
            assert await asyncio.to_thread(exited.wait, 1)
            final = await manager.get('owner', run['run_id'])
            assert final['status'] == ('failed' if mode == 'deadline' else 'cancelled')
            assert repo.list('goal_drafts') == []
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_invalid_model_evidence_is_specific_and_never_saved(world):
    service, repo, _ = world
    class Invalid:
        def generate(self, versions, *, check):
            check()
            from app.domain.goal_drafts import extract_nodes
            nodes = extract_nodes(versions)
            nodes[0]['source']['quote'] = 'invented evidence'
            return {'nodes': nodes, 'generation': {'method': 'ai'}}
    service._allow_experimental_goal_generator = True  # Preserve invalid-evidence experiment.
    service._goal_generator = Invalid()
    async def scenario():
        manager = GoalGenerationRuns(service, GoalGenerationBus())
        try:
            run = await manager.start('owner', ['v1'])
            events = [e async for e in manager.subscribe('owner', run['run_id'])]
            assert events[-1].data['reason'] == 'invalid_evidence'
            assert 'invented' not in str(events[-1].as_dict())
            assert repo.list('goal_drafts') == [] and repo.list('goal_nodes') == []
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_start_rejects_nonowner_and_wrong_source_before_generator(world):
    service, repo, _ = world
    async def scenario():
        manager = GoalGenerationRuns(service, GoalGenerationBus())
        try:
            with pytest.raises(DomainError) as error:
                await manager.start('member', ['v1'])
            assert error.value.status_code == 403
            with pytest.raises(DomainError):
                await manager.start('owner', ['v1', 'v1'])
            with pytest.raises(DomainError):
                await manager.start('owner', ['missing'])
            assert manager._runs == {}
        finally:
            await manager.aclose()
    asyncio.run(scenario())


def test_goal_bus_rejects_chat_and_isolated_replay():
    async def scenario():
        bus = GoalGenerationBus(ring_size=1)
        bus.create('a', actor='owner', space='', conversation='', request_id='request')
        with pytest.raises(ValueError):
            await bus.publish('a', 'answer.final', {'answer': 'never'})
        await bus.publish('a', 'goal.started', {'version_ids': ['v']})
        await bus.publish('a', 'goal.completed', {'version_ids': ['v'], 'draft_id': 'd'})
        from app.domain.run_events import RunReplayExpired
        with pytest.raises(RunReplayExpired):
            await anext(bus.listen('a', 3))
        assert (await anext(bus.listen('a', 0))).data['draft_id'] == 'd'
        assert (await anext(bus.listen('a', 1))).data['draft_id'] == 'd'
    asyncio.run(scenario())
