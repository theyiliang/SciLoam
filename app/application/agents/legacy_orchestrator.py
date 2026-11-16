"""Business orchestration with authorization and conservative answerability gates."""
from __future__ import annotations

import inspect
import json
from app.domain.run_events import EventSink

from app.domain.ai_ports import ResearchRuntime, ResearchService
from app.domain.answer_metadata import normalize_answer_metadata
from app.domain.errors import DomainError
from .answerability import (AnswerOutcome, add_disclosures, assess_topic,
                            insufficient, render_coordinator)
from .evidence import _Request, _call, completed_stream_claims
from .policies import MAX_QUESTION_LENGTH, MAX_SEARCH_LENGTH, ROUTES
from .tools import describe_agents
from .qa_policy import question_source, conversational_agent, render_conversation


class ResearchAssistant:
    def __init__(self, service: ResearchService, runtime: ResearchRuntime | None = None):
        self.service, self.runtime = service, runtime

    @staticmethod
    def _render(text: str, request: _Request, mode: str) -> AnswerOutcome:
        outcome = render_coordinator(text, request.evidence, request.tasks or [],
                                     assess_topic(request.question, request.evidence), mode)
        if mode == "progress":
            # Presentation-only adaptation; keep the legacy task_ids provenance
            # checks and answerability renderer unchanged.
            outcome.answer = outcome.answer.replace("任务记录整理（", "目标记录整理（", 1)
            for record in request.tasks or []:
                outcome.answer = outcome.answer.replace(f"任务记录 {record['id']}", f"目标记录 {record['id']}")
        return outcome

    @staticmethod
    def _fallback(request: _Request, mode: str, reason: str) -> AnswerOutcome:
        gate = assess_topic(request.question, request.evidence)
        if mode != "progress" and not gate.lexical_ids:
            return insufficient(reason + "；现有命中仅为弱相关或待语义判断的候选，不能当作问题依据。", local=True)
        lines = [f"本地证据摘录（{reason}；不是AI总结）。"]
        missing = []
        if mode == "progress":
            lines.append("仅展示服务端目标记录，不判定验收或完成，不自动修改状态。")
            for task in request.tasks or []:
                path = task.get("path") or task["title"]
                lines.append(f"- 目标记录 {task['id']}：{path}；记录状态={task['status']}；期限={task['due_date'] or '未记录'}")
            if not request.tasks:
                return insufficient("没有可读取的目标记录，不能据此推断进度。", local=True)
            missing.append("目标记录不能证明实际工作完成或验收情况")
        else:
            for key, item in request.evidence.items():
                if key in gate.lexical_ids:
                    number = item.get("number")
                    label = f" · v{number}" if type(number) is int else ""
                    lines.append(f"\n来源《{item['title']}》{label}：\n{item['content']}")
            missing.append("片段与问题的语义支持关系、未覆盖的子问题及适用条件尚未判定")
            if mode == "align":
                lines.append("以上仅逐来源列示原文，未生成差异判断或共识。")
                if len(gate.lexical_ids) < 2 or len({request.evidence[key]['document_id'] for key in gate.lexical_ids}) < 2:
                    missing.append("至少两份独立且相关的来源版本，当前不能作确定对比")
        return AnswerOutcome("\n".join(lines), "partial", gate.lexical_ids,
                             ["仅展示主题词匹配的本地原文；词法匹配不保证语义支持，摘录可能截断，不能视为完整回答。"],
                             missing, ["add_knowledge", "rephrase_question"])

    async def answer(self, actor_id: str, space_id: str, conversation_id: str,
                     question: str, mode: str = "qa", *, event_sink: EventSink | None = None,
                     authorization_check=None, commit_guard=None, message_id=None,
                     run_id=None, request_id=None, save_authorization_check=None) -> dict:
        sink_error: BaseException | None = None

        async def emit(kind: str, **data) -> None:
            nonlocal sink_error
            if event_sink is not None:
                await request.check()
                try:
                    await event_sink({"type": kind, **data})
                except BaseException as exc:
                    sink_error = exc
                    raise

        if mode not in ROUTES:
            raise DomainError(422, "mode must be qa, align or progress")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_LENGTH:
            raise DomainError(422, "question must contain 1-8000 characters")
        request = _Request(self.service, actor_id, space_id, conversation_id)
        request.authorization_check = authorization_check
        request.commit_guard = commit_guard
        request.event_sink = event_sink
        request.question = question
        answer_kind = question_source(question, mode)
        await request.conversation_check()
        if mode != "progress":
            await request.load_history()
        if (answer_kind == 'grounded' and mode == 'qa'
                and question.strip(' ?？。!！') in {'继续', '详细一点', '展开说说', 'follow up'}
                and request.history and request.history[-1].get('source_kind') == 'general'):
            answer_kind = 'general'
        # No saved SDK sessions/hints; authorized bounded history is plain data.
        await request.check()
        user_parameters = inspect.signature(self.service.append_message).parameters
        user_options = {}
        if "authorization_check" in user_parameters:
            user_options = {"authorization_check": save_authorization_check, "expected_epoch": request._epoch}
        await _call(self.service, "append_message", actor_id, conversation_id, "user", question, [], **user_options)
        if mode == "progress":
            await request.emit_retrieval({"type": "retrieval.skipped", "reason": "progress_mode"})
            await request.emit_retrieval({"type": "rerank.skipped", "reason": "progress_mode"})
            await request.list_tasks()
        elif answer_kind != 'general':
            if not await request.document_overview():
                await request.search(question[:MAX_SEARCH_LENGTH])
        else:
            await request.emit_retrieval({'type': 'retrieval.skipped', 'reason': 'general_question'})
        gate = assess_topic(question, request.evidence)
        candidates = bool(request.tasks) if mode == "progress" else bool(gate.candidate_ids)
        if answer_kind == 'auto':
            answer_kind = 'grounded' if candidates else 'general'
        events = [{"type": "agent.route", "agent": "ResearchCoordinator", "specialist": ROUTES[mode], "mode": mode}]
        fallback = True
        reason = "未配置LLM_API_KEY"
        outcome = None
        state = None
        if answer_kind == 'general':
            if self.runtime is None:
                outcome = AnswerOutcome('当前未配置可用的模型，暂时无法生成通用回答。知识库未命中不是拒答原因；请配置模型服务后重试。',
                                        'insufficient', limitations=['未调用模型，不将本地摘录冒充通用回答。'])
            else:
                try:
                    state = self.runtime.new_request_state()
                    agent = conversational_agent('general')
                    payload = {'question': question, 'mode': 'qa', 'history': request.history,
                               'source_policy': 'general_only'}
                    await emit('agent.started', agent='ResearchCoordinator')
                    stream = getattr(self.runtime, 'stream_coordinator', None)
                    if event_sink is not None and callable(stream):
                        raw, processed = '', 0
                        iterator = stream(agent, payload, check=request.check, state=state)
                        try:
                            async for fragment in iterator:
                                if not isinstance(fragment, str) or len(raw) + len(fragment) > 100000:
                                    raise ValueError('Conversational stream exceeds limit')
                                raw += fragment
                                claims = completed_stream_claims(raw)
                                for claim in claims[processed:]:
                                    try:
                                        preview = render_conversation(json.dumps({'claims': [claim]}, ensure_ascii=False))
                                    except (ValueError, TypeError):
                                        continue
                                    await emit('answer.delta', text=preview.answer + '\n', version_ids=[])
                                processed = len(claims)
                        finally:
                            # Cancellation must close the provider stream; otherwise
                            # the upstream HTTP response stays open until GC.
                            close = getattr(iterator, 'aclose', None)
                            if close is not None:
                                await close()
                    else:
                        raw = await self.runtime.run_coordinator(agent, payload, check=request.check, state=state)
                    outcome = render_conversation(raw)
                    fallback = False
                    await emit('agent.completed', agent='ResearchCoordinator')
                except Exception as exc:
                    if sink_error is not None:
                        raise sink_error
                    if request.authorization_error is not None:
                        raise request.authorization_error
                    if isinstance(exc, (DomainError, PermissionError)):
                        raise
                    outcome = AnswerOutcome('本次通用回答生成失败，请检查模型服务配置或稍后重试；不是因知识库缺少关键词而拒答。',
                                            'insufficient', limitations=['未生成可用模型答案，不补造内容。'])
                    events.append({'type': 'model.fallback', 'reason': 'model_request_failed'})
        elif not candidates:
            reason = request.overview_notice or ("本次检索未找到可回答当前问题的主题相关候选。" if request.evidence else "本次检索未找到可引用证据。")
            if mode == "progress":
                reason = "没有可读取的目标记录，不能据此推断进度。"
            outcome = insufficient(reason, local=True)
            if request.overview_notice:
                answer_kind = 'clarification'
                outcome.answer = request.overview_notice
            events.append({"type": "answer.insufficient"})
        elif self.runtime is not None:
            stage = 'specialist'
            try:
                agents = describe_agents(request)
                state = self.runtime.new_request_state()
                await request.check()
                context = await request.read_evidence()
                payload = {"mode": mode, "question": question, "history": request.history,
                           "history_notice": request.history_notice,
                           "authorized_evidence": context, "answerability": gate.as_dict(),
                           "document_scope_notice": request.overview_notice}
                events.append({"type": "agent.started", "agent": ROUTES[mode]})
                await emit("agent.started", agent=ROUTES[mode])
                specialist = await self.runtime.run_specialist(
                    agents[ROUTES[mode]], payload, check=request.check, state=state)
                await request.check()
                events.append({"type": "agent.completed", "agent": ROUTES[mode]})
                await emit("agent.completed", agent=ROUTES[mode])
                events.append({"type": "agent.started", "agent": "ResearchCoordinator"})
                await emit("agent.started", agent="ResearchCoordinator")
                coordinator_payload = {**payload, "draft": specialist[:12000],
                                       "authorized_evidence": await request.read_evidence()}
                stage = 'coordinator'
                stream = getattr(self.runtime, "stream_coordinator", None)
                if event_sink is not None and callable(stream):
                    coordinator, processed = "", 0
                    iterator = stream(agents["ResearchCoordinator"], coordinator_payload,
                                      check=request.check, state=state)
                    try:
                        # No per-fragment revalidation: the gateway already
                        # revalidates at a bounded cadence and the SSE reader
                        # beneath it does the same at lower cost.
                        async for fragment in iterator:
                            if not isinstance(fragment, str) or len(coordinator) + len(fragment) > 100000:
                                raise ValueError("Coordinator stream exceeds output limit")
                            coordinator += fragment
                            claims = completed_stream_claims(coordinator)
                            for claim in claims[processed:]:
                                # Identical conservative renderer to the final path.
                                # A complete claim does not grant full-answer sufficiency.
                                try:
                                    preview = self._render(json.dumps({"answer_status": "partial", "claims": [claim],
                                            "limitations": [], "missing_evidence": []}), request, mode)
                                except (ValueError, TypeError, KeyError):
                                    continue
                                if preview.status != "insufficient":
                                    await emit("answer.delta", text=add_disclosures(preview) + "\n",
                                               version_ids=sorted(preview.citation_ids))
                            processed = len(claims)
                    finally:
                        close = getattr(iterator, "aclose", None)
                        if close is not None:
                            await close()
                else:
                    coordinator = await self.runtime.run_coordinator(
                        agents["ResearchCoordinator"], coordinator_payload, check=request.check, state=state)
                # Reauthorization precedes rendering as well as persistence/return.
                await request.check()
                stage = 'validation'
                outcome = self._render(coordinator, request, mode)
                fallback = False
                events.append({"type": "agent.completed", "agent": "ResearchCoordinator"})
                await emit("agent.completed", agent="ResearchCoordinator")
            except Exception as exc:
                if sink_error is not None:
                    raise sink_error
                # Wrapped SDK tool authorization failures must never become a
                # successful local fallback. Model metadata grants no authority.
                if request.authorization_error is not None:
                    raise request.authorization_error
                if isinstance(exc, (DomainError, PermissionError)):
                    raise
                if stage == 'validation':
                    category, detail = 'answer_validation_failed', '模型已返回内容，但结构或引用校验未通过'
                elif isinstance(exc, TimeoutError):
                    category, detail = 'model_timeout', '模型调用超时，请检查模型服务及超时配置'
                else:
                    category, detail = 'model_request_failed', '模型服务请求或流式响应未成功，请检查服务配置'
                reason = '模型不可用或输出缺少有效证据；' + detail + '（阶段：' + stage + '）'
                events.append({"type": "model.fallback", "reason": category})
                await emit('model.fallback', reason=category)
        if outcome is None:
            outcome = self._fallback(request, mode, reason)
            events.append({"type": "answer.local_excerpts" if outcome.citation_ids or mode == "progress" else "answer.insufficient"})
        if answer_kind == 'mixed' and self.runtime is not None:
            try:
                if state is None:
                    state = self.runtime.new_request_state()
                await request.check()
                raw = await self.runtime.run_coordinator(conversational_agent('mixed'),
                    {'question': question, 'mode': 'qa', 'source_policy': 'suggestions_only',
                     'authorized_evidence': await request.read_evidence(),
                     'grounded_answer': outcome.answer},
                    check=request.check, state=state)
                advice = render_conversation(raw)
                outcome.answer = '【材料可确认部分】\n' + outcome.answer + '\n\n' + advice.answer
                outcome.limitations.extend(advice.limitations)
                outcome.status = 'partial'
                fallback = False
            except Exception as exc:
                if request.authorization_error is not None:
                    raise request.authorization_error
                if isinstance(exc, (DomainError, PermissionError)):
                    raise
                outcome.limitations.append('通用建议生成失败；仅保留材料处理结果，不把建议伪装为材料事实。')
        if request.overview_notice and outcome.status != 'insufficient':
            outcome.limitations.append(request.overview_notice)
            outcome.status = 'partial'
        output = add_disclosures(outcome)
        if request.history_notice:
            output += "\n" + request.history_notice
            events.append({"type": "history.bounded", "max_messages": 12, "max_chars": 12000})
        await request.check()
        events.extend(request.retrieval_events)
        citations = [{"version_id": item["version_id"], "document_id": item["document_id"],
                      "title": item["title"], "source": item["source"], "excerpt": item["content"],
                      **{key: item[key] for key in ("chunk_id", "chunk_start", "chunk_end", "retrieval_strategy", "rerank_applied") if key in item}}
                     for key, item in request.evidence.items() if key in outcome.citation_ids and outcome.status != "insufficient"]
        metadata = normalize_answer_metadata({"answer_status": outcome.status, 'answer_kind': answer_kind,
                    "missing_evidence": outcome.missing_evidence[:10],
                    "suggested_actions": outcome.suggested_actions, "fallback": fallback,
                    "mode": mode, "events": events[:100],
                    **{key: value for key, value in (("message_id", message_id), ("run_id", run_id),
                                                      ("request_id", request_id)) if value is not None}})
        # Compatibility with existing five-argument adapters is signature-based,
        # never an exception fallback that could swallow authorization failures.
        parameters = inspect.signature(self.service.append_message).parameters
        supports_metadata = "metadata" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        kwargs = {"metadata": metadata} if supports_metadata else {}
        def validate_save(repository):
            if save_authorization_check is not None:
                save_authorization_check(repository)
            request.save_check(repository)

        options = {"message_id": message_id, "commit_guard": commit_guard,
                   "authorization_check": validate_save, "expected_epoch": request._epoch}
        kwargs.update({key: value for key, value in options.items() if key in parameters})
        result = {"answer": output, "citations": citations, "events": events,
                "conversation_id": conversation_id, "mode": mode, "fallback": fallback,
                "answer_status": outcome.status, 'answer_kind': answer_kind, "limitations": outcome.limitations,
                "missing_evidence": outcome.missing_evidence[:10],
                "suggested_actions": outcome.suggested_actions,
                "evidence_scope": "current_request_only",
                **{key: value for key, value in (("message_id", message_id), ("run_id", run_id),
                                                  ("request_id", request_id)) if value is not None}}
        if state is not None:
            result["_meta"] = {
                "total_tokens": getattr(state, "total_tokens", 0),
                "prompt_tokens": getattr(state, "prompt_tokens", 0),
                "completion_tokens": getattr(state, "completion_tokens", 0),
                "trace_id": getattr(state, "trace_id", ""),
            }
        if commit_guard is not None:
            commit_guard.check()
            # Set before the worker can commit: cancellation after COMMIT may
            # recover this exact canonical answer without executing again.
            commit_guard.result = result
        await _call(self.service, "append_message", actor_id, conversation_id, "assistant", output, citations, **kwargs)
        if commit_guard is None or not commit_guard.committed:
            await request.check()
        return result


async def answer(service: ResearchService, actor_id: str, space_id: str, conversation_id: str,
                 question: str, mode: str = "qa", *, runtime: ResearchRuntime | None = None) -> dict:
    return await ResearchAssistant(service, runtime).answer(actor_id, space_id, conversation_id, question, mode)
