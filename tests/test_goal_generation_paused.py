"""Product subtraction regressions: configured key never enables AI goal calls."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app


@pytest.fixture
def product(tmp_path, monkeypatch):
    for key, value in {
        'LLM_API_KEY': 'configured-but-must-not-be-used',
        'RESEARCH_ENV': 'development', 'RESEARCH_COOKIE_SECURE': '0',
        'RESEARCH_ALLOWED_HOSTS': 'testserver,localhost,127.0.0.1',
        'RESEARCH_RETRIEVAL_MODE': 'local', 'RESEARCH_RAG_ALLOW_EXTERNAL': '0',
    }.items():
        monkeypatch.setenv(key, value)

    calls = []

    def forbidden(*args, **kwargs):
        calls.append('external-or-goal-ai')
        raise AssertionError('Product goal management must not call external services or AI adapters')

    from app.infrastructure.ai.goal_generator import ControlledGoalGenerator
    monkeypatch.setattr(ControlledGoalGenerator, 'generate', forbidden)
    # Local TestClient requests use handle_request, not these network transports.
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    app = create_app(tmp_path / 'paused.sqlite', tmp_path / 'frontend')
    with TestClient(app) as client:
        service = app.state.service
        assert service._goal_generator is None
        assert service._allow_experimental_goal_generator is False
        # Goal pause must not disable the configured independent QA runtime.
        assert app.state.container.assistant.runtime is not None
        actor = service.bootstrap('owner@example.org', 'goal-paused-password-123', 'Paused goals')['user']['id']
        csrf = client.get('/api/auth/me').json()['csrf_token']
        assert client.post('/api/auth/login', headers={'X-CSRF-Token': csrf}, json={
            'email': 'owner@example.org', 'password': 'goal-paused-password-123'}).status_code == 200
        client.headers['X-CSRF-Token'] = client.get('/api/auth/me').json()['csrf_token']
        basis = next(s for s in service.list_spaces(actor) if s['kind'] == 'basis')['id']
        version = service.add_document(actor, basis, 'Original text', '# Original research target\n1. Validate samples\n')['latest_version']
        yield app, client, actor, version, calls
        assert calls == []


def test_key_and_accidental_injection_cannot_enable_sync_regeneration_or_sse(product):
    app, client, actor, version, calls = product
    service = app.state.service

    class ForbiddenGenerator:
        def generate(self, *args, **kwargs):
            calls.append('injected-generator')
            raise AssertionError('Accidentally injected generator was called')

    service._goal_generator = ForbiddenGenerator()
    # Manual management is the primary supported goal workflow.
    manual = client.post('/api/goals', json={'title': 'Human goal', 'acceptance_criteria': 'Human review'})
    assert manual.status_code == 200, manual.text
    assert client.patch('/api/goals/' + manual.json()['id'], json={
        'expected_revision': 1, 'description': 'Human edited'}).status_code == 200

    response = client.post('/api/goal-drafts', json={'version_ids': [version['id']], 'method': 'rule'})
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['generation']['method'] == 'rule'
    assert all(n['generation_method'] == 'rule' for n in draft['nodes'])
    assert client.post(f"/api/goal-drafts/{draft['id']}/publish", json={
        'expected_revision': 1, 'confirmed': False}).status_code == 422
    published = client.post(f"/api/goal-drafts/{draft['id']}/publish", json={
        'expected_revision': 1, 'confirmed': True})
    assert published.status_code == 200, published.text
    tree = client.get('/api/goals').json()
    newer = service.add_version(actor, version['document_id'], '# Revised research target\n')
    regenerated = client.post(f"/api/goal-drafts/{draft['id']}/regenerate", json={
        'version_ids': [newer['id']], 'expected_revision': 2, 'method': 'rule'})
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()['generation']['method'] == 'rule'
    assert regenerated.json()['suggestions']
    assert client.get('/api/goals').json() == tree

    # The omitted method remains backward compatible but always local in product.
    started = client.post('/api/goal-draft-runs', json={'version_ids': [newer['id']], 'request_id': 'local-run'})
    assert started.status_code == 200, started.text
    run_id = started.json()['run_id']
    with client.stream('GET', f'/api/goal-draft-runs/{run_id}/events') as response:
        events = [json.loads(line[6:]) for line in response.iter_lines() if line.startswith('data: ')]
    assert events[-1]['type'] == 'goal.completed'
    assert next(e for e in events if e['type'] == 'generation.started')['data']['method'] == 'rule'
    result = client.get(f'/api/goal-draft-runs/{run_id}').json()['result']['draft']
    assert result['generation']['method'] == 'rule'
    assert client.get('/api/goals').json() == tree
    assert len(client.get('/api/goal-drafts').json()) == 3


@pytest.mark.parametrize('path', ['/api/goal-drafts', '/api/goal-draft-runs', '/api/goal-drafts/old-draft/regenerate'])
def test_api_explicitly_rejects_ai_without_fallback_or_write(product, path):
    app, client, actor, version, calls = product
    response = client.post(path, json={'version_ids': [version['id']], 'method': 'ai'})
    assert response.status_code == 422, response.text
    assert 'AI goal generation/regeneration is paused' in response.text
    assert app.state.service.list_goal_drafts(actor) == []
    assert client.get('/api/goals').json() == []
    assert app.state.container.goal_runs._runs == {}


def test_historical_ai_snapshots_remain_ai_and_readable_with_current_key(product):
    app, client, actor, version, calls = product
    service = app.state.service
    draft = service.generate_goal_draft(actor, [version['id']])
    # Seed a historical record directly; do not execute any AI, even in this test.
    from copy import deepcopy
    historical = deepcopy(draft)
    historical.pop('publication_readiness')
    historical.update(id='historical-ai-draft', method='ai',
        generation={'method': 'ai', 'model': 'historical-experiment'})
    for node in historical['nodes']:
        node['generation_method'] = 'ai'
    with service._uow_factory() as uow:
        uow.repository.add('goal_drafts', historical)
        uow.repository.add('goal_generations', {'draft_id': historical['id'],
            'snapshot': deepcopy(historical), 'created_at': historical['created_at']})
    history = client.get('/api/goal-drafts').json()
    assert len(history) == 2
    assert next(row for row in history if row['id'] == historical['id'])['generation']['method'] == 'ai'
    detail = client.get('/api/goal-drafts/' + historical['id'])
    assert detail.status_code == 200
    assert detail.json()['method'] == 'ai'
    assert detail.json()['nodes'][0]['generation_method'] == 'ai'
    assert detail.json()['nodes'][0]['source'] == draft['nodes'][0]['source']
    # Stored history is not a resumable task; unknown pre-restart run IDs stay 404.
    assert client.get('/api/goal-draft-runs/historical-unfinished-ai-run').status_code == 404
    assert client.get('/api/goal-draft-runs/historical-unfinished-ai-run/events').status_code == 404
    publication = client.post(f"/api/goal-drafts/{historical['id']}/publish", json={
        'expected_revision': 1, 'confirmed': True})
    assert publication.status_code == 200, publication.text
    goal = client.get('/api/goals/' + publication.json()['goal_ids'][0]).json()
    assert goal['generation_sources'][0]['generation']['method'] == 'ai'
    # Explicit new extraction from an old AI draft is local rule work, not AI resume.
    regenerated = client.post(f"/api/goal-drafts/{historical['id']}/regenerate", json={
        'version_ids': [version['id']], 'expected_revision': 2, 'method': 'rule'})
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()['generation']['method'] == 'rule'
    assert all(node['generation_method'] == 'rule' for node in regenerated.json()['nodes'])
