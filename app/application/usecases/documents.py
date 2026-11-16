"""Immutable document snapshots, explicit sharing, withdrawal and retrieval."""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.models import Document, Space, User, Version


class DocumentUseCases:
    def list_documents(self, actor: str, space_id: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            self._space(repository, actor, space_id)
            return [self._document(repository, actor, row["id"]) for row in repository.list("documents", space_id=space_id) if not repository.get("document_withdrawals", row["id"])]

    def add_document(self, actor: str, space_id: str, title: str, content: str, filename: str = "", source: str = "", *, import_report: dict | None = None, structure_map: dict | None = None) -> dict:
        title = policies.text(title, "document title", 500).strip()
        policies.body(content, filename, source)
        with self._uow_factory() as uow:
            repository = uow.repository
            self._space(repository, actor, space_id, write=True)
            document_id, version_id = self._insert_document(repository, actor, space_id, title, content, filename, source)
            if import_report is not None:
                repository.add("knowledge_imports", {"version_id": version_id,
                               "report": import_report, "structure_map": structure_map or {},
                               "created_at": self._clock()})
            return self._document(repository, actor, document_id)

    def withdraw_document(self, actor: str, document_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            row = repository.get("documents", document_id)
            if not row:
                raise DomainError(404, "未找到该文档，请刷新列表")
            space = self._space(repository, actor, row["space_id"])
            Document.from_record(row).require_withdrawal(user, Space.from_record(space), space["role"])
            existing = repository.get("document_withdrawals", document_id)
            if existing:
                return existing
            withdrawal = {"document_id": document_id, "withdrawn_by": actor, "withdrawn_at": self._clock()}
            repository.add("document_withdrawals", withdrawal)
            self._bump_epoch(repository, space["id"])
            self._event(repository, actor, "document_withdrawn", "document", document_id)
            return withdrawal

    def delete_version(self, actor: str, version_id: str, replacement_version_id: str | None = None, reason: str = "") -> dict:
        return self._change_version_lifecycle(actor, version_id, "deleted", reason, replacement_version_id)

    def withdraw_version(self, actor: str, version_id: str, reason: str) -> dict:
        return self._change_version_lifecycle(actor, version_id, "withdrawn", reason)

    def _change_version_lifecycle(self, actor, version_id, status, reason, replacement_version_id=None):
        reason = policies.text(reason, "lifecycle reason", 2000, empty=True).strip()
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            row = repository.version_details(version_id)
            if not row:
                raise DomainError(404, "未找到该版本，请刷新列表")
            space = self._space(repository, actor, row["space_id"])
            version = Version.from_record(row)
            version.require_lifecycle_change(user, Space.from_record(space))
            # Lifecycle receipts are immutable. Retrying cannot rewrite the reason
            # or actor, and cannot turn a withdrawn protected version into deleted.
            existing = repository.get("version_lifecycle", version_id)
            if existing:
                if existing["status"] == status:
                    return existing
                raise DomainError(409, "该版本已处于最终状态，不能再变更")
            self._not_withdrawn(repository, version.document_id)
            if status == "deleted":
                if space["kind"] == "results":
                    raise DomainError(409, "已发布的成果不能删除，请改为撤回发布")
                version.require_deletion(bool(repository.version_references(version_id)))
            head = repository.document_active_head(version.document_id)
            if head is None:
                raise DomainError(410, "该文档没有可阅读的正式版本")
            is_current = head["id"] == version_id
            if is_current or replacement_version_id is not None:
                if replacement_version_id is None:
                    version.require_replacement(None)
                replacement = self._version(repository, actor, replacement_version_id)
                version.require_replacement(Version.from_record(replacement))
                if replacement.get("index_status") != "ready":
                    raise DomainError(409, "替换版本尚未就绪，请稍后重试")
                if is_current:
                    repository.set_document_head(version.document_id, replacement_version_id)
            receipt = {"version_id": version_id, "status": status, "reason": reason, "actor_id": actor, "created_at": self._clock()}
            repository.add("version_lifecycle", receipt)
            self._bump_epoch(repository, space["id"])
            self._event(repository, actor, "version_" + status, "version", version_id)
            return receipt

    def add_version(self, actor: str, document_id: str, content: str, filename: str = "", source: str = "") -> dict:
        policies.body(content, filename, source)
        with self._uow_factory() as uow:
            repository = uow.repository
            document = self._document(repository, actor, document_id)
            self._space(repository, actor, document["space_id"], write=True)
            version_id = self._insert_version(repository, actor, document_id, content, filename, source)
            return self._version(repository, actor, version_id)

    def get_version(self, actor: str, version_id: str) -> dict:
        with self._uow_factory() as uow:
            return self._version(uow.repository, actor, version_id)

    def share_version(self, actor: str, version_id: str, target_space_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            version = self._version(repository, actor, version_id)
            source = self._space(repository, actor, version["space_id"])
            target = self._space(repository, actor, target_space_id, write=True)
            policies.share(source, target)
            document_id, copied_id = self._insert_document(repository, actor, target_space_id, version["title"], version["content"], version["filename"], version["source"], version_id)
            self._event(repository, actor, "version_shared", "version", copied_id)
            return self._document(repository, actor, document_id)

    def search(self, actor: str, space_id: str, query: str, limit: int = 5) -> list[dict]:
        policies.text(query, "query", 500)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise DomainError(422, "检索条数需在 1 到 50 之间")
        with self._uow_factory() as uow:
            space = self._space(uow.repository, actor, space_id)
            epoch = space["epoch"]
            rows = uow.repository.searchable_versions(space_id)
        if self._retrieval is None:
            return policies.rank_evidence(rows, space_id, query, limit)

        def authorize():
            # Never hold a SQLite write transaction during network/model calls.
            with self._uow_factory() as current:
                live_space = self._space(current.repository, actor, space_id)
                if live_space["epoch"] != epoch:
                    raise DomainError(409, "空间权限已变化，请重新检索")
                return current.repository.searchable_versions(space_id)

        return self._retrieval.search(space_id, query, rows, limit, authorize=authorize)
