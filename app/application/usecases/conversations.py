"""Conversation ownership, permission epochs and citation reauthorization."""
from app.domain import policies
from app.domain.answer_metadata import normalize_answer_metadata
from app.domain.errors import DomainError
from app.domain.ai_ports import answer_commit_guard

from .common import new_id


class ConversationUseCases:
    @staticmethod
    def _message_view(message: dict) -> dict:
        result = dict(message)
        metadata = normalize_answer_metadata(result.pop("metadata", None))
        result.update(metadata)
        return result

    def create_conversation(self, actor: str, space_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            space = self._space(repository, actor, space_id)
            conversation_id = new_id()
            repository.add("conversations", {"id": conversation_id, "space_id": space_id, "owner_id": actor, "space_epoch": space["epoch"], "created_at": self._clock()})
            result = self._conversation(repository, actor, conversation_id)
            result["messages"] = []
            return result

    def list_conversations(self, actor: str, space_id: str) -> list[dict]:
        with self._uow_factory() as uow:
            space = self._space(uow.repository, actor, space_id)
            return uow.repository.list("conversations", space_id=space_id, owner_id=actor, space_epoch=space["epoch"])

    def get_conversation(self, actor: str, conversation_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            conversation = self._conversation(repository, actor, conversation_id)
            messages = repository.list("messages", conversation_id=conversation_id)
            for message in messages:
                citations = []
                for citation in message["citations"]:
                    version_id = citation["version_id"]
                    version = self._version(repository, actor, version_id)
                    policies.require_citation_space(version, conversation, reading=True)
                    # Reconstruct display fields only from live authorized records.
                    # ID-only history has no verified historical quote or offset:
                    # never substitute a fresh paragraph and call it an excerpt.
                    visible = {"version_id": version_id, **{
                        key: version[key] for key in ("document_id", "title", "number", "source")
                    }}
                    start, end = citation.get('chunk_start'), citation.get('chunk_end')
                    excerpt = citation.get('excerpt')
                    if (type(start) is int and type(end) is int and 0 <= start <= end <= len(version['content'])
                            and isinstance(excerpt, str) and version['content'][start:end] == excerpt):
                        visible.update(excerpt=excerpt, chunk_start=start, chunk_end=end)
                        if isinstance(citation.get('evidence_id'), str):
                            visible['evidence_id'] = citation['evidence_id']
                    citations.append(visible)
                message["citations"] = citations
            conversation["messages"] = [self._message_view(message) for message in messages]
            safety = getattr(self, '_answer_safety', None)
            if safety is not None:
                for message in conversation['messages']:
                    if message.get('role') != 'assistant':
                        continue
                    message['content'] = safety.output_text(message['content'])
                    visible_citations = []
                    for citation in message['citations']:
                        clean = safety.output_record(citation)
                        if clean.get('excerpt') == citation.get('excerpt'):
                            visible_citations.append(clean)
                    message['citations'] = visible_citations
                    if 'events' in message:
                        message['events'] = safety.output_record(message['events'])
            return conversation

    def append_message(self, actor: str, conversation_id: str, role: str, content: str, citations=None, metadata=None,
                       *, message_id=None, commit_guard=None, authorization_check=None, expected_epoch=None) -> dict:
        if role not in ("user", "assistant"):
            raise DomainError(422, "消息角色只能是 user 或 assistant")
        policies.text(content, "message content", policies.MAX_CONTENT_LENGTH)
        version_ids = policies.citation_ids(citations)
        answer_metadata = normalize_answer_metadata(metadata)
        if message_id is not None and (not isinstance(message_id, str) or not 1 <= len(message_id) <= 200):
            raise DomainError(422, "消息 ID 不正确")
        token = answer_commit_guard.set(commit_guard)
        try:
            with self._uow_factory() as uow:
                repository = uow.repository

                def validate():
                    if authorization_check is not None:
                        authorization_check(repository)
                    conversation = self._conversation(repository, actor, conversation_id)
                    if expected_epoch is not None and conversation.get("space_epoch") != expected_epoch:
                        raise DomainError(409, "会话已更新，请刷新后重试")
                    for version_id in version_ids:
                        policies.require_citation_space(self._version(repository, actor, version_id), conversation)

                validate()
                saved_citations = []
                for citation in citations or []:
                    key = citation if isinstance(citation, str) else citation['version_id']
                    saved = {'version_id': key}
                    if (isinstance(citation, dict) and 'excerpt' in citation
                            and ('chunk_start' in citation or 'chunk_end' in citation)):
                        version = self._version(repository, actor, key)
                        start, end, excerpt = citation.get('chunk_start'), citation.get('chunk_end'), citation['excerpt']
                        if (type(start) is not int or type(end) is not int or not isinstance(excerpt, str)
                                or not 0 <= start <= end <= len(version['content'])
                                or end - start > 6000 or version['content'][start:end] != excerpt):
                            raise DomainError(422, 'Citation range does not reproduce the original text')
                        saved.update(excerpt=excerpt, chunk_start=start, chunk_end=end)
                        if isinstance(citation.get('evidence_id'), str) and len(citation['evidence_id']) <= 200:
                            saved['evidence_id'] = citation['evidence_id']
                    saved_citations.append(saved)
                if commit_guard is not None:
                    commit_guard.check()
                    commit_guard.validate = validate
                message = {"id": message_id or new_id(), "conversation_id": conversation_id,
                           "role": role, "content": content,
                           "citations": saved_citations,
                           "metadata": answer_metadata, "created_at": self._clock()}
                repository.add("messages", message)
            return self._message_view(message)
        finally:
            if commit_guard is not None:
                commit_guard.validate = None
            answer_commit_guard.reset(token)
