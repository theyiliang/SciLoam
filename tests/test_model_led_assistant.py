"""Single model-led path contracts, using real storage and offline runtime."""
import json
import secrets

import pytest

from app.application.agents.orchestrator import ResearchAssistant
from app.application.agents.main_request import MainRequest
from app.domain.errors import DomainError
from app.infrastructure.persistence.factory import build_research_service


@pytest.fixture
def library(tmp_path):
    service = build_research_service(tmp_path / 'research.db')
    actor = service.bootstrap('owner@example.org', secrets.token_urlsafe(24), 'Synthetic')['user']['id']
    space = next(s['id'] for s in service.list_spaces(actor) if s['kind'] == 'basis')
    conv = service.create_conversation(actor, space)['id']
    return service, actor, space, conv


@pytest.mark.parametrize('reason', ['stream_tool_limit', 'stream_mixed_tools_and_text', 'stream_missing_done'])
async def test_failure_logs_safe_reason_even_outside_gateway(library, caplog, reason):
    from app.infrastructure.ai.agentscope_runtime import ProviderStreamError
    service, actor, space, conv = library
    class FailedRuntime:
        def new_request_state(self):
            return object()
        async def run_coordinator(self, *args, **kwargs):
            raise ProviderStreamError(reason)
    result = await ResearchAssistant(service, FailedRuntime()).answer(actor, space, conv, '普通问题')
    assert result['fallback'] is True
    assert 'research.answer_failed' in caplog.text and f'reason={reason}' in caplog.text
    assert '普通问题' not in caplog.text
    assert '模型服务请求或流式响应未正常完成' not in result['answer']


async def test_failure_does_not_log_untrusted_exception_text(library, caplog):
    service, actor, space, conv = library
    class FailedRuntime:
        def new_request_state(self):
            raise ValueError('PRIVATE_SECRET_SENTINEL')
    result = await ResearchAssistant(service, FailedRuntime()).answer(actor, space, conv, '普通问题')
    assert result['fallback']
    assert 'PRIVATE_SECRET_SENTINEL' not in caplog.text + result['answer']
    assert 'error_type=ValueError reason=unclassified' in caplog.text


async def test_english_document_question_gets_chinese_template_and_action(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        return {'answer_status': 'partial', 'clarification_kind': 'document',
                'claims': [claim('No document is currently selected. Please specify which document you want to know about.', 'clarification')]}
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '这个文档说了啥')
    assert not result['fallback'] and result['clarification_kind'] == 'document'
    assert '你想了解哪份材料' in result['answer'] and 'No document' not in result['answer']
    assert '【需要确认】' not in result['answer'] and result['suggested_actions'] == ['select_material']
    saved = service.get_conversation(actor, conv)['messages'][-1]
    assert saved['clarification_kind'] == 'document' and saved['content'] == result['answer']


async def test_english_explanation_is_not_published_or_retried(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        return response(claim('This is a completely English explanatory answer for the user.'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '解释概念')
    assert result['answer_kind'] == 'failure' and result['fallback'] and runtime.calls == 1
    assert '中文' in result['answer'] and 'completely English' not in result['answer']


async def test_english_source_quote_can_be_preserved_with_chinese_explanation(library):
    service, actor, space, conv = library
    original = 'Downloads do not prove actual use in a business scenario.'
    key = service.add_document(actor, space, '英文材料', original)['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        return response(material(item, '资料指出下载不能直接证明实际应用。原文：' + original))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '总结材料', selected_version_id=key)
    assert not result['fallback'] and original in result['answer']


@pytest.mark.parametrize('reason,phrase', [
    ('model_first_response_timeout', '暂未响应'), ('model_stream_timeout', '生成中断'),
    ('pipeline_timeout', '时间上限'), ('model_final_timeout', '最终回答生成超时'),
    ('model_request_timeout', '单轮时间限制'), ('tool_timeout', '读取或检索超时'),
])
async def test_timeout_reason_is_saved_as_failure_not_clarification(library, reason, phrase):
    from app.domain.ai_failures import ModelTimeout
    service, actor, space, conv = library
    class TimeoutRuntime:
        def new_request_state(self):
            return object()
        async def run_coordinator(self, *args, **kwargs):
            raise ModelTimeout(reason)
    result = await ResearchAssistant(service, TimeoutRuntime()).answer(actor, space, conv, '普通问题')
    assert result['answer_kind'] == 'failure' and phrase in result['answer']
    assert 'clarification_kind' not in result
    assert '资料已读取' not in result['answer']
    assert service.get_conversation(actor, conv)['messages'][-1]['answer_kind'] == 'failure'
    assert any(event.get('reason') == reason for event in result['events'])


class Runtime:
    def __init__(self, action):
        self.action = action
        self.calls = 0

    def new_request_state(self):
        return object()

    async def run_coordinator(self, agent, payload, **options):
        self.calls += 1
        await options['check']()
        tools = {tool.name: tool.invoke for tool in agent.tools}
        return json.dumps(await self.action(tools, payload), ensure_ascii=False)

    async def run_specialist(self, *args, **kwargs):
        pytest.fail('No fixed specialist stage')


def response(*claims):
    return {'answer_status': 'sufficient', 'claims': list(claims)}


def claim(text, kind='general', **extra):
    return {'source_kind': kind, 'text': text, **extra}


def material(item, text=None):
    return claim(text or item['content'], 'material', version_ids=[item['version_id']],
                 evidence_quotes=[{'evidence_id': item['evidence_id'],
                                   'version_id': item['version_id'], 'text': item['content']}])


@pytest.mark.parametrize('question', ['普通概念是什么', '本课题要发表多少论文', '这个文档说了啥', '完全陌生口语嘞'])
async def test_every_question_reaches_main_model_without_local_gate(library, question, monkeypatch):
    service, actor, space, conv = library
    monkeypatch.setattr(service, 'search', lambda *a, **k: pytest.fail('No forced retrieval'))
    async def action(tools, payload):
        assert set(tools) == {'list_documents', 'read_document', 'search_documents', 'list_tasks'}
        assert payload['question'] == question
        return response(claim('请明确你想讨论的范围。', 'clarification'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, question)
    assert runtime.calls == 1 and not result['fallback']
    assert result['answer_kind'] == 'clarification'


async def test_selected_document_read_and_range_survive_history(library):
    service, actor, space, conv = library
    doc = service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。')
    key = doc['latest_version']['id']
    async def action(tools, payload):
        assert payload['selected_document']['version_id'] == key
        item = await tools['read_document'](key)
        assert item['read_start'] == 0 and not item['has_more']
        return response(material(item))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '说说这个文档', selected_version_id=key)
    assert not result['fallback']
    citation = result['citations'][0]
    history = service.get_conversation(actor, conv)['messages'][-1]
    assert history['content'] == result['answer']
    assert history['citations'][0]['excerpt'] == citation['excerpt']
    assert history['citations'][0]['chunk_start'] == citation['chunk_start']


async def test_invalid_material_does_not_discard_general_and_suggestions(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        return response(claim('通用解释。'), claim('伪造事实', 'material', version_ids=['fake']),
                        claim('可尝试先做访谈。', 'suggestion'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '结合材料给建议')
    assert '通用解释' in result['answer'] and '访谈' in result['answer']
    assert '伪造事实' not in result['answer'] and not result['citations']
    assert result['answer_status'] == 'partial' and not result['fallback']


async def test_material_backed_suggestion_is_mixed_not_general(library):
    service, actor, space, conv = library
    key = service.add_document(actor, space, '访谈记录', '参与者希望获得口径说明。')['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        suggestion = material(item, '建议在访谈提纲中加入指标口径理解问题。')
        suggestion['source_kind'] = 'suggestion'
        return response(suggestion)
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '结合材料给建议', selected_version_id=key)
    assert result['answer_kind'] == 'mixed' and len(result['citations']) == 1
    assert '建议／待验证' in result['answer']


async def test_multiple_documents_require_model_resolution_not_first(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '甲', '甲正文')
    service.add_document(actor, space, '乙', '乙正文')
    async def action(tools, payload):
        assert payload['selected_document'] is None
        docs = await tools['list_documents']()
        assert len(docs['documents']) == 2
        assert all('content' not in row for row in docs['documents'])
        return response(claim('请选择甲或乙文档。', 'clarification'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '这个说了啥')
    assert result['answer_kind'] == 'clarification' and not result['citations']


async def test_segmented_reads_preserve_each_fragment_and_partial_scope(library):
    service, actor, space, conv = library
    body = '甲' * 6000 + '乙' * 6000 + '丙' * 6000
    doc = service.add_document(actor, space, '长文', body)
    key = doc['latest_version']['id']
    async def action(tools, payload):
        a = await tools['read_document'](key, 0, 2000)
        b = await tools['read_document'](key, 6000, 2000)
        assert a['evidence_id'] != b['evidence_id']
        assert b['has_more'] and b['document_length'] == 18000
        return response(material(a, '开头是甲。'), material(b, '中段是乙。'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '全文概览', selected_version_id=key)
    assert len(result['citations']) == 2
    assert '实际读取4000字符' in result['answer'] and '不能视为全文总结' in result['answer']


async def test_read_budget_and_undiscovered_ids(library):
    request = MainRequest(*library)
    await request.initialize()
    with pytest.raises(ValueError):
        await request.read_document('invented')
    for _ in range(9):
        await request.list_documents()
    with pytest.raises(ValueError, match='budget'):
        await request.list_documents()


async def test_cumulative_body_budget_is_enforced(library):
    service, actor, space, conv = library
    key = service.add_document(actor, space, '长文预算', '甲' * 30000)['latest_version']['id']
    request = MainRequest(service, actor, space, conv, selected_version_id=key)
    await request.initialize()
    for offset in range(0, 24000, 6000):
        await request.read_document(key, offset, 6000)
    with pytest.raises(ValueError, match='budget'):
        await request.read_document(key, 24000, 1)
    assert request.read_chars == 24000 and len(request.evidence) == 4


async def test_selected_version_cannot_cross_library_boundary(library):
    service, actor, space, conv = library
    other = next(row['id'] for row in service.list_spaces(actor) if row['kind'] == 'private')
    key = service.add_document(actor, other, '私有材料', 'PRIVATE_SENTINEL')['latest_version']['id']
    async def action(*args):
        pytest.fail('Out of scope selection must fail before model')
    with pytest.raises(DomainError):
        await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '概览', selected_version_id=key)
    assert service.get_conversation(actor, conv)['messages'] == []


async def test_revocation_aborts_without_fallback_or_assistant_save(library):
    service, actor, space, conv = library
    allowed = True
    async def authorize():
        if not allowed:
            raise DomainError(403, 'Access revoked')
    async def action(tools, payload):
        nonlocal allowed
        allowed = False
        return response(claim('必须丢弃'))
    with pytest.raises(DomainError):
        await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '任何问题', authorization_check=authorize)
    assert [m['role'] for m in service.get_conversation(actor, conv)['messages']] == ['user']


async def test_ordinary_question_can_choose_public_goal_tool(library):
    service, actor, space, conv = library
    service.create_goal(actor, '状态核对')
    async def action(tools, payload):
        rows = await tools['list_tasks']()
        return response(claim('目标记录状态为未开始，不等于实际工作验收结果。', 'material', task_ids=[rows[0]['id']]))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '现在目标啥状态')
    assert not result['fallback'] and '目标记录' in result['answer']


async def test_progress_only_exposes_goal_metadata_tool(library):
    service, actor, space, conv = library
    service.create_goal(actor, '公开目标')
    async def action(tools, payload):
        assert set(tools) == {'list_tasks'} and payload['history'] == []
        rows = await tools['list_tasks']()
        assert rows and not any('description' in row or 'content' in row for row in rows)
        return response(claim('公开目标记录尚未开始，不代表验收结论。', 'material', task_ids=[rows[0]['id']]))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '进展', 'progress')
    assert not result['fallback'] and not result['citations']


async def test_malformed_block_type_only_drops_that_block(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        return response({'source_kind': [], 'text': 'BAD_BLOCK'}, claim('保留有效解释'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '普通问题')
    assert not result['fallback'] and '保留有效解释' in result['answer'] and 'BAD_BLOCK' not in result['answer']


async def test_search_returns_only_actual_authorized_passages(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '指标口径', '指标口径要求按唯一标识去重。')
    async def action(tools, payload):
        found = await tools['search_documents']('指标口径')
        item = found['evidence'][0]
        assert item['chunk_end'] - item['chunk_start'] == len(item['content'])
        return response(material(item))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '指标口径咋算')
    assert not result['fallback'] and len(result['citations']) == 1


async def test_stream_temporary_is_not_persisted_on_incomplete_json(library):
    service, actor, space, conv = library
    class Streaming(Runtime):
        async def stream_coordinator(self, agent, payload, **options):
            yield '{"claims": [{"source_kind":"general","text":"临时输出唯一标记TENTATIVE_SENTINEL_123"}],'
    events = []
    async def sink(event):
        events.append(event)
    result = await ResearchAssistant(service, Streaming(None)).answer(actor, space, conv, '普通问题', event_sink=sink)
    # Orchestrator no longer emits answer.delta; streaming still emits agent.started via event_sink.
    assert any(event['type'] == 'agent.started' for event in events)
    assert result['fallback'] and '临时输出唯一标记TENTATIVE_SENTINEL_123' not in result['answer']
    assert '临时输出唯一标记TENTATIVE_SENTINEL_123' not in service.get_conversation(actor, conv)['messages'][-1]['content']


@pytest.mark.parametrize('question', ['知识库里有什么内容', '当前知识库有哪些资料', '这个知识库讲了什么', '介绍一下知识库'])
async def test_space_overview_loads_real_catalog_and_representative_reads(library, question):
    service, actor, space, conv = library
    for index in range(1, 5):
        service.add_document(actor, space, f'课题资料{index}', f'课题资料{index}的正文片段。')
    async def action(tools, payload):
        overview = payload['space_overview']
        assert [row['title'] for row in overview['documents']] == [f'课题资料{index}' for index in range(1, 5)]
        assert overview['representative_reads'] == 3 and overview['possibly_more'] is False
        fragments = payload['revalidated_evidence']
        assert len(fragments) == 3 and all(item['retrieval_strategy'] == 'document_read' for item in fragments)
        assert all(item['chunk_start'] == 0 and item['content'] for item in fragments)
        return response(claim('当前知识库有4份资料：课题资料1至课题资料4。', 'general'),
                        material(fragments[0], '例如课题资料1的开头为：课题资料1的正文片段。'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, question)
    assert not result['fallback'] and '课题资料1' in result['answer'] and len(result['citations']) == 1


async def test_non_overview_question_does_not_inject_space_overview(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '实验记录', '本课题乳酸处理指标为0.95。')
    async def action(tools, payload):
        assert 'space_overview' not in payload and payload['revalidated_evidence'] == []
        return response(claim('请明确要核对的具体记录。', 'clarification'))
    result = await ResearchAssistant(service, Runtime(action)).answer(
        actor, space, conv, '根据知识库里的实验记录，本课题指标是什么')
    assert not result['fallback']


async def test_progress_mode_never_loads_space_overview(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '实验记录', '正文')
    async def action(tools, payload):
        assert 'space_overview' not in payload and set(tools) == {'list_tasks'}
        return response(claim('无目标记录。', 'general'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '知识库里有什么内容', 'progress')
    assert not result['fallback']


async def test_space_overview_empty_library_reports_empty_catalog(library):
    service, actor, space, conv = library
    async def action(tools, payload):
        assert payload['space_overview']['documents'] == [] and payload['space_overview']['representative_reads'] == 0
        return response(claim('当前知识库暂无有效文档，可先上传资料。', 'general'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '知识库里有什么内容')
    assert not result['fallback'] and '暂无有效文档' in result['answer']


@pytest.mark.parametrize('question,expected', [
    ('知识库里有什么内容', True), ('知识库有哪些资料', True), ('这个知识库讲了什么', True),
    ('列出知识库的目录', True), ("what's in the knowledge base", True),
    ('根据知识库里的实验记录，本课题指标是什么', False), ('知识库里的乳酸处理结果是什么', False),
    ('总结一下《实验记录》', False), ('细胞培养实验温度', False),
])
def test_space_overview_classifier(question, expected):
    from app.application.agents.answerability import is_space_overview
    assert is_space_overview(question) is expected


async def test_zero_tool_empty_claims_triggers_one_retrieval_retry(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。研究设计边界另行说明。')
    seen = []
    async def action(tools, payload):
        seen.append(payload)
        if len(seen) == 1:
            assert not payload['revalidated_evidence'] and 'retrieval_notice' not in payload
            return {'answer_status': 'insufficient', 'claims': [], 'missing_evidence': ['未提供信息']}
        assert payload['revalidated_evidence'], 'Second attempt must carry injected retrieval evidence'
        assert 'retrieval_notice' in payload
        item = payload['revalidated_evidence'][0]
        return response(claim('本课题要求发表两篇论文。', 'material', version_ids=[item['version_id']],
                              evidence_quotes=[{'evidence_id': item['evidence_id'],
                                                'version_id': item['version_id'], 'text': item['content']}]))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要发表多少论文')
    assert runtime.calls == 2 and not result['fallback']
    assert result['answer_kind'] == 'grounded' and result['citations']
    assert any(event.get('type') == 'retrieval.injected' for event in result['events'])


async def test_zero_tool_insufficient_clarification_retried_once_then_accepted(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。研究设计边界另行说明。')
    seen = []
    async def action(tools, payload):
        seen.append(payload)
        # A valid clarification block keeps the answer renderable; status stays insufficient.
        return {'answer_status': 'insufficient', 'missing_evidence': ['论文数量的正式规定'],
                'claims': [claim('未找到关于论文数量要求的明确规定。', 'clarification', clarification_kind='other')]}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要发表多少论文')
    assert runtime.calls == 2 and not result['fallback']
    assert result['answer_kind'] == 'clarification' and result['answer_status'] == 'insufficient'
    assert any(event.get('type') == 'retrieval.injected' for event in result['events'])


async def test_empty_claims_after_real_retrieval_still_rejected(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。')
    async def action(tools, payload):
        await tools['search_documents']('完全无关的检索词')
        return {'answer_status': 'insufficient', 'claims': [], 'missing_evidence': []}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '今天天气如何')
    assert runtime.calls == 1 and result['fallback']
    assert result['answer_kind'] == 'failure'
    assert not any(event.get('type') == 'retrieval.injected' for event in result['events'])


async def test_empty_claims_retry_is_capped_at_once(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。研究设计边界另行说明。')
    async def action(tools, payload):
        return {'answer_status': 'insufficient', 'claims': [], 'missing_evidence': []}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要发表多少论文')
    assert runtime.calls == 2 and result['fallback'] and result['answer_kind'] == 'failure'
    assert sum(event.get('type') == 'retrieval.injected' for event in result['events']) == 1


async def test_sufficient_clarification_does_not_trigger_injection(library, monkeypatch):
    service, actor, space, conv = library
    monkeypatch.setattr(service, 'search', lambda *a, **k: pytest.fail('No forced retrieval'))
    async def action(tools, payload):
        return response(claim('请明确你想讨论的范围。', 'clarification'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '完全陌生口语嘞')
    assert runtime.calls == 1 and not result['fallback'] and result['answer_kind'] == 'clarification'


async def test_zero_tool_ungrounded_project_fact_answer_triggers_one_retrieval_retry(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '结项要求', '本课题要求发表两篇论文。研究设计边界另行说明。')
    async def action(tools, payload):
        if runtime.calls == 1:
            assert not payload['revalidated_evidence']
            return response(claim('本课题要求发表两篇论文。'))  # ungrounded project-fact claim
        assert payload['revalidated_evidence'] and payload['retrieval_notice'].startswith('首轮未检索文档')
        item = payload['revalidated_evidence'][0]
        return response(claim('本课题要求发表两篇论文。', 'material', version_ids=[item['version_id']],
                              evidence_quotes=[{'evidence_id': item['evidence_id'], 'version_id': item['version_id'],
                                                'text': '本课题要求发表两篇论文。'}]))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要发表多少论文')
    assert runtime.calls == 2 and not result['fallback'] and result['answer_kind'] == 'grounded'
    assert len(result['citations']) == 1
    assert {'type': 'retrieval.injected', 'reason': 'zero_tool_ungrounded'} in result['events']


async def test_zero_tool_general_self_contained_question_is_not_forced_into_retrieval(library, monkeypatch):
    service, actor, space, conv = library
    monkeypatch.setattr(service, 'search', lambda *a, **k: pytest.fail('No forced retrieval'))
    async def action(tools, payload):
        return response(claim('下降25%。'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '40分钟降到30分钟，相对变化是多少')
    assert runtime.calls == 1 and not result['fallback'] and result['answer_kind'] == 'general'
    assert all(event['type'] != 'retrieval.injected' for event in result['events'])


async def test_zero_tool_ungrounded_retry_without_evidence_accepts_original(library, monkeypatch):
    service, actor, space, conv = library
    service.add_document(actor, space, '无关文档', '今天天气晴朗，适合外出散步。')
    monkeypatch.setattr(service, 'search', lambda *a, **k: [])
    async def action(tools, payload):
        return response(claim('本课题要求发表两篇论文。'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要发表多少论文')
    assert runtime.calls == 1 and not result['fallback'] and result['answer_kind'] == 'general'
    assert all(event['type'] != 'retrieval.injected' for event in result['events'])


async def test_list_tasks_then_insufficient_still_injects_retrieval(library):
    # list_tasks is not document retrieval: an insufficient answer after only
    # listing goals must still trigger the one-shot retrieval injection.
    service, actor, space, conv = library
    service.add_document(actor, space, '进度记录', '课题进展：指标字典草稿未完成，论文整稿未形成。')
    async def action(tools, payload):
        if 'retrieval_notice' not in payload:
            await tools['list_tasks']()
            return {'answer_status': 'insufficient', 'claims': [
                {'source_kind': 'clarification', 'text': '未检索到目标记录，无法判断。', 'clarification_kind': 'other'}]}
        item = payload['revalidated_evidence'][0]
        return {'answer_status': 'partial', 'claims': [
            claim('记录显示论文整稿未形成。', 'material', version_ids=[item['version_id']],
                  evidence_quotes=[{'evidence_id': item['evidence_id'], 'version_id': item['version_id'],
                                    'text': '论文整稿未形成'}])]}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '按现有文字记录，课题是否全部完成')
    assert runtime.calls == 2 and not result['fallback'] and result['answer_kind'] == 'grounded'
    assert {'type': 'retrieval.injected', 'reason': 'zero_tool_insufficient'} in result['events']


async def test_search_with_no_hits_then_insufficient_is_accepted(library, monkeypatch):
    # A model that already searched (finding nothing) must not be re-injected.
    service, actor, space, conv = library
    service.add_document(actor, space, '无关文档', '今天天气晴朗，适合外出散步。')
    monkeypatch.setattr(service, 'search', lambda *a, **k: [])
    async def action(tools, payload):
        await tools['search_documents']('课题 完成 情况')
        return {'answer_status': 'insufficient', 'claims': [
            {'source_kind': 'clarification', 'text': '未找到相关记录。', 'clarification_kind': 'other'}]}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题完成了吗')
    assert runtime.calls == 1 and not result['fallback'] and result['answer_kind'] == 'clarification'
    assert all(event['type'] != 'retrieval.injected' for event in result['events'])


async def test_align_mode_retrieves_before_first_model_call(library):
    service, actor, space, conv = library
    service.add_document(actor, space, '观察记录', '成员A记录观察期为30分钟，成员B记录为0.5小时。')
    async def action(tools, payload):
        assert payload['mode'] == 'align' and payload['revalidated_evidence']
        item = payload['revalidated_evidence'][0]
        return {'answer_status': 'conflicting', 'claims': [
            claim('成员A记录30分钟，成员B记录0.5小时，数值口径需统一。', 'material',
                  version_ids=[item['version_id']],
                  evidence_quotes=[{'evidence_id': item['evidence_id'], 'version_id': item['version_id'],
                                    'text': '成员A记录观察期为30分钟，成员B记录为0.5小时。'}])]}
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, 'A 说观察期30分钟，B说0.5小时，两人是否冲突？', mode='align')
    assert runtime.calls == 1 and not result['fallback'] and result['answer_kind'] == 'grounded'
    assert {'type': 'retrieval.injected', 'reason': 'align_mode'} in result['events']


async def test_align_mode_with_empty_search_accepts_answer_without_injection(library, monkeypatch):
    service, actor, space, conv = library
    monkeypatch.setattr(service, 'search', lambda *a, **k: [])
    async def action(tools, payload):
        assert not payload['revalidated_evidence']
        return response(claim('两种说法数值相等，不冲突。'))
    runtime = Runtime(action)
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, 'A说30分钟，B说0.5小时，冲突吗？', mode='align')
    assert runtime.calls == 1 and not result['fallback']
    assert all(event.get('reason') != 'align_mode' for event in result['events'])


async def test_spliced_quote_is_dropped_but_verbatim_sibling_keeps_claim(library):
    service, actor, space, conv = library
    body = '目标：梳理机制假设。交付物：章节稿。约束：区分相关与因果。'
    key = service.add_document(actor, space, '立项书', body)['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        spliced = '目标：梳理资源供给、可用性、场景使用、组织协同与公共价值结果之间的机制假设及替代解释。\n约束：区分“相关”“可能机制”和“已识别因果效应”；访谈意见不得直接当作因果证据。'
        assert spliced not in item['content']
        return response(claim('立项书列出机制假设并要求区分相关与因果。', 'material',
                              version_ids=[item['version_id']],
                              evidence_quotes=[
                                  {'evidence_id': item['evidence_id'], 'version_id': item['version_id'],
                                   'text': '约束：区分相关与因果。'},
                                  {'evidence_id': item['evidence_id'], 'version_id': item['version_id'],
                                   'text': '目标：梳理机制假设。\n约束：区分相关与因果。'}]))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '立项书写了吗', selected_version_id=key)
    assert not result['fallback'] and result['answer_kind'] == 'grounded'
    assert '机制假设' in result['answer'] and len(result['citations']) == 1
    assert result['answer_status'] == 'partial'
    assert any('逐字核对' in note for note in result['limitations'])


async def test_forged_quote_drops_material_claim_but_clarification_survives(library):
    service, actor, space, conv = library
    key = service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。')['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        return {'answer_status': 'insufficient', 'missing_evidence': ['正式规定'],
                'claims': [claim('伪造的引文论断。', 'material', version_ids=[item['version_id']],
                                 evidence_quotes=[{'evidence_id': item['evidence_id'],
                                                   'version_id': item['version_id'],
                                                   'text': '正文里根本没有这句话。'}]),
                           claim('缺少正式规定的原文记录。', 'clarification', clarification_kind='other')]}
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '本课题要发表多少论文', selected_version_id=key)
    assert not result['fallback'] and result['answer_kind'] == 'clarification'
    assert '伪造的引文论断' not in result['answer'] and '缺少正式规定' in result['answer']
    assert not result['citations']


async def test_clarification_with_verified_quotes_is_reclassified_as_material(library):
    service, actor, space, conv = library
    key = service.add_document(actor, space, '测度口径', '首轮不计算完整综合指数，也不进行排名。')['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        return {'answer_status': 'insufficient', 'missing_evidence': ['综合指数结果'],
                'claims': [{'source_kind': 'clarification',
                            'text': '当前库未提供综合指数结果；协议明确首轮不计算完整综合指数。',
                            'version_ids': [item['version_id']],
                            'evidence_quotes': [{'evidence_id': item['evidence_id'],
                                                 'version_id': item['version_id'],
                                                 'text': '首轮不计算完整综合指数，也不进行排名。'}]}]}
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '综合指数排第几', selected_version_id=key)
    assert not result['fallback'] and result['answer_status'] == 'insufficient'
    assert result['answer_kind'] == 'grounded' and len(result['citations']) == 1
    assert '首轮不计算' in result['answer']


async def test_material_claim_citing_version_without_any_quote_stays_dropped(library):
    service, actor, space, conv = library
    key = service.add_document(actor, space, '测度口径', '本课题要求发表两篇论文。')['latest_version']['id']
    async def action(tools, payload):
        item = await tools['read_document'](key)
        return response(claim('无引文的材料论断。', 'material', version_ids=[item['version_id']]),
                        claim('通用解释。'))
    result = await ResearchAssistant(service, Runtime(action)).answer(actor, space, conv, '本课题要发表多少论文', selected_version_id=key)
    assert not result['fallback'] and '无引文的材料论断' not in result['answer']
    assert not result['citations'] and result['answer_status'] == 'partial'
