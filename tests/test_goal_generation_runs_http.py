"""Real HTTP/SSE goal runs using temporary SQLite and offline MockTransport."""
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

from app.domain.ai_ports import AnswerCommitGuard
from app.domain import goal_drafts as rules
from app.infrastructure.ai.goal_generator import ControlledGoalGenerator
from app.infrastructure.ai.model_gateway import ResearchAISettings
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from app.presentation.server import create_app

BODY = '# Research target\n1. Validate samples\n'

# Liveness budgets calibrated for slow Windows/CI disks: every SQLite op opens a
# fresh connection (~50ms) while heartbeat re-authorization polls run
# concurrently. The contracts under test are event ordering and outcomes, never
# wall-clock speed.
WAIT_SECONDS = 20
HOOK_WAIT_SECONDS = 40


class PausedBody(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload
        self.release = threading.Event()
        self.closed = threading.Event()
        self.entered = threading.Event()

    async def __aiter__(self):
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(.01)
        yield json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps(self.payload)}}]}).encode()

    async def aclose(self):
        self.closed.set()


@pytest.fixture
def live(tmp_path, monkeypatch):
    for key, value in {'LLM_API_KEY': '', 'RESEARCH_ENV': 'development', 'RESEARCH_COOKIE_SECURE': '0',
        'RESEARCH_ALLOWED_HOSTS': 'localhost,127.0.0.1,testserver', 'RESEARCH_RETRIEVAL_MODE': 'local',
        'RESEARCH_RAG_ALLOW_EXTERNAL': '0'}.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path / 'goal-isolated.sqlite', tmp_path / 'frontend')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='on'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    provider = None
    try:
        service = app.state.service
        actor = service.bootstrap('owner@example.org', 'offline-goal-password-123', 'Goal tests')['user']['id']
        basis = next(s for s in service.list_spaces(actor) if s['kind'] == 'basis')['id']
        version = service.add_document(actor, basis, 'Research', BODY)['latest_version']
        version = service.get_version(actor, version['id'])
        nodes = rules.extract_nodes([version])
        provider = PausedBody({'nodes': nodes})
        # Offline experimental harness only; production composition never opts in.
        service._allow_experimental_goal_generator = True
        service._goal_generator = ControlledGoalGenerator(ResearchAISettings(llm_api_key='offline',
            llm_base_url='https://offline.invalid/v1', llm_min_interval_seconds=0, llm_max_retries=0),
            transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=provider)))
        manager = app.state.container.goal_runs
        # Production default: the resume-in-grace contract needs a gap wider
        # than a slow Windows teardown/reconnect cycle.
        manager.disconnect_grace_seconds = 5
        manager.bus.heartbeat_seconds = .03
        with httpx.Client(base_url=f'http://127.0.0.1:{sock.getsockname()[1]}', timeout=10, trust_env=False) as client:
            client.headers['X-CSRF-Token'] = client.get('/api/auth/me').json()['csrf_token']
            assert client.post('/api/auth/login', json={'email': 'owner@example.org', 'password': 'offline-goal-password-123'}).status_code == 200
            client.headers['X-CSRF-Token'] = client.get('/api/auth/me').json()['csrf_token']
            yield app, client, actor, version, provider
    finally:
        if provider:
            provider.release.set()
        server.should_exit = True
        thread.join(10)
        sock.close()
        assert not thread.is_alive()


def start(client, version, **extra):
    response = client.post('/api/goal-draft-runs', json={'version_ids': [version['id']], 'request_id': 'goal-request', **extra})
    assert response.status_code == 200, response.text
    return response.json()


def frames(response):
    data = []
    for line in response.iter_lines():
        if line:
            data.append(line)
        elif data:
            frame, data = '\n'.join(data), []
            yield None if frame.startswith(':') else json.loads(next(l[6:] for l in frame.splitlines() if l.startswith('data: ')))


def wait_status(client, run, status):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        response = client.get('/api/goal-draft-runs/' + run['run_id'])
        assert response.status_code == 200, response.text
        if response.json()['status'] == status:
            return response.json()
        time.sleep(.02)
    pytest.fail('Goal run did not reach ' + status)


def test_real_model_wait_progress_resume_and_full_result(live):
    app, client, actor, version, provider = live
    run = start(client, version)
    assert start(client, version)['run_id'] == run['run_id']
    url = f"/api/goal-draft-runs/{run['run_id']}/events"
    seq = 0
    with client.stream('GET', url) as response:
        assert response.status_code == 200
        for frame in frames(response):
            if frame:
                assert frame['version_ids'] == [version['id']] and frame['request_id'] == 'goal-request'
                assert frame['seq'] > seq
                seq = frame['seq']
                if frame['type'] == 'generation.started':
                    assert frame['data']['method'] == 'ai'
                    assert provider.entered.wait(WAIT_SECONDS)
                    assert client.get('/api/goal-drafts').json() == []
                    assert client.get('/api/goals').json() == []
                    break
    # Resume in grace with a cursor; no repeated stage/event or model call.
    with client.stream('GET', url + f'?after_seq={seq}') as response:
        assert response.status_code == 200
        provider.release.set()
        received = [frame for frame in frames(response) if frame]
    assert received[-1]['type'] == 'goal.completed'
    assert all(frame['seq'] > seq for frame in received)
    assert all('answer' not in frame['type'] and 'delta' not in frame['type'] for frame in received)
    assert 'draft' not in received[-1]['data']
    result = wait_status(client, run, 'completed')['result']['draft']
    assert result['id'] == received[-1]['data']['draft_id'] and result['status'] == 'draft'
    assert result['generation']['method'] == 'ai'
    assert client.get('/api/goals').json() == []
    assert provider.closed.wait(WAIT_SECONDS)
    # Actor is never accepted from client JSON.
    assert client.post('/api/goal-draft-runs', json={'version_ids': [version['id']], 'actor_id': actor}).status_code == 422


def test_cancel_closes_real_wait_and_prevents_draft(live):
    app, client, actor, version, provider = live
    run = start(client, version)
    assert provider.entered.wait(WAIT_SECONDS)
    response = client.post(f"/api/goal-draft-runs/{run['run_id']}/cancel")
    assert response.status_code == 200 and response.json()['status'] == 'cancelled'
    assert provider.closed.wait(WAIT_SECONDS)
    provider.release.set()
    assert client.get('/api/goal-drafts').json() == []
    with client.stream('GET', f"/api/goal-draft-runs/{run['run_id']}/events") as response:
        events = [frame for frame in frames(response) if frame]
    assert [e['type'] for e in events] == ['goal.cancelled']
    assert 'result' not in client.get(f"/api/goal-draft-runs/{run['run_id']}").json()


def test_expired_session_stops_model_and_blocks_replay(live):
    app, client, actor, version, provider = live
    run = start(client, version)
    assert provider.entered.wait(WAIT_SECONDS)
    assert client.post('/api/auth/logout').status_code == 200
    assert provider.closed.wait(WAIT_SECONDS)
    provider.release.set()
    assert app.state.service.list_goal_drafts(actor) == []
    assert client.get(f"/api/goal-draft-runs/{run['run_id']}").status_code == 401
    assert client.get(f"/api/goal-draft-runs/{run['run_id']}/events").status_code == 401


def test_source_changed_during_model_wait_does_not_save(live):
    app, client, actor, version, provider = live
    run = start(client, version)
    assert provider.entered.wait(WAIT_SECONDS)
    app.state.service.add_version(actor, version['document_id'], '# New current goal\n')
    assert provider.closed.wait(WAIT_SECONDS)
    provider.release.set()
    assert app.state.service.list_goal_drafts(actor) == []
    response = client.get(f"/api/goal-draft-runs/{run['run_id']}")
    assert response.status_code == 409


def test_commit_wins_cancel_reports_completed(live, monkeypatch):
    app, client, actor, version, provider = live
    committed, release = threading.Event(), threading.Event()
    original = AnswerCommitGuard.commit_scope
    @contextmanager
    def paused(guard):
        with original(guard):
            yield
        committed.set()
        assert release.wait(HOOK_WAIT_SECONDS)
    monkeypatch.setattr(AnswerCommitGuard, 'commit_scope', paused)
    run = start(client, version)
    provider.release.set()
    try:
        assert committed.wait(WAIT_SECONDS)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(client.post, f"/api/goal-draft-runs/{run['run_id']}/cancel")
            release.set()
            response = future.result(WAIT_SECONDS)
        assert response.status_code == 200 and response.json()['status'] == 'completed'
        assert len(app.state.service.list_goal_drafts(actor)) == 1
    finally:
        release.set()


def test_post_commit_logout_does_not_replay_draft(live, monkeypatch):
    app, client, actor, version, provider = live
    committed, release = threading.Event(), threading.Event()
    original = AnswerCommitGuard.commit_scope
    @contextmanager
    def paused(guard):
        with original(guard):
            yield
        committed.set()
        assert release.wait(HOOK_WAIT_SECONDS)
    monkeypatch.setattr(AnswerCommitGuard, 'commit_scope', paused)
    run = start(client, version)
    provider.release.set()
    try:
        assert committed.wait(WAIT_SECONDS)
        assert client.post('/api/auth/logout').status_code == 200
    finally:
        release.set()
    deadline = time.monotonic() + WAIT_SECONDS
    channel = app.state.container.goal_runs.bus._channels[run['run_id']]
    while not channel.closed and time.monotonic() < deadline:
        time.sleep(.01)
    assert channel.closed
    events = [event for event, _ in channel.events]
    assert [e.type for e in events] == ['goal.completed']
    assert events[0].data['persisted'] is True
    assert 'draft_id' not in events[0].data
    assert BODY not in json.dumps([e.as_dict() for e in events])
    assert len(app.state.service.list_goal_drafts(actor)) == 1


def test_legacy_sync_and_regenerated_run_preserve_formal_tree(live):
    app, client, actor, version, provider = live
    app.state.service._goal_generator = None
    draft = client.post('/api/goal-drafts', json={'version_ids': [version['id']]}).json()
    published = client.post(f"/api/goal-drafts/{draft['id']}/publish", json={'expected_revision': 1, 'confirmed': True})
    assert published.status_code == 200
    tree = client.get('/api/goals').json()
    newer = app.state.service.add_version(actor, version['document_id'], '# Changed research target\n')
    run = start(client, newer, regenerates_id=draft['id'], expected_revision=2)
    with client.stream('GET', f"/api/goal-draft-runs/{run['run_id']}/events") as response:
        assert response.status_code == 200
        events = [e for e in frames(response) if e]
    assert events[-1]['type'] == 'goal.completed'
    regenerated = wait_status(client, run, 'completed')['result']['draft']
    assert regenerated['regenerates_id'] == draft['id'] and regenerated['suggestions']
    assert client.get('/api/goals').json() == tree


def test_cancel_at_final_write_rolls_back(live, monkeypatch):
    app, client, actor, version, provider = live
    entered, release = threading.Event(), threading.Event()
    original = SQLiteResearchRepository.add
    def paused(repository, table, row):
        value = original(repository, table, row)
        if table == 'goal_drafts':
            entered.set()
            assert release.wait(HOOK_WAIT_SECONDS)
        return value
    monkeypatch.setattr(SQLiteResearchRepository, 'add', paused)
    run = start(client, version)
    provider.release.set()
    assert entered.wait(WAIT_SECONDS)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, f"/api/goal-draft-runs/{run['run_id']}/cancel")
        # Authentication itself uses SQLite BEGIN IMMEDIATE, so pause-free
        # cancellation must exercise the application's guard directly here.
        manager = app.state.container.goal_runs
        manager._runs[run['run_id']].guard.cancel()
        release.set()
        response = future.result(WAIT_SECONDS)
    assert response.status_code == 200
    assert app.state.service.list_goal_drafts(actor) == []
