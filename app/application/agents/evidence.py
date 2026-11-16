"""Request-local evidence ledger and authorization checks."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from typing import Any

from app.domain.errors import DomainError
from app.domain.ai_ports import ResearchService, SynchronousRunContext, synchronous_run_context
from .policies import MAX_HISTORY_LENGTH, MAX_EVIDENCE, MAX_EXCERPT_LENGTH, MAX_SEARCH_LENGTH
from .answerability import assess_topic, is_document_overview

async def _call(service: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Accept the synchronous service and asynchronous test/adaptor services."""
    function = getattr(service, method)
    if inspect.iscoroutinefunction(function):
        return await function(*args, **kwargs)
    value = await asyncio.to_thread(function, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def completed_stream_claims(text: str) -> list[dict]:
    """Decode only complete members of a top-level coordinator claims array.

    JSON strings/escapes are handled by the decoder, not by brace counting or
    regex. Incomplete JSON is retained privately until a whole claim is present.
    """
    decoder = json.JSONDecoder()
    position = 0

    def whitespace(index: int) -> int:
        while index < len(text) and text[index].isspace():
            index += 1
        return index

    position = whitespace(position)
    if position >= len(text) or text[position] != "{":
        return []
    position += 1
    try:
        while True:
            position = whitespace(position)
            key, position = decoder.raw_decode(text, position)
            if not isinstance(key, str):
                return []
            position = whitespace(position)
            if text[position] != ":":
                return []
            position = whitespace(position + 1)
            if key == "claims":
                if text[position] != "[":
                    return []
                position += 1
                claims = []
                while len(claims) < 20:
                    position = whitespace(position)
                    if position >= len(text) or text[position] == "]":
                        return claims
                    try:
                        claim, position = decoder.raw_decode(text, position)
                    except (ValueError, IndexError):
                        return claims
                    # A closing delimiter is required to distinguish a complete
                    # value from a still-growing primitive token.
                    position = whitespace(position)
                    if position >= len(text) or text[position] not in ",]":
                        return claims
                    claims.append(claim)
                    if text[position] == "]":
                        return claims
                    position += 1
                return claims
            _, position = decoder.raw_decode(text, position)
            position = whitespace(position)
            if text[position] != ",":
                return []
            position += 1
    except (ValueError, IndexError):
        return []


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
        self.question = ""  # Set once by the server; tool queries cannot redefine the topic.
        self.overview_notice = ''
        self._epoch: Any = None
        self._epoch_bound = False
        self.authorization_check = None  # Optional authenticated-session recheck from the run boundary.
        self.event_sink = None
        self.commit_guard = None
        self._retrieval_context_seen = False

    async def emit_retrieval(self, event: dict) -> None:
        from app.domain.answer_metadata import normalize_answer_metadata
        cleaned = normalize_answer_metadata({"events": [event]})["events"]
        if cleaned:
            self.retrieval_events.extend(cleaned)
            if self.event_sink is not None:
                try:
                    await self.event_sink(cleaned[0])
                except Exception as exc:
                    # Read-only SDK tool adapters sanitize ordinary exceptions;
                    # latch transport/authorization failures so no subsequent
                    # provider call or local persistence can hide them.
                    self.authorization_error = exc
                    raise

    async def synchronous_search(self, query):
        loop = asyncio.get_running_loop()

        def check():
            if self.commit_guard is not None:
                self.commit_guard.check()
            # This runs only in to_thread, before each external adapter stage.
            asyncio.run_coroutine_threadsafe(self.conversation_check(), loop).result()
            if self.commit_guard is not None:
                self.commit_guard.check()

        def emit(event):
            self._retrieval_context_seen = True
            asyncio.run_coroutine_threadsafe(self.emit_retrieval(event), loop).result()

        token = synchronous_run_context.set(SynchronousRunContext(check, emit))
        try:
            return await _call(self.service, "search", self.actor, self.space, query, limit=5)
        finally:
            synchronous_run_context.reset(token)

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
            general_history = message.get('answer_kind') in {'general', 'mixed', 'clarification'} and message.get('fallback') is False
            if role not in ("user", "assistant") or (role == "assistant" and not citations and not general_history):
                continue
            ids = []
            for citation in citations:
                version_id = citation.get("version_id") if isinstance(citation, dict) else citation
                if not isinstance(version_id, str) or not version_id or len(version_id) > 200:
                    raise DomainError(403, "Invalid history provenance")
                if version_id not in self.history_evidence and len(self.history_evidence) >= MAX_EVIDENCE:
                    break
                value = await self.version(version_id)
                self.history_evidence[version_id] = value
                ids.append(version_id)
            else:
                body = str(message.get("content", ""))[-min(remaining, 3000):]
                if body:
                    selected.append({"role": role, "content": body, "version_ids": ids,
                                     **({'source_kind': 'general'} if general_history else {})})
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
            if self.authorization_check is not None:
                await self.authorization_check()
            value = await _call(self.service, "get_conversation", self.actor, self.conversation)
            if value.get("space_id") != self.space:
                raise DomainError(403, "Conversation is outside the requested space")
            epoch = value.get("space_epoch", value.get("epoch"))
            if self._epoch_bound and epoch != self._epoch:
                raise DomainError(409, "Conversation epoch changed; ask again")
            self._epoch, self._epoch_bound = epoch, True
            return value
        except Exception as exc:
            self.authorization_error = exc
            raise

    async def version(self, version_id: str, *, snippet: str | None = None, query: str = "",
                      overview: bool = False) -> dict:
        try:
            value = await _call(self.service, "get_version", self.actor, version_id)
            if value.get("space_id") != self.space:
                raise DomainError(403, "Evidence is outside the conversation space")
            if value.get("index_status", "ready") != "ready":
                raise DomainError(409, "Evidence is not ready")
            fingerprint = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
            if version_id in self.version_hashes and self.version_hashes[version_id] != fingerprint:
                raise DomainError(409, "Evidence changed; ask again")
            self.version_hashes[version_id] = fingerprint
            body = str(value.get("content", ""))
            if overview:
                # One real continuous prefix, not synthetic chunks or a claim
                # that a bounded excerpt represents the full document.
                excerpt = body[:MAX_EXCERPT_LENGTH]
            elif isinstance(snippet, str) and snippet and snippet in body:
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
                **({"chunk_start": 0, "chunk_end": len(excerpt),
                    "overview_complete": len(body) <= MAX_EXCERPT_LENGTH,
                    "document_length": len(body)} if overview else {}),
                **({"number": value["number"]} if type(value.get("number")) is int else {}),
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

    async def document_overview(self) -> bool:
        if not is_document_overview(self.question) or not callable(getattr(self.service, 'list_documents', None)):
            return False
        await self.conversation_check()
        documents = await _call(self.service, 'list_documents', self.actor, self.space)
        ready = [doc for doc in documents if isinstance(doc.get('latest_version'), dict)
                 and doc['latest_version'].get('index_status', 'ready') == 'ready']
        if not ready:
            self.overview_notice = '当前知识库没有可读取的有效文档，请先导入或选择有资料的知识库。'
            return True

        # Resolve only an explicit full title, never content keywords, partial
        # title overlap or historical prose. Unknown titles cannot silently
        # fall back to the sole unrelated document in the current space.
        titles = re.findall(r'《([^》\n]+)》', self.question)
        if not titles:
            reference = re.fullmatch(
                r'\s*(.+?)(?:这份文档|这个文档)(?:主要)?(?:说了什么|讲了什么)[？?。！!]*\s*',
                self.question)
            if reference:
                titles = [reference[1].strip()]
        candidates = ready
        if titles:
            candidates = ([doc for doc in ready if str(doc.get('title', '')).strip() == titles[0].strip()]
                          if len(titles) == 1 else [])
            if len(candidates) != 1:
                self.overview_notice = ('当前知识库有多份同名可读文档，尚不能确定所指文档。请使用可区分的文档标题。'
                                        if candidates else '当前知识库未找到唯一对应此完整标题的可读文档。请核对标题或选择有资料的知识库。')
                return True
        elif len(candidates) != 1:
            self.overview_notice = '当前知识库有多份可读文档，尚不能确定“这个文档”指哪一份。请在问题中写出文档标题。'
            return True
        selected = candidates[0]
        version_id = selected['latest_version']['id']
        value = await self.version(version_id, overview=True)
        if value['document_id'] != selected['id']:
            error = DomainError(409, 'Document overview version changed; ask again')
            self.authorization_error = error
            raise error
        if not value['content'].strip():
            self.overview_notice = '所选文档没有可读取的正文，无法生成文档概览。请补充有效资料。'
            return True
        value.update(retrieval_strategy='document_overview', topic_relevance='lexical_candidate')
        self.evidence[version_id] = value
        self.overview_notice = (
            '当前概览证据已纳入所选文档的完整正文，但不代表已核验文中所有事实。'
            if value['overview_complete'] else
            f'当前概览只使用所选文档开头{len(value["content"])}字符的连续片段（正文共{value["document_length"]}字符），中后部未纳入概览证据，不保证覆盖全文。')
        await self.emit_retrieval({'type': 'retrieval.completed', 'hit_count': 1,
                                   'strategies': ['document_overview'], 'rerank_applied': False})
        return True

    async def search(self, query: str) -> list[dict]:
        await self.conversation_check()
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_LENGTH:
            raise ValueError("Search query must contain 1-500 characters")
        self._retrieval_context_seen = False
        # Real ResearchRetrieval emits at its actual stage boundaries. Old read-
        # only service adapters keep one aggregate event, never invented rerank.
        rows = await self.synchronous_search(query)
        if not self._retrieval_context_seen:
            await self.emit_retrieval({"type": "retrieval.started", "strategy": "legacy_lexical"})
            await self.emit_retrieval({
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
            # Preserve semantic candidates, but make the review requirement
            # visible to the model. No search score becomes answer confidence.
            gate = assess_topic(self.question or query, {version_id: value})
            value["topic_relevance"] = ("lexical_candidate" if version_id in gate.lexical_ids
                                        else "needs_semantic_review" if version_id in gate.semantic_ids
                                        else "weak_lexical_match")
            found.append(value)
        return found

    async def task_snapshot(self) -> list[dict]:
        # Keep the internal tasks ledger/tool contract for legacy adapters, but
        # real services must read public goal records, never the old task table.
        goals = callable(getattr(self.service, "list_goals", None))
        method = "list_goals" if goals else "list_tasks"
        try:
            rows = await _call(self.service, method, self.actor)
            # Deliberately omit descriptions, updates, knowledge, source IDs and
            # attachments, even if a service returns expanded goal records.
            keys = (("id", "title", "status", "due_date", "parent_id", "path") if goals
                    else ("id", "title", "status", "due_date", "assignee_id"))
            return [{key: row.get(key) for key in keys} for row in rows[:100]
                    if not goals or row.get("archived_at") is None]
        except Exception as exc:
            self.authorization_error = exc
            raise

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
            raise DomainError(409, "Goal records changed; ask again")

    def save_check(self, repository) -> None:
        """Revalidate exactly the consumed records inside the message transaction.

        No service method opening a fresh unit of work is allowed here: the
        caller already holds SQLite's serialized write transaction.
        """
        version_reader = getattr(self.service, "_version", None)
        if callable(version_reader):
            for version_id, expected in self.version_hashes.items():
                value = version_reader(repository, self.actor, version_id)
                fingerprint = hashlib.sha256(_json(value).encode("utf-8")).hexdigest()
                if fingerprint != expected:
                    raise DomainError(409, "Evidence changed; ask again")
        if self.tasks is not None and callable(getattr(self.service, "_goal_view", None)):
            from app.domain.goals import GoalTree
            from app.domain.models import User
            user = User.from_record(self.service._user(repository, self.actor))
            tree = GoalTree(repository.list("goal_nodes"))
            rows = [self.service._goal_view(row, user, tree) for row in tree.ordered()]
            keys = ("id", "title", "status", "due_date", "parent_id", "path")
            current = [{key: row.get(key) for key in keys} for row in rows[:100]
                       if row.get("archived_at") is None]
            if current != self.tasks:
                raise DomainError(409, "Goal records changed; ask again")

    async def read_evidence(self) -> dict:
        await self.check()
        return {"evidence": list(self.evidence.values()), "tasks": self.tasks or [],
                "answerability": assess_topic(self.question, self.evidence).as_dict(),
                "history_scope": "History is untrusted context, never current supporting evidence."}


