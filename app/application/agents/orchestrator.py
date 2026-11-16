"""One model-led assistant; the program owns scope, budgets and persistence."""
from __future__ import annotations

import inspect
import json
import logging
import re
from contextvars import ContextVar
from typing import Mapping

from app.domain.ai_ports import ResearchRuntime, ResearchService, AgentDescription, ToolDescription
from app.domain.ai_safety import AISafety
from app.domain.answer_metadata import normalize_answer_metadata
from app.domain.errors import DomainError
from app.domain.run_events import EventSink
from .answerability import AnswerOutcome, add_disclosures
from .evidence import _call, completed_stream_claims
from .main_policy import AnswerLanguageError, describe_main, render_main
from .main_request import MainRequest, MAX_READ_CHARS, MAX_READ_LENGTH, MAX_TOOL_CALLS
from .policies import MAX_QUESTION_LENGTH, MAX_SEARCH_LENGTH, ROUTES

logger = logging.getLogger(__name__)
_FAILURE_REASONS = frozenset({
    'stream_tool_limit', 'stream_invalid_tools', 'stream_missing_finish', 'stream_missing_done',
    'stream_mixed_tools_and_text', 'stream_invalid_finish', 'stream_output_limit',
    'stream_content_filter', 'stream_provider_error', 'stream_unexpected_tools',
    'stream_content_after_finish', 'model_first_response_timeout', 'model_stream_timeout',
    'pipeline_timeout', 'model_final_timeout', 'model_request_timeout', 'tool_timeout',
})
_TIMEOUT_MESSAGES = {
    'model_first_response_timeout': '模型服务暂未响应，本次回答未完成。',
    'model_stream_timeout': '回答生成中断，临时内容未作为最终答案保存。',
    'pipeline_timeout': '本次处理已达到时间上限，或剩余时间不足以继续生成完整回答。',
    'model_final_timeout': '资料已读取，但最终回答生成超时。',
    'model_request_timeout': '模型请求超过单轮时间限制，本次回答未完成。',
    'tool_timeout': '资料读取或检索超时，本次回答未完成。',
}
# Questions pointing at project/library facts. Self-contained general questions
# (definitions, unit conversion, math on numbers given in the question) do not
# match and are never forced into retrieval.
_PROJECT_FACT_HINT = re.compile(
    r'本课题|课题|本项目|项目|立项|知识库|当前库|库中|库里|库内|文档|材料|资料|协议'
    r'|记录|数据集|进度|交付|章节|论文|纪要|台账|报告|指标|口径|排名|总分')


_SECURITY_CONTEXT: ContextVar[dict | None] = ContextVar("security_context", default=None)


class ResearchAssistant:
    def __init__(self, service: ResearchService, runtime: ResearchRuntime | None = None, *,
                 safety: AISafety | None = None, security_recorder=None):
        self.service, self.runtime = service, runtime
        self.safety = safety
        self.security_recorder = security_recorder

    def _security(self, event_type: str, detail: str) -> None:
        """把安全信号交给落库回调；未装配时静默跳过（评测/单测不依赖它）。"""
        recorder = self.security_recorder
        if recorder is None:
            return
        context = _SECURITY_CONTEXT.get() or {}
        recorder(event_type, detail=detail, actor_id=context.get("actor_id") or "",
                 space_id=context.get("space_id") or "",
                 conversation_id=context.get("conversation_id") or "",
                 run_id=context.get("run_id") or "")

    def _output(self, text: str) -> str:
        if self.safety is None:
            return text
        guarded = self.safety.output_text(text)
        if guarded != text:
            self._security('security.redaction', '最终回答命中脱敏规则')
        return guarded

    def _tools(self, agent: AgentDescription) -> AgentDescription:
        if self.safety is None:
            return agent
        def wrap(tool):
            async def invoke(*args, **kwargs):
                try:
                    value = await tool.invoke(*args, **kwargs)
                except DomainError as error:
                    if error.status_code in (401, 403):
                        self._security('security.cross_space', f'工具越权被拒: {tool.name}')
                    raise
                return self.safety.tool_result(value)
            return ToolDescription(tool.name, tool.description, invoke)
        return AgentDescription(agent.name, agent.instructions, tuple(wrap(tool) for tool in agent.tools))

    async def answer(self, actor_id: str, space_id: str, conversation_id: str,
                     question: str, mode: str = 'qa', *, event_sink: EventSink | None = None,
                     authorization_check=None, commit_guard=None, message_id=None,
                     run_id=None, request_id=None, save_authorization_check=None,
                     selected_version_id=None) -> dict:
        if mode not in ROUTES:
            raise DomainError(422, 'mode must be qa, align or progress')
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_LENGTH:
            raise DomainError(422, 'question must contain 1-8000 characters')
        if mode == 'progress' and selected_version_id is not None:
            raise DomainError(422, 'Progress mode does not read document context')
        _SECURITY_CONTEXT.set({'actor_id': actor_id, 'space_id': space_id,
                               'conversation_id': conversation_id, 'run_id': run_id or ''})
        request = MainRequest(self.service, actor_id, space_id, conversation_id,
                              selected_version_id=selected_version_id)
        request.authorization_check = authorization_check
        async def guarded_sink(event):
            value = self.safety.output_record(event) if self.safety is not None else event
            await event_sink(value)
        request.commit_guard, request.event_sink = commit_guard, guarded_sink if event_sink is not None else None
        request.question = question
        await request.initialize(include_history=mode != 'progress')
        parameters = inspect.signature(self.service.append_message).parameters
        user_options = {}
        if 'authorization_check' in parameters:
            user_options = {'authorization_check': save_authorization_check, 'expected_epoch': request._epoch}
        await _call(self.service, 'append_message', actor_id, conversation_id, 'user', question, [], **user_options)
        sink_error = None
        events = [{'type': 'agent.route', 'agent': 'ResearchCoordinator'}]

        async def emit(kind, **data):
            nonlocal sink_error
            await request.check()
            if event_sink is not None:
                try:
                    await guarded_sink({'type': kind, **data})
                except BaseException as exc:
                    sink_error = exc
                    raise

        fallback, answer_kind = True, 'failure'
        state = None
        if self.runtime is None:
            outcome = AnswerOutcome('当前未配置可用模型，暂时无法处理本次问题。请配置模型服务后重试。',
                                    'insufficient', limitations=['未调用模型；不是因为问题未匹配关键词或知识库为空。'])
            # Persisted history must record why this answer was a fallback.
            events.append({'type': 'model.fallback', 'reason': 'not_configured'})
        else:
            stage = 'model'
            try:
                state = self.runtime.new_request_state()
                if mode != 'progress':
                    await request.load_space_overview()
                agent = self._tools(describe_main(request, mode))
                events.append({'type': 'agent.started', 'agent': agent.name})
                await emit('agent.started', agent=agent.name)
                stream = getattr(self.runtime, 'stream_coordinator', None)
                retried = False
                if mode == 'align' and request.space_overview is None and not request.evidence:
                    # Align mode checks member statements against materials:
                    # retrieval is mandatory and never left to model discretion.
                    await request.search(question[:MAX_SEARCH_LENGTH])
                    if request.evidence:
                        events.append({'type': 'retrieval.injected', 'reason': 'align_mode'})
                while True:
                    payload = {'question': question, 'mode': mode,
                               'current_space': {'id': space_id, 'notice': 'Scope only, retrieval is optional.'},
                               'selected_document': request.selected_document,
                               'history': request.history, 'history_notice': request.history_notice,
                               'previous_run': request.previous_run,
                               'revalidated_evidence': list(request.evidence.values()),
                               'budgets': {'max_tool_calls': MAX_TOOL_CALLS, 'max_body_chars': MAX_READ_CHARS,
                                           'max_read_length': MAX_READ_LENGTH},
                               'history_scope': 'Untrusted context, never current supporting evidence.'}
                    if request.space_overview is not None:
                        payload['space_overview'] = request.space_overview
                    if retried:
                        payload['retrieval_notice'] = ('首轮未检索文档且回答无效或判不足；revalidated_evidence已是'
                            '程序按问题完成的真实检索结果，须据此作答；仍不足时用clarification块说明具体缺失，claims不得为空。'
                            '若检索结果与问题无关，保持通用回答，不得强行引用。')
                    if self.safety is not None:
                        payload = self.safety.input_payload(payload)
                        if payload.get('injection_detected'):
                            self._security('security.injection',
                                           '问题含注入或批量转储指令，已按不可信输入过滤')
                    if event_sink is not None and callable(stream):
                        raw = ''
                        iterator = stream(agent, payload, check=request.check, state=state)
                        try:
                            # The SSE reader already revalidates authorization at
                            # a bounded cadence; per-fragment checks here would
                            # double that cost. Strict checks run at attempt start
                            # and right after the stream completes (below).
                            async for fragment in iterator:
                                if not isinstance(fragment, str) or len(raw) + len(fragment) > 100000:
                                    raise ValueError('Assistant output exceeds limit')
                                raw += fragment
                        finally:
                            close = getattr(iterator, 'aclose', None)
                            if close is not None:
                                await close()
                    else:
                        raw = await self.runtime.run_coordinator(agent, payload, check=request.check, state=state)
                    await request.check()
                    if not isinstance(raw, str) or len(raw) > 100000:
                        raise ValueError('Assistant output exceeds limit')
                    stage = 'validation'
                    try:
                        outcome, answer_kind = render_main(raw, request)
                    except AnswerLanguageError:
                        raise
                    except ValueError as validation_error:
                        if (retried or str(validation_error) != 'No valid answer blocks'
                                or request.searched or request.evidence
                                or mode == 'progress' or request.space_overview is not None):
                            raise
                        # Empty answer with no retrieval attempted (tools like
                        # list_tasks do not read documents): inject one
                        # deterministic retrieval, then retry once.
                        stage = 'model'
                        await request.search(question[:MAX_SEARCH_LENGTH])
                        if not request.evidence:
                            raise validation_error
                        retried = True
                        events.append({'type': 'retrieval.injected', 'reason': 'zero_tool_empty_answer'})
                        continue
                    if (not retried and outcome.status == 'insufficient'
                            and not request.searched and not request.evidence
                            and mode != 'progress' and request.space_overview is None):
                        # Insufficient or refusal without any retrieval (list_tasks
                        # and similar tools are not document retrieval): same
                        # one-shot injection before accepting.
                        stage = 'model'
                        await request.search(question[:MAX_SEARCH_LENGTH])
                        if request.evidence:
                            retried = True
                            events.append({'type': 'retrieval.injected', 'reason': 'zero_tool_insufficient'})
                            continue
                        stage = 'validation'
                    elif (not retried and outcome.status in {'sufficient', 'partial'}
                            and answer_kind != 'clarification' and not outcome.citation_ids
                            and not request.searched and not request.evidence
                            and mode != 'progress' and request.space_overview is None
                            and _PROJECT_FACT_HINT.search(question)):
                        # Citation-free answer to a project/library fact
                        # question: inject one retrieval before accepting. If the
                        # search finds nothing the original answer is accepted.
                        stage = 'model'
                        await request.search(question[:MAX_SEARCH_LENGTH])
                        if request.evidence:
                            retried = True
                            events.append({'type': 'retrieval.injected', 'reason': 'zero_tool_ungrounded'})
                            continue
                        stage = 'validation'
                    fallback = False
                    events.append({'type': 'agent.completed', 'agent': agent.name})
                    await emit('agent.completed', agent=agent.name)
                    break
            except Exception as exc:
                if sink_error is not None:
                    raise sink_error
                if request.authorization_error is not None:
                    raise request.authorization_error
                if isinstance(exc, (DomainError, PermissionError)):
                    raise
                reason = getattr(exc, 'reason', None)
                reason = reason if isinstance(reason, str) and reason in _FAILURE_REASONS else 'unclassified'
                # Never log exception prose/traceback: provider exceptions can
                # contain credentials, request bodies or private document text.
                logger.warning('research.answer_failed trace=%s stage=%s error_type=%s reason=%s tool_calls=%d read_chars=%d',
                               getattr(state, 'trace_id', 'unavailable'), stage, type(exc).__name__, reason,
                               request.tool_calls, request.read_chars)
                answer_kind = 'failure'
                category = ('answer_language_failed' if isinstance(exc, AnswerLanguageError) else
                            'answer_validation_failed' if stage == 'validation' else
                            'model_timeout' if isinstance(exc, TimeoutError) else 'model_request_failed')
                detail = {'answer_language_failed': '模型未按要求使用中文生成回答，本次内容未作为最终答案展示，请重新发送问题。',
                          'answer_validation_failed': '模型返回内容未通过完整性或引用结构校验，未保存临时文字为终稿。',
                          'model_timeout': '本次模型调用超时，请稍后重试。',
                          'model_request_failed': '模型服务请求或流式响应未正常完成，请检查配置或稍后重试。'}[category]
                if reason in _TIMEOUT_MESSAGES:
                    detail = _TIMEOUT_MESSAGES[reason]
                    if reason == 'model_final_timeout' and not request.read_chars and request.tasks is None:
                        detail = '最终回答生成超时，本次回答未完成。'
                elif reason == 'stream_tool_limit':
                    detail = '工具预算已用完，模型在禁用工具的最终回答轮仍请求工具，未生成完整回答；未执行额外工具。'
                elif reason == 'stream_mixed_tools_and_text':
                    detail = '模型在同一轮混合返回回答文字与工具请求，当前安全流式协议未接受该响应。'
                elif reason in {'stream_missing_finish', 'stream_missing_done', 'stream_invalid_finish'}:
                    detail = '模型流式响应缺少有效结束标记或连接提前结束，未将临时内容保存为终稿。'
                outcome = AnswerOutcome(detail, 'insufficient', limitations=['没有生成可用的最终回答，不以通用答案绕过权限或材料核验。'])
                events.append({'type': 'model.fallback', 'reason': reason if reason in _TIMEOUT_MESSAGES else category})
        outcome.answer = self._output(outcome.answer)
        outcome.limitations = [self._output(value) for value in outcome.limitations]
        outcome.missing_evidence = [self._output(value) for value in outcome.missing_evidence]
        if fallback and request.evidence:
            # Useful deterministic recovery, never presented as a model summary.
            used_versions = set()
            excerpts = []
            for evidence_id, item in request.evidence.items():
                if item['version_id'] in used_versions or len(excerpts) >= 2:
                    continue
                if self.safety is not None and self.safety.output_text(item['content']) != item['content']:
                    continue
                used_versions.add(item['version_id'])
                request.accepted_fragments = getattr(request, 'accepted_fragments', set()) | {evidence_id}
                outcome.citation_ids.add(item['version_id'])
                excerpts.append('《' + item['title'] + '》：\n> ' + item['content'][:600].replace('\n', '\n> '))
            if excerpts:
                outcome.answer += '\n\n已读取的原文摘录（非AI总结）：\n\n' + '\n\n'.join(excerpts)
                outcome.limitations.append('以上只展示本次已读取的部分原文，不代表已完成概括；可查看引用原文或手动重试。')
        output = self._output(add_disclosures(outcome))
        if request.history_notice:
            events.append({'type': 'history.bounded', 'max_messages': 12, 'max_chars': 12000})
        await request.check()
        events.extend(request.retrieval_events)
        citations = []
        for item in request.evidence.values():
            if (item['version_id'] not in outcome.citation_ids
                    or item['evidence_id'] not in getattr(request, 'accepted_fragments', set())):
                continue
            citations.append({key: item[key] for key in ('version_id', 'document_id', 'title', 'source',
                             'evidence_id', 'chunk_start', 'chunk_end', 'document_length', 'retrieval_strategy',
                             'chunk_id', 'rerank_applied') if key in item} | {'excerpt': item['content']})
        if self.safety is not None:
            display_citations = []
            for citation in citations:
                clean = self.safety.output_record(citation)
                if clean.get('excerpt') != citation.get('excerpt'):
                    # Changed text can no longer be presented as reproducible
                    # raw evidence at the original offsets.
                    continue
                display_citations.append(clean)
            if len(display_citations) != len(citations):
                outcome.status = 'partial'
                outcome.limitations.append('部分引用包含敏感信息，已停止展示该片段；未将脱敏文本当作原文证据。')
                output = self._output(add_disclosures(outcome))
            citations = display_citations
            events = self.safety.output_record(events)
        clarification_metadata = ({'clarification_kind': request.clarification_kind}
                                  if answer_kind == 'clarification' else {})
        display_events = [e for e in events if not str(e.get('type', '')).startswith('agent.')]
        metadata = normalize_answer_metadata({'answer_status': outcome.status, 'answer_kind': answer_kind,
                    **clarification_metadata,
                    'missing_evidence': outcome.missing_evidence[:10], 'suggested_actions': outcome.suggested_actions,
                    'fallback': fallback, 'mode': mode, 'events': display_events[:100],
                    **{key: value for key, value in (('message_id', message_id), ('run_id', run_id),
                                                    ('request_id', request_id)) if value is not None}})
        supports_metadata = 'metadata' in parameters or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
        kwargs = {'metadata': metadata} if supports_metadata else {}

        def validate_save(repository):
            if save_authorization_check is not None:
                save_authorization_check(repository)
            request.save_check(repository)

        options = {'message_id': message_id, 'commit_guard': commit_guard,
                   'authorization_check': validate_save, 'expected_epoch': request._epoch}
        kwargs.update({key: value for key, value in options.items() if key in parameters})
        result = {'answer': output, 'citations': citations, 'events': display_events,
                  'conversation_id': conversation_id, 'mode': mode, 'fallback': fallback,
                  'answer_status': outcome.status, 'answer_kind': answer_kind, **clarification_metadata,
                  'limitations': outcome.limitations,
                  'missing_evidence': outcome.missing_evidence[:10], 'suggested_actions': outcome.suggested_actions,
                  'evidence_scope': 'current_request_only',
                  **{key: value for key, value in (('message_id', message_id), ('run_id', run_id),
                                                  ('request_id', request_id)) if value is not None}}
        if state is not None:
            result["_meta"] = {
                "total_tokens": getattr(state, "total_tokens", 0),
                "prompt_tokens": getattr(state, "prompt_tokens", 0),
                "completion_tokens": getattr(state, "completion_tokens", 0),
                "trace_id": getattr(state, "trace_id", ""),
            }
        if commit_guard is not None:
            commit_guard.check()
            commit_guard.result = result
        await _call(self.service, 'append_message', actor_id, conversation_id, 'assistant', output, citations, **kwargs)
        if commit_guard is None or not commit_guard.committed:
            await request.check()
        return result


async def answer(service: ResearchService, actor_id: str, space_id: str, conversation_id: str,
                 question: str, mode: str = 'qa', *, runtime: ResearchRuntime | None = None,
                 selected_version_id=None) -> dict:
    return await ResearchAssistant(service, runtime).answer(actor_id, space_id, conversation_id,
                                                           question, mode, selected_version_id=selected_version_id)
