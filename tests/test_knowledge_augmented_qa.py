"""Product source-policy regressions, no external service or model calls."""
import json
import secrets

import pytest

from app.application.agents.legacy_orchestrator import ResearchAssistant
from app.application.agents.qa_policy import question_source, render_conversation
from app.infrastructure.persistence.factory import build_research_service


@pytest.mark.parametrize('question,kind', [
    ('什么是政府数据公共价值？', 'general'),
    ('请问研究方法是什么？', 'general'),
    ('解释一下定性和定量方法的区别', 'general'),
    ('帮我设计一个访谈提纲', 'general'),
    ('本课题要求发表几篇论文？', 'grounded'),
    ('要求发表几篇论文？', 'grounded'),
    ('截止日期是哪天？', 'grounded'),
    ('According to our project, how many papers are required?', 'grounded'),
    ('我们目前完成了多少目标？', 'grounded'),
    ('根据我们的材料提出三个研究假设', 'mixed'),
    ('这个文档说了什么', 'grounded'),
    ('总结《样本口径》', 'grounded'),
])
def test_source_routes(question, kind):
    assert question_source(question, 'qa') == kind
    assert question_source(question, 'align') == 'grounded'


class GeneralRuntime:
    def __init__(self):
        self.payloads = []

    def new_request_state(self):
        return object()

    async def run_coordinator(self, agent, payload, **options):
        await options['check']()
        assert not agent.tools
        self.payloads.append(payload)
        return json.dumps({'claims': [{'text': '可以从研究问题、访谈对象与分析框架三个方面设计提纲。',
                                      'source_kind': 'suggestion'}]}, ensure_ascii=False)


@pytest.fixture
def library(tmp_path):
    service = build_research_service(tmp_path / 'research.db')
    actor = service.bootstrap('owner@example.org', secrets.token_urlsafe(24), 'Synthetic')['user']['id']
    space = next(s['id'] for s in service.list_spaces(actor) if s['kind'] == 'basis')
    conv = service.create_conversation(actor, space)
    return service, actor, space, conv['id']


async def test_general_qa_without_documents_calls_model_and_persists_source_kind(library, monkeypatch):
    service, actor, space, conv = library
    monkeypatch.setattr(service, 'search', lambda *a, **k: pytest.fail('General question must not require retrieval'))
    runtime = GeneralRuntime()
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '帮我设计一个访谈提纲')
    assert not result['fallback'] and not result['citations']
    assert result['answer_kind'] == 'general' and '建议／待验证' in result['answer']
    assert len(runtime.payloads) == 1
    assert 'authorized_evidence' not in runtime.payloads[0]
    history = service.get_conversation(actor, conv)
    assert history['messages'][-1]['answer_kind'] == 'general'


async def test_short_topic_miss_still_allows_general_discussion(library):
    service, actor, space, conv = library
    result = await ResearchAssistant(service, GeneralRuntime()).answer(actor, space, conv, '量子纠错')
    assert result['answer_kind'] == 'general' and not result['fallback']


async def test_project_fact_miss_never_uses_general_knowledge_to_guess(library):
    service, actor, space, conv = library
    runtime = GeneralRuntime()
    result = await ResearchAssistant(service, runtime).answer(actor, space, conv, '本课题要求发表几篇论文？')
    assert result['answer_kind'] == 'grounded'
    assert result['answer_status'] == 'insufficient' and not runtime.payloads


async def test_mixed_missing_materials_can_offer_labeled_generic_suggestions(library):
    service, actor, space, conv = library
    result = await ResearchAssistant(service, GeneralRuntime()).answer(actor, space, conv, '根据我们的材料提出三个研究假设')
    assert result['answer_kind'] == 'mixed' and not result['citations']
    assert '材料可确认部分' in result['answer'] and '建议／待验证' in result['answer']
    assert '证据不足' in result['answer']


async def test_unconfigured_general_model_is_not_reported_as_missing_keywords(library):
    service, actor, space, conv = library
    result = await ResearchAssistant(service).answer(actor, space, conv, '什么是公共价值？')
    assert result['answer_kind'] == 'general' and result['fallback']
    assert '未配置' in result['answer'] and '主题相关候选' not in result['answer']


async def test_general_followup_keeps_uncited_general_context(library):
    service, actor, space, conv = library
    runtime = GeneralRuntime()
    assistant = ResearchAssistant(service, runtime)
    await assistant.answer(actor, space, conv, '帮我设计一个访谈提纲')
    result = await assistant.answer(actor, space, conv, '继续')
    assert result['answer_kind'] == 'general'
    assert any(h.get('source_kind') == 'general' for h in runtime.payloads[-1]['history'])


def test_conversational_citation_forgery_is_rejected():
    with pytest.raises(ValueError):
        render_conversation(json.dumps({'claims': [{'text': 'Unverified fact', 'source_kind': 'general',
                                                  'version_ids': ['private-id']}]}))


async def test_general_sse_fragments_need_no_fake_citations(library):
    service, actor, space, conv = library
    class Runtime(GeneralRuntime):
        async def stream_coordinator(self, agent, payload, **options):
            yield await self.run_coordinator(agent, payload, **options)
    events = []
    async def sink(event):
        events.append(event)
    result = await ResearchAssistant(service, Runtime()).answer(actor, space, conv, '帮我设计访谈提纲', event_sink=sink)
    assert result['answer_kind'] == 'general' and not result['fallback']
    assert any(e['type'] == 'answer.delta' and not e['version_ids'] for e in events)
