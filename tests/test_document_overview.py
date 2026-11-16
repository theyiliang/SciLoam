"""Document overview selects authorized documents, not matching body keywords."""
import copy
import secrets

import pytest

from app.application.agents.evidence import _Request
from app.application.agents.legacy_orchestrator import ResearchAssistant
from app.application.agents.policies import MAX_EXCERPT_LENGTH
from app.domain.errors import DomainError
from app.infrastructure.persistence.factory import build_research_service


@pytest.fixture
def library(tmp_path, monkeypatch):
    service = build_research_service(tmp_path / 'research.db')
    actor = service.bootstrap('owner@example.org', secrets.token_urlsafe(24), 'Overview')['user']['id']
    space = next(s['id'] for s in service.list_spaces(actor) if s['kind'] == 'basis')

    def no_search(*args, **kwargs):
        pytest.fail('Document overview must not call keyword or external search')

    monkeypatch.setattr(service, 'search', no_search)
    return service, actor, space


@pytest.mark.parametrize('question', ['这个文档说了什么', '请总结一下这份文档', '这篇文章主要讲了什么？'])
async def test_single_authorized_document_has_local_overview_without_keywords(library, question):
    service, actor, space = library
    document = service.add_document(actor, space, '样本口径', '唯一数据集按标识去重，仅下载不算场景使用。')
    conversation = service.create_conversation(actor, space)
    result = await ResearchAssistant(service).answer(actor, space, conversation['id'], question)
    assert result['fallback'] and result['answer_status'] == 'partial'
    assert result['citations'] and '唯一数据集' in result['answer']
    assert result['citations'][0]['document_id'] == document['id']
    assert '不是AI总结' in result['answer']
    assert '完整正文' in result['answer']
    assert '不保证覆盖全文' not in result['answer']


@pytest.mark.parametrize('question', ['总结《样本口径》', '样本口径这份文档说了什么', '样本口径这个文档主要讲了什么？'])
async def test_explicit_title_selects_current_library_document_without_body_keywords(library, question):
    service, actor, space = library
    selected = service.add_document(actor, space, '样本口径', '唯一数据集按标识去重，仅下载不算场景使用。')
    service.add_document(actor, space, '其它材料', '这里不是目标资料。')
    conversation = service.create_conversation(actor, space)
    result = await ResearchAssistant(service).answer(actor, space, conversation['id'], question)
    assert result['answer_status'] == 'partial'
    assert [row['document_id'] for row in result['citations']] == [selected['id']]
    assert '唯一数据集' in result['answer'] and '这里不是目标资料' not in result['answer']


async def test_ambiguous_overview_asks_for_document_instead_of_claiming_no_evidence(library):
    service, actor, space = library
    for title in ['甲', '乙']:
        service.add_document(actor, space, title, '这是各自文档的内容。')
    conversation = service.create_conversation(actor, space)
    result = await ResearchAssistant(service).answer(actor, space, conversation['id'], '这个文档说了什么')
    assert '尚不能确定' in result['answer'] and not result['citations']


class OverviewService:
    """In-memory read-only adapter; no database, search, runtime or network."""

    def __init__(self, documents):
        self.documents = []
        self.versions = {}
        self.read_ids = []
        self.revoked = set()
        for index, (title, content) in enumerate(documents):
            document_id, version_id = f'd{index}', f'v{index}'
            self.documents.append({'id': document_id, 'title': title,
                                   'latest_version': {'id': version_id, 'index_status': 'ready'}})
            self.versions[version_id] = {'id': version_id, 'document_id': document_id,
                                         'space_id': 'current', 'title': title, 'content': content}

    def get_conversation(self, actor, conversation):
        return {'space_id': 'current', 'space_epoch': 1, 'messages': []}

    def list_documents(self, actor, space):
        assert space == 'current'
        return copy.deepcopy(self.documents)

    def get_version(self, actor, version_id):
        self.read_ids.append(version_id)
        if version_id in self.revoked:
            raise DomainError(410, 'Document has been withdrawn')
        return copy.deepcopy(self.versions[version_id])

    def search(self, *args, **kwargs):
        pytest.fail('Document overview must never search')


def overview_request(service, question='这个文档说了什么'):
    request = _Request(service, 'actor', 'current', 'conversation')
    request.question = question
    return request


@pytest.mark.parametrize('question', ['总结《未知标题》', '未知标题这份文档说了什么',
                                    '总结《样本》', '样本这份文档说了什么'])
async def test_missing_or_partial_title_does_not_fall_back_to_only_document(question):
    service = OverviewService([('样本口径', '标题和问题都不会出现在正文中。')])
    request = overview_request(service, question)
    assert await request.document_overview()
    assert not request.evidence and not service.read_ids
    assert '核对标题' in request.overview_notice


async def test_duplicate_complete_title_does_not_choose_arbitrary_document():
    service = OverviewService([('样本口径', '第一份。'), ('样本口径', '第二份。')])
    request = overview_request(service, '总结《样本口径》')
    assert await request.document_overview()
    assert not request.evidence and not service.read_ids
    assert '同名' in request.overview_notice


@pytest.mark.parametrize('question', ['这个文档说了什么', '总结《样本口径》'])
async def test_empty_or_unready_library_has_no_evidence(question):
    service = OverviewService([('样本口径', '尚未就绪。')])
    service.documents[0]['latest_version']['index_status'] = 'pending'
    request = overview_request(service, question)
    assert await request.document_overview()
    assert not request.evidence and not service.read_ids
    assert '没有可读取' in request.overview_notice
    service.documents.clear()
    assert await request.document_overview()
    assert not request.evidence


async def test_withdrawn_document_is_not_selected(library):
    service, actor, space = library
    document = service.add_document(actor, space, '已撤回', '不应进入概览。')
    previous_conversation = service.create_conversation(actor, space)
    service.withdraw_document(actor, document['id'])
    previous_request = _Request(service, actor, space, previous_conversation['id'])
    previous_request.question = '总结《已撤回》'
    with pytest.raises(DomainError, match='权限已变化'):
        await previous_request.document_overview()
    conversation = service.create_conversation(actor, space)
    request = _Request(service, actor, space, conversation['id'])
    request.question = '总结《已撤回》'
    assert await request.document_overview()
    assert not request.evidence
    assert '没有可读取' in request.overview_notice


async def test_revocation_between_listing_and_version_read_propagates():
    service = OverviewService([('样本口径', '权限敏感内容。')])
    service.revoked.add('v0')
    request = overview_request(service)
    with pytest.raises(DomainError, match='withdrawn'):
        await request.document_overview()
    assert not request.evidence
    assert request.authorization_error is not None


async def test_revocation_after_selection_is_rechecked():
    service = OverviewService([('样本口径', '权限敏感内容。')])
    request = overview_request(service)
    assert await request.document_overview()
    service.revoked.add('v0')
    with pytest.raises(DomainError, match='withdrawn'):
        await request.check()


@pytest.mark.parametrize('size', [MAX_EXCERPT_LENGTH - 1, MAX_EXCERPT_LENGTH, MAX_EXCERPT_LENGTH + 1, 10000])
async def test_overview_excerpt_budget_and_truthful_scope_at_length_boundary(size):
    body = ('首段内容。' + '中' * size + '尾段内容。')[:size]
    service = OverviewService([('长文', body)])
    request = overview_request(service)
    assert await request.document_overview()
    value = request.evidence['v0']
    assert value['content'] == body[:MAX_EXCERPT_LENGTH]
    assert len(value['content']) <= MAX_EXCERPT_LENGTH
    assert value['chunk_start'] == 0
    assert value['chunk_end'] == len(value['content'])
    assert value['content'] == body[value['chunk_start']:value['chunk_end']]
    assert 'chunk_id' not in value  # No invented indexed chunk identity.
    assert value['overview_complete'] == (size <= MAX_EXCERPT_LENGTH)
    if size <= MAX_EXCERPT_LENGTH:
        assert '完整正文' in request.overview_notice
        assert '中后部未纳入概览证据' not in request.overview_notice
    else:
        assert '中后部未纳入概览证据' in request.overview_notice
        assert '不保证覆盖全文' in request.overview_notice
        assert str(size) in request.overview_notice


async def test_full_version_fingerprint_detects_changes_outside_excerpt():
    body = '开头' + '中' * MAX_EXCERPT_LENGTH + '原末尾'
    service = OverviewService([('长文', body)])
    request = overview_request(service)
    assert await request.document_overview()
    service.versions['v0']['content'] = body[:-3] + '新末尾'
    with pytest.raises(DomainError, match='Evidence changed'):
        await request.check()


async def test_transaction_fingerprint_rechecks_entire_overview_version():
    service = OverviewService([('长文', '中' * (MAX_EXCERPT_LENGTH + 10))])
    request = overview_request(service)
    assert await request.document_overview()
    service._version = lambda repository, actor, version_id: copy.deepcopy(service.versions[version_id])
    request.save_check(object())
    service.versions['v0']['content'] += '证据边界之外的变更'
    with pytest.raises(DomainError, match='Evidence changed'):
        request.save_check(object())


@pytest.mark.parametrize('body', ['', '   \n\t'])
async def test_empty_document_body_does_not_become_supporting_evidence(body):
    service = OverviewService([('空文档', body)])
    request = overview_request(service)
    assert await request.document_overview()
    assert not request.evidence
    assert '没有可读取的正文' in request.overview_notice


async def test_listed_version_must_belong_to_selected_document():
    service = OverviewService([('甲文档', '甲内容。'), ('乙文档', '乙内容。')])
    service.documents[0]['latest_version']['id'] = 'v1'
    request = overview_request(service, '总结《甲文档》')
    with pytest.raises(DomainError, match='version changed'):
        await request.document_overview()
    assert not request.evidence
    assert request.authorization_error is not None


async def test_current_space_check_is_not_bypassed_by_matching_title():
    service = OverviewService([('样本口径', '其它库不应使用。')])
    service.versions['v0']['space_id'] = 'other-space'
    request = overview_request(service, '总结《样本口径》')
    with pytest.raises(DomainError, match='outside'):
        await request.document_overview()
    assert not request.evidence


async def test_overview_ignores_history_when_current_documents_are_ambiguous():
    service = OverviewService([('当前甲', '甲内容。'), ('当前乙', '乙内容。')])
    request = overview_request(service)
    request.history = [{'role': 'assistant', 'content': '上次总结的是当前甲', 'version_ids': ['v0']}]
    request.history_evidence['v0'] = copy.deepcopy(service.versions['v0'])
    assert await request.document_overview()
    assert not request.evidence and not service.read_ids
    assert '尚不能确定' in request.overview_notice


async def test_only_current_document_wins_over_historical_reference():
    service = OverviewService([('当前甲', '当前唯一授权内容。')])
    request = overview_request(service)
    request.history = [{'role': 'assistant', 'content': '以前谈过其它库旧标题', 'version_ids': ['old']}]
    request.history_evidence['old'] = {'title': '旧标题', 'space_id': 'other-space', 'content': '旧内容'}
    assert await request.document_overview()
    assert list(request.evidence) == ['v0']
    assert request.evidence['v0']['content'] == '当前唯一授权内容。'


async def test_non_overview_topic_question_does_not_select_only_document():
    service = OverviewService([('样本口径', '会议和观察指标。')])
    request = overview_request(service, '观察指标怎么计算？')
    assert not await request.document_overview()
    assert not request.evidence and not service.read_ids
