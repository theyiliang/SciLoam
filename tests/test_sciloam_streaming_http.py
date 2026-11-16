"""Real socket HTTP/SSE with paused offline providers and real SQLite commits."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app.application.agents.legacy_orchestrator import ResearchAssistant
from app.application.usecases.retrieval import ResearchRetrieval
from app.domain.ai_ports import AnswerCommitGuard
from app.infrastructure.ai.agentscope_runtime import AgentScopeRuntime
from app.infrastructure.ai.model_gateway import ResearchAISettings
from app.infrastructure.events.run_bus import InMemoryRunBus
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from app.presentation.server import create_app

BODY = "Lactate improves growth under the observed conditions."

# Liveness budgets calibrated for slow Windows/CI disks: every SQLite op opens a
# fresh connection (~50ms) while heartbeat re-authorization polls run
# concurrently. The contracts under test are event ordering and outcomes, never
# wall-clock speed.
WAIT_SECONDS = 20
HOOK_WAIT_SECONDS = 40


class Provider(httpx.AsyncByteStream):
    def __init__(self, version_id):
        self.version_id = version_id
        self.release = threading.Event()
        self.entered = threading.Event()
        self.closed = threading.Event()

    @staticmethod
    def chunk(text):
        return ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n").encode()

    async def __aiter__(self):
        self.entered.set()
        claim = {"text": BODY, "version_ids": [self.version_id],
                 "evidence_reason": "The source explicitly reports lactate and growth under these conditions.",
                 "evidence_quotes": [{"version_id": self.version_id, "text": BODY}]}
        yield self.chunk('{"answer_status":"sufficient","claims":[' + json.dumps(claim) + ']')
        while not self.release.is_set():
            await asyncio.sleep(.01)
        yield self.chunk(',"limitations":[],"missing_evidence":[]}')
        yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed.set()


@pytest.fixture
def live(tmp_path, monkeypatch):
    for key, value in {"LLM_API_KEY": "", "RESEARCH_ENV": "development", "RESEARCH_COOKIE_SECURE": "0",
                       "RESEARCH_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver", "RESEARCH_RETRIEVAL_MODE": "local",
                       "RESEARCH_RAG_ALLOW_EXTERNAL": "0"}.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path / "isolated.sqlite", tmp_path / "frontend")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    provider = None
    try:
        service = app.state.service
        actor = service.bootstrap("owner@example.org", "offline-stream-password-123", "Stream tests")["user"]["id"]
        space = next(item for item in service.list_spaces(actor) if item["kind"] == "private")["id"]
        doc = service.add_document(actor, space, "Lactate growth", BODY)
        conversation = service.create_conversation(actor, space)["id"]
        provider = Provider(doc["latest_version"]["id"])
        runtime = AgentScopeRuntime(ResearchAISettings(llm_api_key="offline", llm_base_url="https://offline.invalid/v1",
            llm_min_interval_seconds=0, llm_max_retries=0),
            stream_transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=provider)))
        async def specialist(agent, payload, *, check, state):
            await check()
            return "untrusted draft"
        runtime.run_specialist = specialist
        manager = app.state.container.runs
        manager.assistant = ResearchAssistant(service, runtime)
        # Production default. A sub-second grace cancels the run before the
        # provider stream is even open on slow disks, which makes the
        # "cancel closes the real stream" contract unobservable.
        manager.disconnect_grace_seconds = 5
        manager.bus.heartbeat_seconds = .04
        with httpx.Client(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", timeout=10, trust_env=False) as client:
            csrf = client.get("/api/auth/me").json()["csrf_token"]
            client.headers["X-CSRF-Token"] = csrf
            assert client.post("/api/auth/login", json={"email": "owner@example.org", "password": "offline-stream-password-123"}).status_code == 200
            # Login rotates CSRF.
            client.headers["X-CSRF-Token"] = client.get("/api/auth/me").json()["csrf_token"]
            yield app, client, actor, space, conversation, provider
    finally:
        if provider is not None:
            provider.release.set()
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def start(client, space, conversation, request_id="stable-request"):
    body = {"space_id": space, "conversation_id": conversation, "question": "lactate growth", "request_id": request_id}
    response = client.post("/api/chat/runs", json=body)
    assert response.status_code == 200, response.text
    return response.json(), body


def frames(response):
    data = []
    for line in response.iter_lines():
        if line:
            data.append(line)
        else:
            if data:
                frame = "\n".join(data)
                data = []
                if frame.startswith(":"):
                    yield None
                else:
                    yield json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))


def until_delta(client, run):
    # Orchestrator no longer emits answer.delta; wait for agent.started which is still emitted via SSE.
    with client.stream("GET", f"/api/chat/runs/{run['run_id']}/events") as response:
        assert response.status_code == 200
        for frame in frames(response):
            if frame and frame["type"] == "agent.started":
                assert frame["data"]["agent"]  # non-empty agent name
                return frame["seq"]
    raise AssertionError("No provider progress event")


def test_socket_sse_heartbeat_reconnect_final_id_and_legacy_dedup(live):
    app, client, actor, space, conversation, provider = live
    run, body = start(client, space, conversation)
    cursor = until_delta(client, run)
    with client.stream("GET", f"/api/chat/runs/{run['run_id']}/events", params={"after_seq": cursor}) as response:
        reader = frames(response)
        # The provider is paused: this must be a real flushed network comment,
        # not a bus poll invisible to HTTP or a completed-response ASGI buffer.
        assert next(reader) is None
        provider.release.set()
        tail = [frame for frame in reader if frame]
    assert tail[-1]["type"] == "answer.final"
    final = tail[-1]["data"]
    assert final["replace"] and final["message_id"] == run["message_id"]
    assert final["run_id"] == run["run_id"] and final["request_id"] == body["request_id"]
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert history[-1]["id"] == run["message_id"]
    assert history[-1]["content"] == final["answer"]
    legacy = client.post("/api/chat", json=body)
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["message_id"] == run["message_id"]
    assert len(client.get(f"/api/conversations/{conversation}").json()["messages"]) == 2
    assert client.post(f"/api/chat/runs/{run['run_id']}/cancel").json()["status"] == "completed"
    assert provider.closed.wait(WAIT_SECONDS)


def test_socket_cancel_has_no_assistant_and_old_replay_is_unavailable(live):
    app, client, actor, space, conversation, provider = live
    run, _ = start(client, space, conversation)
    # Cancel only once the upstream stream is genuinely in flight; cancelling
    # earlier would have nothing to close and prove nothing.
    assert provider.entered.wait(WAIT_SECONDS)
    response = client.post(f"/api/chat/runs/{run['run_id']}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert provider.closed.wait(WAIT_SECONDS)
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert not any(item["role"] == "assistant" for item in history)
    response = client.get(f"/api/chat/runs/{run['run_id']}/events")
    assert response.status_code == 409
    assert BODY not in response.text


def test_sqlite_insert_cancel_race_rolls_back_after_to_thread_cancel(live, monkeypatch):
    app, client, actor, space, conversation, provider = live
    manager = app.state.container.runs
    manager.disconnect_grace_seconds = 10
    cancel_authorized, allow_cancel = threading.Event(), threading.Event()
    inserted, release_insert = threading.Event(), threading.Event()
    original_stop, original_add = manager._stop, SQLiteResearchRepository.add

    async def controlled_stop(run, reason):
        if reason == "cancelled":
            cancel_authorized.set()
            while not allow_cancel.is_set():
                await asyncio.sleep(.005)
        return await original_stop(run, reason)

    def paused_insert(repository, entity, record):
        result = original_add(repository, entity, record)
        if entity == "messages" and record["role"] == "assistant":
            inserted.set()
            assert release_insert.wait(HOOK_WAIT_SECONDS)
        return result

    monkeypatch.setattr(manager, "_stop", controlled_stop)
    monkeypatch.setattr(SQLiteResearchRepository, "add", paused_insert)
    run, _ = start(client, space, conversation)
    until_delta(client, run)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, f"/api/chat/runs/{run['run_id']}/cancel")
        try:
            assert cancel_authorized.wait(WAIT_SECONDS)
            provider.release.set()
            assert inserted.wait(WAIT_SECONDS), "real SQLite INSERT must occur before cancellation"
            allow_cancel.set()
            # Cancellation returns while the worker remains paused after INSERT.
            response = request.result(WAIT_SECONDS)
            assert response.status_code == 200 and response.json()["status"] == "cancelled"
        finally:
            allow_cancel.set()
            release_insert.set()
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert not any(item["role"] == "assistant" for item in history)


def test_sqlite_commit_wins_cancel_returns_completed(live, monkeypatch):
    app, client, actor, space, conversation, provider = live
    app.state.container.runs.disconnect_grace_seconds = 10
    committed, release = threading.Event(), threading.Event()
    original = AnswerCommitGuard.commit_scope

    @contextmanager
    def pause_after_commit(guard):
        with original(guard):
            yield
        committed.set()  # SQLite COMMIT and guard.committed already succeeded.
        assert release.wait(HOOK_WAIT_SECONDS)

    monkeypatch.setattr(AnswerCommitGuard, "commit_scope", pause_after_commit)
    run, _ = start(client, space, conversation)
    until_delta(client, run)
    provider.release.set()
    try:
        assert committed.wait(WAIT_SECONDS)
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.post, f"/api/chat/runs/{run['run_id']}/cancel")
            release.set()
            response = request.result(WAIT_SECONDS)
        assert response.status_code == 200 and response.json()["status"] == "completed"
    finally:
        release.set()
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert len([item for item in history if item["role"] == "assistant"]) == 1
    assert history[-1]["id"] == run["message_id"]


def test_commit_then_logout_does_not_publish_formerly_authorized_body(live, monkeypatch):
    app, client, actor, space, conversation, provider = live
    app.state.container.runs.disconnect_grace_seconds = 10
    committed, release, returned = threading.Event(), threading.Event(), threading.Event()
    original = AnswerCommitGuard.commit_scope

    @contextmanager
    def pause_after_commit(guard):
        with original(guard):
            yield
        committed.set()
        assert release.wait(HOOK_WAIT_SECONDS)
        returned.set()

    monkeypatch.setattr(AnswerCommitGuard, "commit_scope", pause_after_commit)
    run, _ = start(client, space, conversation)
    until_delta(client, run)
    provider.release.set()
    try:
        assert committed.wait(WAIT_SECONDS)
        assert client.post("/api/auth/logout").status_code == 200
    finally:
        release.set()
    assert returned.wait(WAIT_SECONDS)
    # A fresh authenticated request cannot access another session's run after
    # logout; durable history exists, but no replay contains its prior prose.
    assert client.get(f"/api/chat/runs/{run['run_id']}").status_code == 401
    deadline = time.monotonic() + WAIT_SECONDS
    channel = app.state.container.runs.bus._channels[run["run_id"]]
    while not channel.closed and time.monotonic() < deadline:
        time.sleep(.01)
    assert channel.closed
    retained = [event for event, _ in channel.events]
    assert [event.type for event in retained] == ["run.completed"]
    assert retained[0].data["answer"] == "" and retained[0].data["persisted"]
    assert BODY not in json.dumps([event.as_dict() for event in retained])
    assert app.state.container.runs._runs[run["run_id"]].status == "completed"
    assert app.state.service.get_conversation(actor, conversation)["messages"][-1]["id"] == run["message_id"]


def test_progress_goal_edit_after_last_check_cannot_commit_stale_answer(live, monkeypatch):
    app, client, actor, space, conversation, provider = live
    service = app.state.service
    goal = service.create_goal(actor, "Original goal")
    app.state.container.runs.assistant.runtime = None
    entered, release = threading.Event(), threading.Event()
    original = service.append_message

    def pause_before_transaction(*args, **kwargs):
        if args[2] == "assistant":
            entered.set()
            assert release.wait(HOOK_WAIT_SECONDS)
        return original(*args, **kwargs)

    # Preserve signature capability detection; only the worker's transaction
    # entry is delayed, after the request's final async check has already run.
    import functools
    monkeypatch.setattr(service, "append_message", functools.wraps(original)(pause_before_transaction))
    body = {"space_id": space, "conversation_id": conversation, "question": "项目进度", "mode": "progress"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, "/api/chat", json=body)
        try:
            assert entered.wait(WAIT_SECONDS)
            service.update_goal(actor, goal["id"], goal["revision"], title="Changed goal")
        finally:
            release.set()
        response = request.result(WAIT_SECONDS)
    assert response.status_code == 503, response.text
    assert not any(message["role"] == "assistant" for message in service.get_conversation(actor, conversation)["messages"])


async def test_slow_bus_replay_cannot_emit_purged_body():
    bus = InMemoryRunBus()
    bus.create("r", actor="a", space="s", conversation="c", request_id="q")
    await bus.publish("r", "run.started", {})
    await bus.publish("r", "answer.delta", {"text": "OLD_PRIVATE_BODY"})
    reader = bus.listen("r")
    assert (await anext(reader)).type == "run.started"
    bus.purge_content("r")
    await bus.publish("r", "run.cancelled", {"replace": True, "answer": ""})
    tail = [event async for event in reader]
    assert [event.type for event in tail] == ["run.cancelled"]
    assert "OLD_PRIVATE_BODY" not in json.dumps([event.as_dict() for event in tail])
