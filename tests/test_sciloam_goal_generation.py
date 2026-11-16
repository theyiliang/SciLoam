"""Evidence, controlled generation, differences and durable receipts."""
from copy import deepcopy
from contextlib import closing
import json
import sqlite3

import httpx
import pytest

from app.domain import goal_drafts as rules
from app.domain.errors import DomainError
from app.infrastructure.ai.goal_generator import ControlledGoalGenerator, build_goal_generator
from app.infrastructure.ai.model_gateway import ResearchAISettings
from app.infrastructure.persistence.database import Database, SCHEMA_6, SCHEMA_VERSION, _execute_statements, _migrate_schema_5
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from tests.test_sciloam_goal_drafts import world, add_source
from tests.test_research_domain import denied
from tests.test_sciloam_schema5 import _schema_four, _snapshot
from tests.test_sciloam_goals_api import world as http_world


def test_field_level_requirements_are_literal_and_no_metrics_are_invented(world):
    service, repo, _ = world
    content = '# 研究总目标\n研究目标：研究样本稳定性\n交付物：实验报告与数据集\n时间：立项后六个月内\n约束：仅使用脱敏样本\n- 样本收集\n截止日期：2027-02-01\n验收标准：不少于三批原文指定样本\n'
    repo.update('versions', 'v1', {'content': content})
    draft = service.generate_goal_draft('owner', ['v1'])
    root, child = draft['nodes']
    assert root['description'] == '研究样本稳定性'
    assert root['deliverables'] == '实验报告与数据集'
    assert root['time_requirement'] == '立项后六个月内'
    assert root['constraints'] == '仅使用脱敏样本'
    assert root['due_date'] is None and root['acceptance_criteria'] == ''
    assert child['due_date'] == child['time_requirement'] == '2027-02-01'
    assert child['parent_id'] == root['id']
    for node in draft['nodes']:
        assert node['origin'] == 'explicit' and node['generation_method'] == 'rule'
        for field in rules.FIELDS:
            if node[field]:
                assert node[field] == '\n'.join(a['quote'] for a in node['field_sources'][field])
                for a in node['field_sources'][field]:
                    assert content[a['start']:a['end']] == a['quote']
    published = service.publish_goal_draft('owner', draft['id'], 1, confirmed=True)
    goal = service.get_goal('owner', published['goal_ids'][0])
    assert goal['time_requirement'] == root['time_requirement']
    receipt = goal['generation_sources'][0]
    assert receipt['node'] == root
    assert receipt['confirmation']['nodes_hash'] == rules.canonical_hash(draft['nodes'])
    assert receipt['confirmation']['sources_hash'] == draft['sources_hash']


def test_schema6_node_defaults_do_not_falsely_block_readiness(world):
    service, repo, _ = world
    draft = service.generate_goal_draft('owner', ['v1'])
    legacy = deepcopy(repo.get('goal_drafts', draft['id']))
    for node in legacy['nodes']:
        for field in ('field_sources', 'generation_method', 'deliverables', 'constraints', 'time_requirement'):
            node.pop(field, None)
    legacy['generation'] = {}
    repo.update('goal_drafts', draft['id'], {'nodes': legacy['nodes'], 'generation': {}})
    view = service.get_goal_draft('owner', draft['id'])
    assert view['publication_readiness']['publishable']
    assert view['generation'] == {'method': 'rule'}
    assert view['nodes'][0]['field_sources']['title'] == [view['nodes'][0]['source']]


def test_manual_unsourced_nodes_and_invented_metrics_remain_drafts(world):
    service, repo, _ = world
    draft = service.generate_goal_draft('owner', ['v1'])
    nodes = deepcopy(draft['nodes'])
    nodes.append({'id': 'manual', 'title': '人工补充未提供依据', 'origin': 'suggested'})
    edited = service.update_goal_draft('owner', draft['id'], 1, nodes)
    assert not edited['publication_readiness']['publishable']
    assert edited['nodes'][-1]['generation_method'] == 'manual'
    denied(422, service.publish_goal_draft, 'owner', draft['id'], 2, confirmed=True)
    assert repo.list('goal_nodes') == []
    fresh = service.generate_goal_draft('owner', ['v1'])
    nodes = deepcopy(fresh['nodes'])
    nodes[0].update(origin='suggested', acceptance_criteria='准确率99%')
    nodes[0]['field_sources']['acceptance_criteria'] = [deepcopy(nodes[0]['source'])]
    edited = service.update_goal_draft('owner', fresh['id'], 1, nodes)
    assert not edited['publication_readiness']['publishable']
    denied(422, service.publish_goal_draft, 'owner', fresh['id'], 2, confirmed=True)


def test_one_stale_draft_never_blocks_other_history_and_withdrawal_redacts(world):
    service, repo, _ = world
    stale = service.generate_goal_draft('owner', ['v1'])
    add_source(repo, 'v2', '# 新目标', document_id='doc-v1')
    fresh = service.generate_goal_draft('owner', ['v2'])
    rows = {r['id']: r for r in service.list_goal_drafts('owner')}
    assert not rows[stale['id']]['publication_readiness']['publishable']
    assert rows[stale['id']]['nodes']
    assert rows[fresh['id']]['publication_readiness']['publishable']
    published = service.publish_goal_draft('owner', fresh['id'], 1, confirmed=True)
    repo.add('document_withdrawals', {'document_id': 'doc-v1'})
    rows = service.list_goal_drafts('owner')
    assert all(r['source_details_redacted'] and not r['nodes'] for r in rows)
    receipt = service.get_goal('owner', published['goal_ids'][0])['generation_sources'][0]
    assert receipt['source_details_redacted'] and receipt['node'] == {'id': fresh['nodes'][0]['id']}
    assert receipt['confirmation']['confirmed'] is True


def test_regeneration_proposes_modify_move_add_retire_without_mutating_tree(world):
    service, repo, _ = world
    repo.update('versions', 'v1', {'content': '# 根目标\n## 保留子目标\n## 退役子目标'})
    first = service.generate_goal_draft('owner', ['v1'])
    publication = service.publish_goal_draft('owner', first['id'], 1, confirmed=True)
    add_source(repo, 'v2', '# 根目标\n交付物：原文实验报告\n## 新增子目标\n# 保留子目标', document_id='doc-v1')
    before = deepcopy(repo.list('goal_nodes'))
    draft = service.regenerate_goal_draft('owner', first['id'], ['v2'], expected_revision=2)
    assert repo.list('goal_nodes') == before
    operations = {s['operation'] for s in draft['suggestions']}
    assert {'add', 'modify', 'move', 'retire'} <= operations
    denied(409, service.publish_goal_draft, 'owner', draft['id'], 1, confirmed=True)
    add = next(s for s in draft['suggestions'] if s['operation'] == 'add')
    denied(422, service.adopt_goal_draft_suggestion, 'owner', draft['id'], add['id'], 1)
    draft = service.adopt_goal_draft_suggestion('owner', draft['id'], add['id'], 1, confirmed=True)
    assert len(repo.list('goal_nodes')) == len(before) + 1
    denied(409, service.adopt_goal_draft_suggestion, 'owner', draft['id'], add['id'], 1, confirmed=True)
    modify = next(s for s in draft['suggestions'] if s['operation'] == 'modify')
    denied(409, service.adopt_goal_draft_suggestion, 'owner', draft['id'], modify['id'], 2,
        expected_goal_revision=999, confirmed=True)
    draft = service.adopt_goal_draft_suggestion('owner', draft['id'], modify['id'], 2,
        expected_goal_revision=modify['expected_goal_revision'], confirmed=True)
    assert repo.get('goal_nodes', modify['target_goal_id'])['deliverables'] == '原文实验报告'
    retire = next(s for s in draft['suggestions'] if s['operation'] == 'retire')
    retired_id = retire['target_goal_id']
    draft = service.adopt_goal_draft_suggestion('owner', draft['id'], retire['id'], draft['revision'],
        expected_goal_revision=retire['expected_goal_revision'], confirmed=True)
    assert repo.get('goal_nodes', retired_id)['archived_at'] is not None
    assert repo.get('goal_nodes', retired_id)['title'] == '退役子目标'
    assert len(repo.list('goal_nodes')) == len(before) + 1
    assert publication['node_id_mapping']


def test_reject_abandon_and_goal_conflict_never_change_formal_tree(world):
    service, repo, _ = world
    first = service.generate_goal_draft('owner', ['v1'])
    service.publish_goal_draft('owner', first['id'], 1, confirmed=True)
    add_source(repo, 'v2', '# 研究总目标\n交付物：报告', document_id='doc-v1')
    draft = service.regenerate_goal_draft('owner', first['id'], ['v2'])
    proposal = next(s for s in draft['suggestions'] if s['operation'] == 'modify')
    goal = repo.get('goal_nodes', proposal['target_goal_id'])
    service.update_goal('owner', goal['id'], goal['revision'], description='人工要求保留')
    before = deepcopy(repo.list('goal_nodes'))
    denied(409, service.adopt_goal_draft_suggestion, 'owner', draft['id'], proposal['id'], 1,
        expected_goal_revision=proposal['expected_goal_revision'], confirmed=True)
    rejected = service.reject_goal_draft_suggestion('owner', draft['id'], proposal['id'], 1,
        reason='不采纳', confirmed=True)
    assert next(s for s in rejected['suggestions'] if s['id'] == proposal['id'])['status'] == 'rejected'
    abandoned = service.abandon_goal_draft('owner', draft['id'], 2, confirmed=True)
    assert abandoned['status'] == 'abandoned'
    assert repo.list('goal_nodes') == before
    assert repo.get('goal_generations', draft['id'])['snapshot']['suggestions'][0]['status'] == 'pending'
    denied(409, service.publish_goal_draft, 'owner', draft['id'], 3, confirmed=True)
    denied(409, service.abandon_goal_draft, 'owner', first['id'], 2, confirmed=True)


def test_independent_fake_generator_is_ai_not_rule_and_must_have_every_anchor(world):
    service, repo, _ = world
    calls = []
    class Fake:
        def generate(self, versions, *, check):
            check()
            calls.append(versions)
            return {'nodes': rules.extract_nodes(versions), 'generation': {'method': 'ai', 'model': 'fake-qwen'}}
    service._allow_experimental_goal_generator = True  # Explicit offline experiment, not product behavior.
    service._goal_generator = Fake()
    draft = service.generate_goal_draft('owner', ['v1'])
    assert draft['method'] == 'ai' and draft['generation']['method'] == 'ai'
    assert all(n['origin'] == 'explicit' and n['generation_method'] == 'ai' for n in draft['nodes'])
    assert len(calls) == 1 and repo.list('goal_nodes') == []
    class Bad(Fake):
        def generate(self, versions, *, check):
            result = super().generate(versions, check=check)
            result['nodes'][0].update(origin='suggested', source=None)
            return result
    service._goal_generator = Bad()
    denied(422, service.generate_goal_draft, 'owner', ['v1'])
    assert len(repo.list('goal_drafts')) == 1


def test_qwen_adapter_uses_only_configured_endpoint_and_budget_no_real_network():
    requests = []
    version = {'id': 'basis-v', 'content': '# 研究目标'}
    nodes = rules.extract_nodes([version])
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'nodes': nodes})}}]})
    settings = ResearchAISettings(llm_api_key='test-key', llm_base_url='https://qwen.invalid/v1',
        llm_model='qwen-test', llm_min_interval_seconds=0)
    checks = []
    adapter = ControlledGoalGenerator(settings, transport=httpx.MockTransport(handler))
    result = adapter.generate([version], check=lambda: checks.append(True))
    assert result['generation'] == {'method': 'ai', 'provider': 'qwen-compatible', 'model': 'qwen-test'}
    payload = json.loads(requests[0].content)
    assert payload['model'] == 'qwen-test' and 'tools' not in payload
    assert str(requests[0].url) == 'https://qwen.invalid/v1/chat/completions'
    assert json.loads(payload['messages'][1]['content']) == [{'version_id': 'basis-v', 'content': '# 研究目标'}]
    assert len(checks) >= 2
    assert build_goal_generator(ResearchAISettings()) is None
    tiny = ControlledGoalGenerator(ResearchAISettings(llm_api_key='x', total_input_chars=1), transport=httpx.MockTransport(handler))
    denied(502, tiny.generate, [version], check=lambda: None)
    assert len(requests) == 1


def test_http_difference_adoption_rejection_abandon_and_owner_boundary(http_world):
    owner, member, write = http_world
    basis = next(s for s in owner.get('/api/spaces').json() if s['kind'] == 'basis')
    document = write(owner, f"/api/spaces/{basis['id']}/documents", {'title': '依据', 'content': '# 根目标\n## 子目标'}).json()
    version = document['latest_version']['id']
    assert write(member, '/api/goal-drafts', {'version_ids': [version]}).status_code == 403
    draft = write(owner, '/api/goal-drafts', {'version_ids': [version]}).json()
    publication = write(owner, f"/api/goal-drafts/{draft['id']}/publish", {'expected_revision': 1, 'confirmed': True})
    assert publication.status_code == 200, publication.text
    second = write(owner, f"/api/documents/{document['id']}/versions", {'content': '# 根目标\n交付物：报告', 'filename': 'v2.md'})
    assert second.status_code == 200, second.text
    version2 = second.json()['id']
    history = owner.get('/api/goal-drafts').json()
    assert history[0]['publication_readiness']['publishable'] is False
    regenerated = write(owner, f"/api/goal-drafts/{draft['id']}/regenerate", {'version_ids': [version2], 'expected_revision': 2})
    assert regenerated.status_code == 200, regenerated.text
    regen = regenerated.json()
    proposal = next(s for s in regen['suggestions'] if s['operation'] == 'modify')
    assert write(member, f"/api/goal-drafts/{regen['id']}/adopt", {'suggestion_id': proposal['id'], 'expected_revision': 1,
        'expected_goal_revision': proposal['expected_goal_revision'], 'confirmed': True}).status_code == 403
    adopted = write(owner, f"/api/goal-drafts/{regen['id']}/adopt", {'suggestion_id': proposal['id'], 'expected_revision': 1,
        'expected_goal_revision': proposal['expected_goal_revision'], 'confirmed': True})
    assert adopted.status_code == 200, adopted.text
    retire = next(s for s in adopted.json()['suggestions'] if s['operation'] == 'retire')
    rejected = write(owner, f"/api/goal-drafts/{regen['id']}/reject", {'suggestion_id': retire['id'], 'expected_revision': 2, 'confirmed': True})
    assert rejected.status_code == 200, rejected.text
    abandoned = write(owner, f"/api/goal-drafts/{regen['id']}/abandon", {'expected_revision': 3, 'confirmed': True})
    assert abandoned.status_code == 200 and abandoned.json()['status'] == 'abandoned'
    assert len(owner.get('/api/goals').json()) == 2
    goal = owner.get(f"/api/goals/{proposal['target_goal_id']}").json()
    assert goal['deliverables'] == '报告' and goal['generation_sources'][-1]['confirmation']['confirmed']
    changed = write(owner, f"/api/goals/{goal['id']}", {'expected_revision': goal['revision'], 'deliverables': '人工新要求'}, 'PATCH')
    assert changed.status_code == 200
    assert changed.json()['generation_sources'][-1]['current_differs_from_confirmation'] is True


def test_real_schema6_to7_keeps_all_historical_columns_and_freezes_new_evidence(tmp_path):
    path = tmp_path / 'real-v6.db'
    _schema_four(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        _migrate_schema_5(conn)
        _execute_statements(conn, SCHEMA_6)
        conn.execute('PRAGMA user_version=6')
        before = _snapshot(conn)
    database = Database(path)
    with database.transaction() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] >= 7  # SCHEMA_VERSION may have advanced
        for table, (columns, rows) in before.items():
            assert [tuple(r) for r in conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1")] == rows
        repo = SQLiteResearchRepository(conn)
        record = {'version_id': 'unlinked-version', 'report': {'parser_version': 1}, 'structure_map': {'blocks': []}}
        repo.add('knowledge_imports', record)
        assert repo.get('knowledge_imports', 'unlinked-version')['report'] == record['report']
        for sql in ("UPDATE knowledge_imports SET report='{}'", 'DELETE FROM knowledge_imports',
                    "INSERT OR REPLACE INTO knowledge_imports VALUES ('unlinked-version','{}','{}',0)"):
            with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                conn.execute(sql)
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
    Database(path)
    with closing(database.connect()) as conn:
        assert SQLiteResearchRepository(conn).get('knowledge_imports', 'unlinked-version')['structure_map'] == {'blocks': []}


def test_real_sqlite_generated_receipts_are_immutable_after_restart(tmp_path):
    from app.infrastructure.persistence.factory import build_research_service
    from tests.test_research_service import PASSWORD, space_of
    path = tmp_path / 'generation.db'
    service = build_research_service(path)
    owner = service.bootstrap('owner@example.org', PASSWORD, 'Research')['user']['id']
    basis = space_of(service, owner, 'basis')
    version = service.add_document(owner, basis['id'], '依据', '# 目标\n交付物：报告')['latest_version']['id']
    draft = service.generate_goal_draft(owner, [version])
    result = service.publish_goal_draft(owner, draft['id'], 1, confirmed=True)
    restarted = build_research_service(path)
    receipt = restarted.get_goal(owner, result['goal_ids'][0])['generation_sources'][0]
    assert receipt['node']['deliverables'] == '报告'
    with service.db.transaction() as conn:
        for table, key, value in [('goal_generations', 'draft_id', draft['id']),
                                  ('goal_generation_sources', 'id', receipt['id'])]:
            for sql in (f'UPDATE {table} SET {key}={key} WHERE {key}=?', f'DELETE FROM {table} WHERE {key}=?'):
                with pytest.raises(sqlite3.IntegrityError, match='immutable'):
                    conn.execute(sql, (value,))
        repo = SQLiteResearchRepository(conn)
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            repo.update('goal_drafts', draft['id'], {'generation': {'method': 'manual'}})
