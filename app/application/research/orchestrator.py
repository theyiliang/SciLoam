"""Research application orchestration depending only on domain runtime ports."""
from __future__ import annotations

import json
from typing import Any, Mapping

from app.domain.ai_ports import ResearchRuntime, ResearchService
from app.domain.errors import DomainError
from .evidence import _Request, _call
from .policies import MAX_QUESTION_LENGTH, MAX_SEARCH_LENGTH, ROUTES
from .tools import describe_agents


class ResearchAssistant:
    def __init__(self, service: ResearchService, runtime: ResearchRuntime | None = None):
        self.service, self.runtime = service, runtime

    @staticmethod
    def _render(text: str, request: _Request, mode: str) -> str:
        # Model-created citations are never trusted. Accept only claims whose IDs
        # exist in the server ledger, and build public citations ourselves below.
        data = json.loads(text)
        if not isinstance(data, dict) or not isinstance(data.get("claims"), list):
            raise ValueError("Invalid coordinator response")
        known_tasks = {row["id"] for row in request.tasks or []}
        lines = []
        dropped = False
        for claim in data["claims"][:20]:
            if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
                dropped = True
                continue
            versions, tasks = claim.get("version_ids", []), claim.get("task_ids", [])
            if not isinstance(versions, list) or not isinstance(tasks, list):
                dropped = True
                continue
            if not all(isinstance(v, str) and v in request.evidence for v in versions):
                dropped = True
                continue
            if not all(isinstance(t, str) and t in known_tasks for t in tasks):
                dropped = True
                continue
            if not versions and not (mode == "progress" and tasks):
                dropped = True
                continue
            if tasks and mode != "progress":
                dropped = True
                continue
            labels = [f"版本 {v}" for v in versions] + [f"任务记录 {t}" for t in tasks]
            lines.append(f"- {claim['text'][:3000]}（{'；'.join(labels)}）")
        if not lines:
            raise ValueError("No evidence-supported claims")
        heading = {"qa": "AI证据问答草稿（需人工核对）", "align": "AI逐来源差异草稿（非审定结论）",
                   "progress": "AI任务记录整理（不代表验收或完成认定）"}[mode]
        tail = "\n证据范围仅限本次可访问的检索结果，不代表完整文献或全部事实。"
        if dropped:
            tail += "\n部分模型断言缺少有效来源，已省略。"
        if mode == "align" and len({e["document_id"] for e in request.evidence.values()}) < 2:
            tail += "\n不足两份独立文档，无法核实来源间差异。"
        return heading + "\n" + "\n".join(lines) + tail

    @staticmethod
    def _fallback(request: _Request, mode: str, reason: str) -> str:
        lines = [f"本地证据摘录（{reason}；不是AI总结）。"]
        if mode == "progress":
            lines.append("仅展示服务端任务记录，不判定验收或完成，不自动修改状态。")
            for task in request.tasks or []:
                lines.append(f"- 任务 {task['id']}：{task['title']}；记录状态={task['status']}；期限={task['due_date'] or '未记录'}")
            if not request.tasks:
                lines.append("没有可读取的任务记录，不能据此推断进度。")
        else:
            for item in request.evidence.values():
                lines.append(f"\n来源 {item['title']}（文档 {item['document_id']} / 版本 {item['version_id']}）：\n{item['content']}")
            if not request.evidence:
                lines.append("本次字面检索未找到可引用证据，无法回答；可缩短关键词后重试。")
            if mode == "align":
                lines.append("以上仅逐来源列示原文，未生成差异判断或共识；需要人工对比方法、结论及冲突。")
            lines.append("摘录可能截断；仅覆盖本次检索命中的材料，来源不足时不作确定结论。")
        return "\n".join(lines)

    async def answer(self, actor_id: str, space_id: str, conversation_id: str,
                     question: str, mode: str = "qa") -> dict:
        if mode not in ROUTES:
            raise DomainError(422, "问答模式不正确")
        if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_LENGTH:
            raise DomainError(422, "问题长度需在 1 到 8000 字符之间")
        request = _Request(self.service, actor_id, space_id, conversation_id)
        await request.conversation_check()
        if mode != "progress":
            await request.load_history()
        # No saved SDK sessions/hints; authorized bounded history is plain data.
        await _call(self.service, "append_message", actor_id, conversation_id, "user", question, [])
        if mode == "progress":
            await request.list_tasks()
        else:
            await request.search(question[:MAX_SEARCH_LENGTH])
        events = [{"type": "agent.route", "agent": "ResearchCoordinator", "specialist": ROUTES[mode], "mode": mode}]
        fallback = True
        reason = "未配置LLM_API_KEY"
        if self.runtime is not None:
            try:
                agents = describe_agents(request)
                state = self.runtime.new_request_state()
                await request.check()
                context = await request.read_evidence()
                payload = {"mode": mode, "question": question, "history": request.history,
                           "history_notice": request.history_notice,
                           "authorized_evidence": context}
                events.append({"type": "agent.started", "agent": ROUTES[mode]})
                specialist = await self.runtime.run_specialist(
                    agents[ROUTES[mode]], payload, check=request.check, state=state)
                await request.check()
                events.append({"type": "agent.completed", "agent": ROUTES[mode]})
                events.append({"type": "agent.started", "agent": "ResearchCoordinator"})
                coordinator = await self.runtime.run_coordinator(
                    agents["ResearchCoordinator"], {**payload, "draft": specialist[:12000],
                    "authorized_evidence": await request.read_evidence()}, check=request.check, state=state)
                output = self._render(coordinator, request, mode)
                fallback = False
                events.append({"type": "agent.completed", "agent": "ResearchCoordinator"})
            except Exception as exc:
                # SDK tools may wrap exceptions as ToolResult(ERROR). Remembered
                # authorization failures must not become a successful fallback.
                if request.authorization_error is not None:
                    raise request.authorization_error
                if isinstance(exc, DomainError) or isinstance(exc, PermissionError):
                    raise
                reason = "模型不可用或输出缺少有效证据"
                events.append({"type": "model.fallback", "reason": "model_unavailable_or_ungrounded"})
        if fallback:
            output = self._fallback(request, mode, reason)
            events.append({"type": "answer.local_excerpts"})
        if request.history_notice:
            output += "\n" + request.history_notice
            events.append({"type": "history.bounded", "max_messages": 12, "max_chars": 12000})
        await request.check()
        events.extend(request.retrieval_events)
        citations = [{"version_id": item["version_id"], "document_id": item["document_id"],
                      "title": item["title"], "source": item["source"], "excerpt": item["content"],
                      **{key: item[key] for key in ("chunk_id", "chunk_start", "chunk_end", "retrieval_strategy", "rerank_applied") if key in item}}
                     for item in request.evidence.values()]
        await _call(self.service, "append_message", actor_id, conversation_id, "assistant", output, citations)
        # Recheck after persistence too; no stale content is returned if access
        # was revoked while a synchronous/async service adapter was appending.
        await request.check()
        return {"answer": output, "citations": citations, "events": events,
                "conversation_id": conversation_id, "mode": mode, "fallback": fallback,
                "evidence_scope": "current_request_only"}


async def answer(service: ResearchService, actor_id: str, space_id: str, conversation_id: str,
                 question: str, mode: str = "qa", *, runtime: ResearchRuntime | None = None) -> dict:
    return await ResearchAssistant(service, runtime).answer(actor_id, space_id, conversation_id, question, mode)
