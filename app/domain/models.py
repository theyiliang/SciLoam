"""Immutable research entities. No persistence or framework dependencies."""
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import DomainError

Record = dict[str, Any]


@dataclass(frozen=True)
class User:
    id: str
    email: str
    role: str
    created_at: float
    disabled_at: float | None = None

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "User":
        return cls(**{key: row[key] for key in ("id", "email", "role", "created_at")}, disabled_at=row.get("disabled_at"))

    def public(self) -> Record:
        return {key: getattr(self, key) for key in ("id", "email", "role", "created_at")}

    def require_active(self) -> None:
        if self.disabled_at is not None:
            raise DomainError(401, "请先登录")

    def require_owner(self) -> None:
        self.require_active()
        if self.role != "owner":
            raise DomainError(403, "需要课题负责人权限")


@dataclass(frozen=True)
class Space:
    id: str
    kind: str
    owner_id: str
    epoch: int

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Space":
        return cls(**{key: row[key] for key in ("id", "kind", "owner_id", "epoch")})

    def access_role(self, user: User, membership: str | None) -> str:
        user.require_active()
        if self.kind in ("basis", "results"):
            role = "owner" if user.role == "owner" else "reader" if membership is not None else None
        else:
            role = "owner" if self.owner_id == user.id else membership if membership in ("reader", "editor") else None
        if role is None:
            raise DomainError(403, "没有该知识库的访问权限")
        return role

    def require_access(self, user: User, membership: str | None, *, write=False, manage=False) -> str:
        role = self.access_role(user, membership)
        if manage and (role != "owner" or user.id != self.owner_id):
            raise DomainError(403, "需要知识库所有者权限")
        if write and (role not in ("owner", "editor") or self.kind == "results"):
            raise DomainError(403, "该知识库对你是只读的")
        return role

    def require_grant(self, user_id: str, role: str) -> None:
        if role not in ("reader", "editor"):
            raise DomainError(422, "知识库权限只能授予编辑者或读者")
        if self.owner_id == user_id:
            raise DomainError(409, "知识库所有者角色不能变更")
        if self.kind in ("basis", "results") and role != "reader":
            raise DomainError(403, "系统知识库的成员不能修改")

    def require_revocation(self, user: User) -> None:
        if user.id == self.owner_id or (self.kind in ("basis", "results") and user.role == "owner"):
            raise DomainError(403, "知识库所有者的权限不能被收回")

    def require_deletion(self, user: User) -> None:
        user.require_active()
        if self.kind != "collaboration":
            raise DomainError(403, "系统知识库和默认个人知识库不能删除")
        if self.owner_id != user.id:
            raise DomainError(403, "只有协作知识库所有者可以删除它")


@dataclass(frozen=True)
class Document:
    id: str
    space_id: str
    created_by: str

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Document":
        return cls(**{key: row[key] for key in ("id", "space_id", "created_by")})

    def require_withdrawal(self, user: User, space: Space, role: str) -> None:
        if space.kind == "results":
            if user.role != "owner":
                raise DomainError(403, "只有课题负责人可以撤回已发布的成果")
        elif self.created_by != user.id and role != "owner":
            raise DomainError(403, "只有文档作者或知识库所有者可以撤回文档")


@dataclass(frozen=True)
class Version:
    id: str
    document_id: str
    number: int
    content: str
    filename: str
    source: str
    created_by: str
    created_at: float
    copied_from: str | None = None

    def record(self) -> Record:
        return dict(self.__dict__)

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Version":
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})

    def require_lifecycle_change(self, user: User, space: Space) -> None:
        user.require_active()
        if space.kind == "results":
            if user.role != "owner":
                raise DomainError(403, "只有课题负责人可以撤回已发布的成果")
        elif self.created_by != user.id and space.owner_id != user.id:
            raise DomainError(403, "只有版本作者或知识库所有者可以移除版本")

    def require_deletion(self, protected: bool) -> None:
        if protected:
            raise DomainError(409, "被引用、送审或复制过的版本请改用撤回，而不是删除")

    def require_replacement(self, replacement: "Version | None") -> None:
        if replacement is None:
            raise DomainError(409, "正式版本需要先指定替换版本，或对整个文档执行撤回")
        if replacement.id == self.id or replacement.document_id != self.document_id:
            raise DomainError(409, "替换版本必须是同一文档的另一个正式版本")


@dataclass(frozen=True)
class Review:
    id: str
    submitted_by: str
    status: str
    reviewed_by: str | None
    comment: str

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Review":
        return cls(**{key: row[key] for key in ("id", "submitted_by", "status", "reviewed_by", "comment")})

    def decision(self, actor: str, decision: str, comment: str, authors: set[str]) -> tuple[str, bool]:
        if actor == self.submitted_by or actor in authors:
            raise DomainError(403, "审核人不能审核自己的成果（包括复制版本）")
        status = "approved" if decision == "approve" else "rejected"
        if self.status != "pending":
            if (self.status, self.comment, self.reviewed_by) == (status, comment, actor):
                return status, True
            raise DomainError(409, "这条审核已有结论，不能重复处理")
        return status, False


@dataclass(frozen=True)
class Task:
    id: str
    assignee_id: str | None
    status: str

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Task":
        return cls(**{key: row[key] for key in ("id", "assignee_id", "status")})

    def authorize_edit(self, user: User, metadata: bool) -> None:
        if metadata and user.role != "owner":
            raise DomainError(403, "只有课题负责人可以编辑任务的负责人和元信息")
        if user.role != "owner" and self.assignee_id != user.id:
            raise DomainError(403, "只有任务承办人可以更新任务进展")

    def require_reassignment(self, assignee: str | None) -> None:
        if assignee != self.assignee_id and self.status in ("pending_acceptance", "completed"):
            raise DomainError(409, "待处理或已完成的工作不能改派")

    def transition(self, user: User, target: str) -> str:
        if target == self.status:
            return target
        if self.status == "pending_acceptance" and target in ("completed", "in_progress"):
            if user.role != "owner":
                raise DomainError(403, "只有课题负责人可以验收或退回工作")
            if target == "completed" and user.id == self.assignee_id:
                raise DomainError(403, "任务承办人不能验收自己提交的工作")
        else:
            allowed = {"not_started": {"in_progress"}, "in_progress": {"paused", "pending_acceptance"}, "paused": {"in_progress"}, "completed": set(), "pending_acceptance": set()}
            if self.assignee_id != user.id:
                raise DomainError(403, "只有任务承办人可以更新任务进展")
            if target not in allowed[self.status]:
                raise DomainError(409, "任务状态不能这样变更")
        return target

    def require_progress(self, actor: str) -> None:
        if self.assignee_id != actor:
            raise DomainError(403, "仅任务承办人可以添加进展")
        if self.status != "in_progress":
            raise DomainError(409, "任务进行中才能提交进展")


@dataclass(frozen=True)
class Conversation:
    id: str
    owner_id: str
    space_epoch: int

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Conversation":
        return cls(**{key: row[key] for key in ("id", "owner_id", "space_epoch")})

    def require_owner(self, actor: str) -> None:
        if self.owner_id != actor:
            raise DomainError(403, "该会话属于其他用户")

    def require_epoch(self, space: Space) -> None:
        if self.space_epoch != space.epoch:
            raise DomainError(409, "知识库权限已变化，请新建会话")
