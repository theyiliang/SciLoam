"""Request-local evidence ledger and authorization checks."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from typing import Any

from app.domain.errors import DomainError
from app.domain.ai_ports import ResearchService
from .policies import MAX_HISTORY_LENGTH, MAX_EVIDENCE, MAX_EXCERPT_LENGTH, MAX_SEARCH_LENGTH

async def _call(service: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Accept the synchronous service and asynchronous test/adaptor services."""
    function = getattr(service, method)
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    value = await asyncio.to_thread(function, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


class _Request:
    def __init__(self, service: ResearchService, actor: str, space: str, conversation: str):
        self.service, self.actor, self.space, self.conversation = service, actor, space, conversation
        self.evidence: dict[str, dict] = {}  # Current retrieval only; history never consumes this budget.
        self.history_evidence: dict[str, dict] = {}
        self.tasks: list[dict] | None = None
        self.authorization_error: Exception | None = None
        self.version_hashes: dict[str, str] = {}
        self.history: list[dict] = []
        self.history_notice = ""
        self.retrieval_events: list[dict] = []
        self._epoch: Any = None
        self._epoch_bound = False

    async def load_history(self) -> None:
        """Only authorized same-epoch, cited assistant bodies can enter context.

        Legacy uncited assistant prose is intentionally not reused: there is no
        provenance to reauthorize if it contained formerly private evidence.
        """
        conversation = await self.conversation_check()
        remaining = MAX_HISTORY_LENGTH
        selected = []
        for message in reversed(conversation.get("messages", [])[-12:]):
            role = message.get("role")
            citations = message.get("citations") or []
            if role not in ("user", "assistant") or (role == "assistant" and not citations):
                continue
            ids = []
            for citation in citations:
                version_id = citation.get("version_id") if isinstance(citation, dict) else citation
                if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
                    raise DomainError(403, "历史记录来源校验未通过")
                if version_id not in self.history_evidence and len(self.history_evidence) >= MAX_EVIDENCE:
                    break
                value = await self.version(version_id)
                self.history_evidence[version_id] = value
                ids.append(version_id)
            else:
                body = str(message.get("content", ""))[-min(remaining, 3000):]
                if body:
                    selected.append({"role": role, "content": body, "version_ids": ids})
                    remaining -= len(body)
                if remaining <= 0:
                    break
                continue
            # A partially authorized history entry is never forwarded.
            continue
        self.history = list(reversed(selected))
        if len(selected) < len(conversation.get("messages", [])) or any(
            len(str(message.get("content", ""))) > 3000
            for message in conversation.get("messages", [])[-12:]
        ):
            self.history_notice = "历史上下文仅保留最近12条、总计最多12000字符的可复核片段；部分记录已省略或截断，未生成研究结论记忆。"

    async def conversation_check(self) -> dict:
        try:
            value = await _call(self.service, "get_conversation", self.actor, self.conversation)
            if value.get("space_id") != self.space:
                raise DomainError(403, "该会话不属于当前知识库")
            epoch = value.get("space_epoch", value.get("epoch"))
            if self._epoch_bound and epoch != self._epoch:
                raise DomainError(409, "会话已更新，请重新提问")
            self._epoch, self._epoch_bound = epoch, True
            return value
        except Exception as exc:
            self.authorization_error = exc
            raise

    async def version(self, version_id: str, *, snippet: str | None = None, query: str = "") -> dict:
        try:
            value = await _call(self.service, "get_version", self.actor, version_id)
            if value.get("space_id") != self.space:
                raise DomainError(403, "依据不属于当前会话的知识库")
            if value.get("index_status", "ready") != "ready":
                raise DomainError(409, "依据尚未就绪，请重新提问")
            fingerprint = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
            if version_id in self.version_hashes and self.version_hashes[version_id] != fingerprint:
                raise DomainError(409, "依据已变化，请重新提问")
            self.version_hashes[version_id] = fingerprint
            body = str(value.get("content", ""))
            if isinstance(snippet, str) and snippet and snippet in body:
                excerpt = snippet[:MAX_EXCERPT_LENGTH]
            else:
                position = body.casefold().find(query.casefold()) if query else 0
                start = max(0, position - 160)
                excerpt = body[start:start + MAX_EXCERPT_LENGTH]
            return {
                "version_id": version_id,
                "document_id": value.get("document_id", ""),
                "title": str(value.get("title", ""))[:500],
                "source": str(value.get("source", ""))[:1000],
                "content": excerpt,
                "space_id": self.space,
            }
        except Exception as exc:
            self.authorization_error = exc
            raise

    async def read_version(self, version_id: str) -> dict:
        await self.conversation_check()
        if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
            raise ValueError("Invalid version id")
        # A model may read only IDs discovered by the server-bound search.
        if version_id not in self.evidence:
            raise ValueError("Version was not found in this request's search")
        await self.version(version_id)
        return self.evidence[version_id]

    async def search(self, query: str) -> list[dict]:
        await self.conversation_check()
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_LENGTH:
            raise ValueError("Search query must contain 1-500 characters")
        rows = await _call(self.service, "search", self.actor, self.space, query, limit=5)
        self.retrieval_events.append({
            "type": "retrieval.completed", "hit_count": len(rows),
            "strategies": sorted({row.get("retrieval_strategy", "legacy_lexical") for row in rows}),
            "rerank_applied": any(row.get("rerank_applied", False) for row in rows),
            "degraded": any(row.get("retrieval_degraded", False) for row in rows),
            "reasons": sorted({reason for row in rows for reason in row.get("degradation_reasons", [])}),
        })
        found = []
        for row in rows[:5]:
            version_id = row.get("version_id")
            if not isinstance(version_id, str) or not version_id:
                continue
            if version_id not in self.evidence and len(self.evidence) >= MAX_EVIDENCE:
                break
            # The search fragment must occur verbatim in reauthorized content.
            value = await self.version(version_id, snippet=row.get("content") or row.get("snippet"), query=query)
            for key in ("chunk_id", "chunk_start", "chunk_end", "generation", "retrieval_strategy", "rerank_applied"):
                if key in row:
                    value[key] = row[key]
            # A fresh query wins even for the same immutable version: its
            # relevant paragraph may differ from a previous query/history hit.
            self.evidence[version_id] = value
            found.append(value)
        return found

    async def task_snapshot(self) -> list[dict]:
        rows = await _call(self.service, "list_tasks", self.actor)
        # Task metadata is globally authorized by list_tasks. Never copy progress
        # prose/attachments from other spaces into this conversation's body.
        keys = ("id", "title", "status", "due_date", "assignee_id")
        return [{key: row.get(key) for key in keys} for row in rows[:100]]

    async def list_tasks(self) -> list[dict]:
        await self.conversation_check()
        self.tasks = await self.task_snapshot()
        return self.tasks

    async def check(self) -> None:
        if self.authorization_error is not None:
            raise self.authorization_error
        await self.conversation_check()
        for version_id in self.history_evidence.keys() | self.evidence.keys():
            await self.version(version_id)  # checks the full immutable-version fingerprint
        if self.tasks is not None and await self.task_snapshot() != self.tasks:
            raise DomainError(409, "任务记录已变化，请重新提问")

    async def read_evidence(self) -> dict:
        await self.check()
        return {"evidence": list(self.evidence.values()), "tasks": self.tasks or []}


