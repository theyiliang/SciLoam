"""New main path through the canonical streaming run lifecycle."""
import asyncio
import json

import pytest

from app.application.agents.orchestrator import ResearchAssistant
from app.application.usecases.chat_runs import RunManager
from app.infrastructure.events.run_bus import InMemoryRunBus
from tests.test_model_led_assistant import library, response, claim, Runtime, material


async def test_streaming_without_delta_then_final_matches_history(library):
    service, actor, space, conv = library
    released = asyncio.Event()
    class Streaming(Runtime):
        async def stream_coordinator(self, agent, payload, **options):
            text = json.dumps(response(claim('通用概念解释。')), ensure_ascii=False)
            yield text[:-1]
            await released.wait()
            yield text[-1]
    manager = RunManager(service, ResearchAssistant(service, Streaming(None)), InMemoryRunBus())
    run = await manager.start(actor, space, conv, '普通概念')
    reader = manager.subscribe(actor, run['run_id'])
    try:
        async with asyncio.timeout(10):
            # Orchestrator no longer emits answer.delta; wait for agent.started (still emitted for progress).
            while True:
                event = await anext(reader)
                if event.type == 'agent.started':
                    assert len(service.get_conversation(actor, conv)['messages']) == 1
                    break
            released.set()
            while True:
                event = await anext(reader)
                if event.type == 'answer.final':
                    assert event.data['answer_kind'] == 'general'
                    assert event.data['answer'] == service.get_conversation(actor, conv)['messages'][-1]['content']
                    break
    finally:
        released.set()
        await reader.aclose()
        await manager.aclose()


async def test_cancel_during_stream_never_saves_final(library):
    service, actor, space, conv = library
    class Streaming(Runtime):
        async def stream_coordinator(self, agent, payload, **options):
            yield json.dumps(response(claim('CANCEL_PREVIEW')), ensure_ascii=False)[:-1]
            await asyncio.Event().wait()
    manager = RunManager(service, ResearchAssistant(service, Streaming(None)), InMemoryRunBus())
    run = await manager.start(actor, space, conv, '普通概念')
    reader = manager.subscribe(actor, run['run_id'])
    try:
        async with asyncio.timeout(10):
            # Orchestrator no longer emits answer.delta; wait for agent.started.
            while (await anext(reader)).type != 'agent.started':
                pass
            await manager.cancel(actor, run['run_id'])
            while True:
                event = await anext(reader)
                if event.type == 'run.cancelled':
                    assert event.data['answer'] == '' and event.data['replace']
                    break
        assert [row['role'] for row in service.get_conversation(actor, conv)['messages']] == ['user']
    finally:
        await reader.aclose()
        await manager.aclose()


async def test_document_clarification_metadata_reaches_final_and_history(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        return {'answer_status': 'partial', 'clarification_kind': 'document',
                'claims': [claim('请选择材料', 'clarification')]}
    manager = RunManager(service, ResearchAssistant(service, Runtime(action)), InMemoryRunBus())
    try:
        run = await manager.start(actor, space, conv, '这个文档说了啥')
        events = [event async for event in manager.subscribe(actor, run['run_id'])]
        final = next(event for event in events if event.type == 'answer.final')
        assert final.data['clarification_kind'] == 'document'
        assert final.data['suggested_actions'] == ['select_material']
        saved = service.get_conversation(actor, conv)['messages'][-1]
        assert saved['clarification_kind'] == 'document' and saved['content'] == final.data['answer']
    finally:
        await manager.aclose()


async def test_selected_version_is_in_request_fingerprint(library):
    service, actor, space, conv = library
    a = service.add_document(actor, space, '甲', '甲正文')['latest_version']['id']
    b = service.add_document(actor, space, '乙', '乙正文')['latest_version']['id']
    async def action(tools, payload):
        return response(material(await tools['read_document'](payload['selected_document']['version_id'])))
    manager = RunManager(service, ResearchAssistant(service, Runtime(action)), InMemoryRunBus())
    try:
        run = await manager.start(actor, space, conv, '概览', request_id='same', selected_version_id=a)
        result = await manager.wait_result(actor, run['run_id'])
        assert result['citations'][0]['version_id'] == a
        from app.domain.errors import DomainError
        with pytest.raises(DomainError) as error:
            await manager.start(actor, space, conv, '概览', request_id='same', selected_version_id=b)
        assert error.value.status_code == 409
    finally:
        await manager.aclose()
