"""Stage 8: offline real-incremental provider streaming and authorized run lifecycle."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.application.agents.legacy_orchestrator import ResearchAssistant
from app.application.usecases.chat_runs import RunManager
from app.domain.errors import DomainError
from app.domain.run_events import RunBackpressure, RunReplayExpired
from app.infrastructure.ai.agentscope_runtime import AgentScopeRuntime
from app.infrastructure.ai.model_gateway import ResearchAISettings, ModelGateway, InputBudgetExceeded
from app.infrastructure.events.run_bus import InMemoryRunBus
from app.presentation.streaming import encode_sse


BODY = "Lactate improves growth under the observed conditions."
CLAIM = {"text": BODY, "version_ids": ["v1"],
         "evidence_reason": "The source explicitly reports lactate and observed growth in these conditions.",
         "evidence_quotes": [{"version_id": "v1", "text": BODY}]}


class Service:
    def __init__(self):
        self.allowed = True
        self.epoch = 1
        self.body = BODY
        self.messages = []

    def get_conversation(self, actor, conversation):
        if not self.allowed:
            raise DomainError(403, "private revocation detail")
        return {"space_id": "space", "space_epoch": self.epoch, "owner_id": actor,
                "messages": list(self.messages)}

    def get_version(self, actor, version):
        if not self.allowed:
            raise DomainError(403, "private revocation detail")
        return {"space_id": "space", "document_id": "doc", "index_status": "ready",
                "title": "Lactate growth", "content": self.body, "source": "local"}

    def search(self, actor, space, query, limit=5):
        return [{"version_id": "v1", "content": self.body}]

    def append_message(self, actor, conversation, role, content, citations, metadata=None):
        self.get_conversation(actor, conversation)
        message = {"role": role, "content": content, "citations": citations, "metadata": metadata}
        self.messages.append(message)
        return message


class PausedSSE(httpx.AsyncByteStream):
    def __init__(self, release, *, fail=False, unsupported=False):
        self.release, self.fail, self.unsupported = release, fail, unsupported
        self.closed = False

    @staticmethod
    def chunk(text):
        return ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n").encode()

    async def __aiter__(self):
        claim = dict(CLAIM)
        if self.unsupported:
            claim["version_ids"] = ["PRIVATE_UNKNOWN"]
        # This is provider SSE, not a completed answer split by application code.
        yield self.chunk('{"answer_status":"sufficient","claims":[' + json.dumps(claim) + ']')
        await self.release.wait()
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_SECRET")
        yield self.chunk(',"limitations":[],"missing_evidence":[]}')
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b'data: [DONE]\n\n'

    async def aclose(self):
        self.closed = True


def runtime_for(stream):
    def handler(request):
        assert request.url.host == "fake.invalid"
        assert json.loads(request.content)["stream"] is True
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, stream=stream)
    runtime = AgentScopeRuntime(ResearchAISettings(llm_api_key="not-a-real-key", llm_base_url="https://fake.invalid/v1",
        llm_min_interval_seconds=0, llm_max_retries=0), stream_transport=httpx.MockTransport(handler))
    async def specialist(agent, payload, *, check, state):
        await check()
        return "private untrusted draft"
    runtime.run_specialist = specialist
    return runtime


async def collect(manager, run_id):
    return [event async for event in manager.subscribe("alice", run_id)]


async def test_provider_delta_arrives_before_completion_and_final_is_replacement():
    release = asyncio.Event()
    stream = PausedSSE(release)
    service = Service()
    manager = RunManager(service, ResearchAssistant(service, runtime_for(stream)), InMemoryRunBus())
    run = await manager.start("alice", "space", "conversation", "lactate growth")
    reader = manager.subscribe("alice", run["run_id"])
    events = []
    try:
        async with asyncio.timeout(5):
            while True:
                event = await anext(reader)
                events.append(event)
                if event.type == "answer.delta":
                    break
        assert not release.is_set()
        assert BODY in events[-1].data["text"]
        assert not any(message["role"] == "assistant" for message in service.messages)
        release.set()
        events += [event async for event in reader]
        assert events[-1].type == "answer.final" and events[-1].data["replace"] is True
        assert events[-1].data["answer"] == service.messages[-1]["content"]
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert events[0].type == "run.started"
        assert events[1].type == "retrieval.started" and events[2].type == "retrieval.completed"
        assert "answer.delta" not in json.dumps(service.messages[-1]["metadata"])
        assert "private untrusted draft" not in json.dumps([event.as_dict() for event in events])
        assert stream.closed
        wire = encode_sse(events[-1]).decode()
        assert "event: answer.final\n" in wire and "data: {" in wire
    finally:
        await reader.aclose()
        await manager.aclose()


async def test_no_key_local_path_has_progress_and_final_but_no_fake_delta():
    service = Service()
    manager = RunManager(service, ResearchAssistant(service), InMemoryRunBus())
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth")
        events = await collect(manager, run["run_id"])
        assert [event.type for event in events] == ["run.started", "retrieval.started", "retrieval.completed", "answer.final"]
        assert events[-1].data["fallback"] is True
    finally:
        await manager.aclose()


async def test_model_failure_after_valid_delta_replaces_with_canonical_local_answer():
    release = asyncio.Event()
    service = Service()
    manager = RunManager(service, ResearchAssistant(service, runtime_for(PausedSSE(release, fail=True))), InMemoryRunBus())
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth")
        reader = manager.subscribe("alice", run["run_id"])
        events = []
        async with asyncio.timeout(5):
            while not events or events[-1].type != "answer.delta":
                events.append(await anext(reader))
        release.set()
        events += [event async for event in reader]
        assert events[-1].type == "answer.final"
        assert events[-1].data["replace"] is True and events[-1].data["fallback"] is True
        assert "不是AI总结" in events[-1].data["answer"]
        assert "PRIVATE_PROVIDER_SECRET" not in json.dumps([event.as_dict() for event in events])
    finally:
        await manager.aclose()


async def test_unsupported_complete_claim_never_escapes_as_delta():
    release = asyncio.Event()
    release.set()
    service = Service()
    manager = RunManager(service, ResearchAssistant(service, runtime_for(PausedSSE(release, unsupported=True))), InMemoryRunBus())
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth")
        events = await collect(manager, run["run_id"])
        assert not any(event.type == "answer.delta" for event in events)
        assert "PRIVATE_UNKNOWN" not in json.dumps([event.as_dict() for event in events])
    finally:
        await manager.aclose()


async def test_owner_hiding_dedup_actor_limit_and_cancel_no_half_answer():
    release = asyncio.Event()
    service = Service()
    stream = PausedSSE(release)
    manager = RunManager(service, ResearchAssistant(service, runtime_for(stream)), InMemoryRunBus())
    try:
        first = await manager.start("alice", "space", "conversation", "lactate growth", request_id="req")
        second = await manager.start("alice", "space", "conversation", "lactate growth", request_id="req")
        assert first["run_id"] == second["run_id"]
        for method in (manager.get, manager.cancel):
            with pytest.raises(DomainError) as denied:
                await method("mallory", first["run_id"])
            assert denied.value.status_code == 404
        with pytest.raises(DomainError) as conflict:
            await manager.start("alice", "space", "conversation", "another", request_id="req")
        assert conflict.value.status_code == 409
        with pytest.raises(DomainError) as limited:
            await manager.start("alice", "space", "conversation2", "lactate growth")
        assert limited.value.status_code == 429
        reader = manager.subscribe("alice", first["run_id"])
        async with asyncio.timeout(5):
            while (await anext(reader)).type != "answer.delta":
                pass
        cancelled = await manager.cancel("alice", first["run_id"])
        assert cancelled["status"] == "cancelled"
        tail = [event async for event in reader]
        assert tail[-1].type == "run.cancelled" and tail[-1].data["replace"]
        assert not any(message["role"] == "assistant" for message in service.messages)
        assert stream.closed
    finally:
        await manager.aclose()


async def test_epoch_and_version_changes_stop_stream_and_hide_replay():
    for change in ("epoch", "body", "allowed"):
        release = asyncio.Event()
        service = Service()
        manager = RunManager(service, ResearchAssistant(service, runtime_for(PausedSSE(release))), InMemoryRunBus())
        try:
            run = await manager.start("alice", "space", "conversation", "lactate growth")
            reader = manager.subscribe("alice", run["run_id"])
            async with asyncio.timeout(5):
                while (await anext(reader)).type != "answer.delta":
                    pass
            setattr(service, change, 2 if change == "epoch" else "changed" if change == "body" else False)
            release.set()
            with pytest.raises(DomainError) as revoked:
                await anext(reader)
            assert revoked.value.status_code == 404
            assert not any(message["role"] == "assistant" for message in service.messages)
            with pytest.raises(DomainError):
                await manager.get("alice", run["run_id"])
        finally:
            await manager.aclose()


async def test_session_callback_is_checked_during_generation_and_replay():
    service = Service()
    session_allowed = True
    async def authorize():
        if not session_allowed:
            raise DomainError(401, "Session expired")
    release = asyncio.Event()
    manager = RunManager(service, ResearchAssistant(service, runtime_for(PausedSSE(release))), InMemoryRunBus())
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth", authorize_actor=authorize)
        reader = manager.subscribe("alice", run["run_id"], authorize_actor=authorize)
        async with asyncio.timeout(5):
            while (await anext(reader)).type != "answer.delta":
                pass
        session_allowed = False
        release.set()
        with pytest.raises(DomainError):
            await anext(reader)
        assert not any(message["role"] == "assistant" for message in service.messages)
    finally:
        await manager.aclose()


class BlockingAssistant:
    def __init__(self):
        self.entered = []
        self.release = asyncio.Event()

    async def answer(self, actor, space, conversation, question, mode, *, event_sink):
        self.entered.append(conversation)
        await self.release.wait()
        return {"answer": "finished", "citations": [], "conversation_id": conversation}


async def test_global_two_limit_space_round_lock_and_shutdown():
    service, assistant = Service(), BlockingAssistant()
    manager = RunManager(service, assistant, InMemoryRunBus())
    first = await manager.start("alice", "space", "one", "question")
    await manager.start("bob", "space", "two", "question")
    with pytest.raises(DomainError) as limited:
        await manager.start("charlie", "space", "three", "question")
    assert limited.value.status_code == 429
    # get awaits a service call, yielding enough for the first run to take lock.
    await manager.get("alice", first["run_id"])
    assert assistant.entered == ["one"]
    await manager.aclose()
    assert manager._runs == {} and manager.bus._channels == {}


async def test_bus_bounded_replay_backpressure_and_terminal_reset():
    bus = InMemoryRunBus(ring_size=2, queue_size=1)
    bus.create("r", actor="alice", space="s", conversation="c", request_id=None)
    await bus.publish("r", "run.started", {})
    reader = bus.listen("r")
    assert (await anext(reader)).seq == 1
    await bus.publish("r", "answer.delta", {"text": "one"})
    with pytest.raises(RunBackpressure):
        await bus.publish("r", "answer.delta", {"text": "two"})
    await bus.publish("r", "run.failed", {"replace": True, "answer": ""})
    assert (await anext(reader)).type == "run.failed"
    await reader.aclose()
    expired = bus.listen("r", 0)
    with pytest.raises(RunReplayExpired):
        await anext(expired)


async def test_ttl_and_disconnect_cancel_without_autonomous_work():
    service, assistant = Service(), BlockingAssistant()
    manager = RunManager(service, assistant, InMemoryRunBus(), ttl_seconds=.01)
    try:
        run = await manager.start("alice", "space", "conversation", "question")
        reader = manager.subscribe("alice", run["run_id"])
        assert (await anext(reader)).type == "run.started"
        await reader.aclose()
        await manager._runs[run["run_id"]].task
        assert manager._runs[run["run_id"]].status == "cancelled"
        await manager._runs[run["run_id"]].expiry
        with pytest.raises(DomainError):
            await manager.get("alice", run["run_id"])
    finally:
        await manager.aclose()


async def test_sync_only_runtime_is_not_sliced_into_fake_deltas():
    class SyncOnlyRuntime:
        def new_request_state(self):
            return object()
        async def run_specialist(self, *args, **kwargs):
            return "draft"
        async def run_coordinator(self, *args, **kwargs):
            return json.dumps({"answer_status": "sufficient", "claims": [CLAIM]})
    service = Service()
    manager = RunManager(service, ResearchAssistant(service, SyncOnlyRuntime()), InMemoryRunBus())
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth")
        events = await collect(manager, run["run_id"])
        assert events[-1].type == "answer.final" and not events[-1].data["fallback"]
        assert not any(event.type == "answer.delta" for event in events)
    finally:
        await manager.close()


async def test_subscriber_count_is_bounded():
    bus = InMemoryRunBus(max_subscribers=1)
    bus.create("r", actor="alice", space="s", conversation="c", request_id=None)
    await bus.publish("r", "run.started", {})
    first = bus.listen("r")
    assert (await anext(first)).type == "run.started"
    second = bus.listen("r")
    with pytest.raises(RunBackpressure):
        await anext(second)
    await first.aclose()
    assert not bus._channels["r"].subscribers


async def test_wait_result_uses_canonical_result_and_current_shared_limiter():
    service = Service()
    limiter = asyncio.Semaphore(1)
    manager = RunManager(service, ResearchAssistant(service), InMemoryRunBus(), execution_limiter=limiter)
    await limiter.acquire()
    try:
        run = await manager.start("alice", "space", "conversation", "lactate growth")
        waiter = asyncio.create_task(manager.wait_result("alice", run["run_id"]))
        await manager.get("alice", run["run_id"])
        assert service.messages == [] and not waiter.done()
        limiter.release()
        result = await waiter
        assert result["answer"] == service.messages[-1]["content"]
        assert "events" in result and result["fallback"] is True
        assert limiter._value == 1
    finally:
        await manager.aclose()


async def test_unattached_run_times_out_and_waiter_counts_as_consumer():
    service, assistant = Service(), BlockingAssistant()
    manager = RunManager(service, assistant, InMemoryRunBus(), first_attach_seconds=.03,
                         disconnect_grace_seconds=.03, execution_timeout_seconds=2)
    try:
        run = await manager.start("alice", "space", "conversation", "question")
        await asyncio.wait_for(manager._runs[run["run_id"]].task, 1)
        assert (await manager.get("alice", run["run_id"]))["status"] == "cancelled"
        run = await manager.start("alice", "space", "conversation", "question")
        waiter = asyncio.create_task(manager.wait_result("alice", run["run_id"]))
        # A separate authorized subscriber can disconnect without cancelling the
        # HTTP waiter, even after the complete reconnect lease has elapsed.
        reader = manager.subscribe("alice", run["run_id"])
        await anext(reader)
        await reader.aclose()
        await asyncio.sleep(.08)
        assert not waiter.done() and manager._runs[run["run_id"]].subscribers == 1
        assistant.release.set()
        assert (await waiter)["answer"] == "finished"
    finally:
        await manager.aclose()


async def test_short_disconnect_reconnect_keeps_execution_but_deadline_stops_it():
    service, assistant = Service(), BlockingAssistant()
    manager = RunManager(service, assistant, InMemoryRunBus(), first_attach_seconds=.1,
                         disconnect_grace_seconds=.1, execution_timeout_seconds=.3)
    try:
        run = await manager.start("alice", "space", "conversation", "question")
        reader = manager.subscribe("alice", run["run_id"])
        first = await anext(reader)
        await reader.aclose()
        await asyncio.sleep(.02)
        reader = manager.subscribe("alice", run["run_id"], first.seq)
        terminal = await asyncio.wait_for(anext(reader), 1)
        assert terminal.type == "run.failed" and terminal.data["reason"] == "execution_deadline"
        assert assistant.entered == ["conversation"]
        assert (await manager.get("alice", run["run_id"]))["status"] == "failed"
        await reader.aclose()
    finally:
        await manager.aclose()


async def test_stream_gateway_preserves_budget_and_timeout_closes_iterator():
    settings = ResearchAISettings(llm_min_interval_seconds=0, llm_max_retries=0,
                                  total_input_chars=1, model_timeout_seconds=.01)
    gateway = ModelGateway(settings)
    entered, closed = False, False
    async def check():
        pass
    async def provider(messages):
        nonlocal entered, closed
        entered = True
        try:
            await asyncio.Event().wait()
            yield "never"
        finally:
            closed = True
    iterator = gateway.stream(name="coordinator", primary=provider, fallback=None,
            messages=[{"role": "user", "content": "too long"}], check=check, state=gateway.new_request_state())
    with pytest.raises(InputBudgetExceeded):
        await anext(iterator)
    assert not entered
    gateway = ModelGateway(ResearchAISettings(llm_min_interval_seconds=0, llm_max_retries=0, model_timeout_seconds=.01))
    iterator = gateway.stream(name="coordinator", primary=provider, fallback=None,
            messages=[], check=check, state=gateway.new_request_state())
    with pytest.raises(TimeoutError):
        await anext(iterator)
    assert entered and closed
