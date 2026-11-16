"""Shared authorization and aggregate-loading operations over pure ports."""
from uuid import uuid4

from app.domain.errors import DomainError
from app.domain.models import Conversation, Document, Space, User, Version
from app.domain.ports import Clock, ResearchSecurity, UnitOfWorkFactory


def new_id():
    return uuid4().hex


class ResearchContext:
    def __init__(self, uow_factory: UnitOfWorkFactory, security: ResearchSecurity, clock: Clock, retrieval=None):
        self._uow_factory = uow_factory
        self._security = security
        self._clock = clock
        self._retrieval = retrieval
        self._dummy_password = security.hash_password(security.new_token())

    def _event(self, repository, actor, action, resource_type, resource_id):
        # An explicit allowlist: no contents, titles, email, password or tokens.
        repository.add("audit_events", {"id": new_id(), "actor_id": actor, "action": action, "resource_type": resource_type, "resource_id": resource_id, "created_at": self._clock()})

    @staticmethod
    def _user(repository, actor):
        if not isinstance(actor, str):
            raise DomainError(401, "请先登录")
        row = repository.get("users", actor)
        if not row:
            raise DomainError(401, "请先登录")
        user = User.from_record(row)
        user.require_active()
        return user.public()

    def _owner(self, repository, actor):
        record = self._user(repository, actor)
        User.from_record(record).require_owner()
        return record

    @staticmethod
    def _project(repository):
        return repository.find("projects")

    def _space(self, repository, actor, space_id, write=False, manage=False):
        user = User.from_record(self._user(repository, actor))
        row = repository.get("spaces", space_id)
        if not row:
            raise DomainError(404, "未找到该知识库，请刷新列表")
        if repository.get("space_deletions", space_id):
            raise DomainError(410, "该知识库已删除")
        row["role"] = Space.from_record(row).require_access(user, repository.membership(space_id, actor), write=write, manage=manage)
        return row

    @staticmethod
    def _not_withdrawn(repository, document_id):
        if repository.get("document_withdrawals", document_id):
            raise DomainError(410, "该文档已撤回下架")

    def _version(self, repository, actor, version_id, write=False):
        self._user(repository, actor)
        row = repository.version_details(version_id)
        if not row:
            raise DomainError(404, "未找到该版本，请刷新列表")
        self._space(repository, actor, row["space_id"], write=write)
        self._not_withdrawn(repository, row["document_id"])
        if repository.get("version_lifecycle", version_id):
            raise DomainError(410, "该版本已删除或已撤回")
        imported = repository.get("knowledge_imports", version_id)
        if imported is not None:
            row["import_report"] = imported["report"]
            row["structure_map"] = imported["structure_map"]
        return row

    def _document(self, repository, actor, document_id):
        self._user(repository, actor)
        row = repository.get("documents", document_id)
        if not row:
            raise DomainError(404, "未找到该文档，请刷新列表")
        document = Document.from_record(row)
        self._space(repository, actor, document.space_id)
        self._not_withdrawn(repository, document.id)
        head = repository.document_active_head(document.id)
        if head is None:
            raise DomainError(410, "该文档没有可阅读的正式版本")
        row["versions"] = repository.version_metadata(document.id)
        for version in row["versions"]:
            lifecycle = repository.get("version_lifecycle", version["id"])
            version["lifecycle_status"] = lifecycle["status"] if lifecycle else "active"
            version["protected"] = bool(repository.version_references(version["id"]))
            version["is_current"] = version["id"] == head["id"]
        row["latest_version"] = next((version for version in row["versions"] if version["is_current"]), None)
        if row["latest_version"] is None:
            raise DomainError(410, "该文档没有可阅读的正式版本")
        return row

    def _new_space(self, repository, actor, name, kind, task_id=None):
        space_id = new_id()
        repository.add("spaces", {"id": space_id, "name": name, "kind": kind, "owner_id": actor, "epoch": 1, "task_id": task_id, "created_at": self._clock()})
        repository.set_membership(space_id, actor, "owner")
        return space_id

    @staticmethod
    def _bump_epoch(repository, space_id):
        space = repository.get("spaces", space_id)
        repository.update("spaces", space_id, {"epoch": space["epoch"] + 1})

    def _join(self, repository, user_id):
        for space in repository.list("spaces"):
            if space["kind"] in ("basis", "results"):
                repository.set_membership(space["id"], user_id, "reader")
                self._bump_epoch(repository, space["id"])
        self._new_space(repository, user_id, "个人知识库", "private")

    def _insert_version(self, repository, actor, document_id, content, filename, source, copied_from=None):
        version = Version(new_id(), document_id, repository.next_version_number(document_id), content, filename, source, actor, self._clock(), copied_from)
        repository.add("versions", version.record())
        repository.add("version_indexes", {"version_id": version.id, "status": "ready", "updated_at": version.created_at})
        repository.set_document_head(document_id, version.id)
        self._event(repository, actor, "version_created", "version", version.id)
        return version.id

    def _insert_document(self, repository, actor, space_id, title, content, filename, source, copied_from=None):
        document_id = new_id()
        repository.add("documents", {"id": document_id, "space_id": space_id, "title": title, "created_by": actor, "created_at": self._clock()})
        version_id = self._insert_version(repository, actor, document_id, content, filename, source, copied_from)
        return document_id, version_id

    def _task(self, repository, actor, task_id):
        self._user(repository, actor)
        task = repository.get("tasks", task_id)
        if not task:
            raise DomainError(404, "未找到该任务，请刷新列表")
        return task

    def _conversation(self, repository, actor, conversation_id):
        self._user(repository, actor)
        row = repository.get("conversations", conversation_id)
        if not row:
            raise DomainError(404, "未找到该会话，请刷新列表")
        conversation = Conversation.from_record(row)
        conversation.require_owner(actor)
        space = self._space(repository, actor, row["space_id"])
        conversation.require_epoch(Space.from_record(space))
        return row
