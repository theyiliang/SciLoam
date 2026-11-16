"""Regression for synthetic evaluation: section scope and literal time labels."""
from copy import deepcopy

import pytest

from app.domain import goal_drafts as rules
from app.domain.errors import DomainError
from tests.test_sciloam_goal_drafts import world, add_source


@pytest.mark.parametrize('label', ['时间要求', '时间', '截止时间', '截止日期'])
@pytest.mark.parametrize('value,date', [('启动后第七个月内', None), ('2027-06-01前完成', '2027-06-01')])
def test_time_labels_preserve_original_and_only_explicit_dates(label, value, date):
    content = f'# 研究目标\r\n{label}：{value}\r\n'
    nodes = rules.extract_nodes([{'id': 'v1', 'content': content}])
    node = nodes[0]
    assert node['time_requirement'] == value
    assert node['due_date'] == date
    for field in ('time_requirement', 'due_date'):
        for citation in node['field_sources'].get(field, []):
            assert content[citation['start']:citation['end']] == citation['quote'] == node[field]


def test_explicit_goal_scope_excludes_unrelated_sections_and_nested_suggestions():
    content = ('# 课题说明\n## 问题缘起\n- 这不是交付目标\n'
               '## 总目标\n交付物：研究论文\n'
               '### 数据核对\n时间要求：启动后四个月内\n'
               '### 备选建议\n交付物：不要串入论文\n- 不应发布的建议\n'
               '### 结果复核\n约束：保留证据\n'
               '## 其他说明\n交付物：不属于结果复核\n'
               '## 子目标二：测度体系\n时间要求：启动后七个月内\n')
    nodes = rules.extract_nodes([{'id': 'v1', 'content': content}])
    assert [n['title'] for n in nodes] == ['总目标', '数据核对', '结果复核', '子目标二：测度体系']
    assert nodes[1]['parent_id'] == nodes[2]['parent_id'] == nodes[0]['id']
    assert nodes[3]['parent_id'] is None
    assert nodes[0]['deliverables'] == '研究论文'
    assert nodes[1]['deliverables'] == nodes[2]['deliverables'] == ''
    assert nodes[1]['time_requirement'] == '启动后四个月内'
    for n in nodes:
        for citations in n['field_sources'].values():
            for c in citations:
                assert content[c['start']:c['end']] == c['quote']


def test_background_does_not_activate_nested_goal_heading():
    content = '# 研究背景\n## 研究目标\n交付物：背景引用的他人要求\n# 总目标\n交付物：论文\n'
    nodes = rules.extract_nodes([{'id': 'v1', 'content': content}])
    assert [n['title'] for n in nodes] == ['总目标']
    assert nodes[0]['deliverables'] == '论文'


def test_fenced_goal_heading_does_not_activate_scoping_or_fields():
    content = '# 自定义工作\n```md\n# 总目标\n时间要求：不应提取\n```\n- 数据核对\n'
    nodes = rules.extract_nodes([{'id': 'v1', 'content': content}])
    assert [n['title'] for n in nodes] == ['自定义工作', '数据核对']
    assert all(not n['time_requirement'] for n in nodes)


def test_fallback_still_excludes_background_subtree_without_leaking_fields():
    content = '# 自定义工作\n交付物：论文\n## 研究背景\n### 旧研究\n时间要求：2027-01-01\n## 核对结果\n'
    nodes = rules.extract_nodes([{'id': 'v1', 'content': content}])
    assert [n['title'] for n in nodes] == ['自定义工作', '核对结果']
    assert not any(n['time_requirement'] or n['due_date'] for n in nodes)


def test_only_background_and_suggestions_reject_instead_of_inventing_goals():
    with pytest.raises(DomainError):
        rules.extract_nodes([{'id': 'v1', 'content': '# 研究背景\n## 备选建议\n- 可能做一个页面\n'}])


def test_time_revision_is_proposed_not_automatically_applied(world):
    service, repo, _ = world
    repo.update('versions', 'v1', {'content': '# 子目标二：测度体系\n时间要求：启动后第七个月内\n'})
    draft = service.generate_goal_draft('owner', ['v1'])
    service.publish_goal_draft('owner', draft['id'], 1, confirmed=True)
    before = deepcopy(repo.list('goal_nodes'))
    add_source(repo, 'v2', '# 子目标二：测度体系\n时间要求：启动后第八个月内\n', document_id='doc-v1')
    revised = service.regenerate_goal_draft('owner', draft['id'], ['v2'])
    assert repo.list('goal_nodes') == before
    suggestion = next(s for s in revised['suggestions'] if s['operation'] == 'modify')
    assert suggestion['node']['time_requirement'] == '启动后第八个月内'
    assert suggestion['node']['due_date'] is None
    assert before[0]['time_requirement'] == '启动后第七个月内'
