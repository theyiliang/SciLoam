"""Space membership and authorization lifecycle use cases."""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.goals import Goal
from app.domain.models import Space, User


class SpaceUseCases:
    def list_spaces(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            result = []
            for row in repository.list("spaces"):
                if repository.get("space_deletions", row["id"]):
                    continue
                try:
                    row["role"] = Space.from_record(row).require_access(user, repository.membership(row["id"], actor))
                except DomainError as exc:
                    if exc.status_code == 403:
                        continue
                    raise
                result.append(row)
            return result

    def create_space(self, actor: str, name: str, kind: str = "collaboration", task_id: str | None = None,
                     goal_id: str | None = None) -> dict:
        if task_id is not None and goal_id is not None:
            raise DomainError(422, "关联任务与关联目标只能选择其一")
        name = policies.text(name, "space name", 200).strip()
        if kind not in ("basis", "private", "collaboration", "results"):
            raise DomainError(422, "知识库类型不正确")
        with self._uow_factory() as uow:
            repository = uow.repository
            self._user(repository, actor)
            if kind in ("basis", "results"):
                self._owner(repository, actor)
            if task_id:
                self._task(repository, actor, task_id)
                if kind != "collaboration":
                    raise DomainError(422, "只有协作知识库可以关联任务")
            if goal_id is not None:
                policies.text(goal_id, "goal id", 128)
                Goal.from_record(self._goal(repository, actor, goal_id)).require_active()
                if kind != "collaboration":
                    raise DomainError(422, "只有协作知识库可以关联目标")
            if kind != "collaboration":
                filters = {"kind": kind}
                if kind == "private":
                    filters["owner_id"] = actor
                if repository.find("spaces", **filters):
                    raise DomainError(409, "同名知识库已存在")
            space_id = self._new_space(repository, actor, name, kind, task_id)
            if goal_id is not None:
                # An association is metadata, never membership or source authority.
                repository.update("spaces", space_id, {"goal_id": goal_id})
            self._event(repository, actor, "space_created", "space", space_id)
            return self._space(repository, actor, space_id)

    def delete_space(self, actor: str, space_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            row = repository.get("spaces", space_id)
            if not row:
                raise DomainError(404, "未找到该知识库，请刷新列表")
            # A project owner or an editor never inherits destructive authority
            # over somebody else's collaboration space.
            Space.from_record(row).require_deletion(user)
            existing = repository.get("space_deletions", space_id)
            if existing:
                return existing
            self._space(repository, actor, space_id, manage=True)
            receipt = {"space_id": space_id, "actor_id": actor, "created_at": self._clock()}
            repository.add("space_deletions", receipt)
            self._bump_epoch(repository, space_id)
            self._event(repository, actor, "space_deleted", "space", space_id)
            return receipt

    def list_space_members(self, actor: str, space_id: str) -> list[dict]:
        with self._uow_factory() as uow:
            self._space(uow.repository, actor, space_id)
            return uow.repository.space_members(space_id)

    def grant_space(self, actor: str, space_id: str, user_id: str, role: str) -> dict:
        if role not in ("editor", "reader"):
            raise DomainError(422, "知识库权限只能授予编辑者或读者")
        with self._uow_factory() as uow:
            repository = uow.repository
            space = self._space(repository, actor, space_id, manage=True)
            self._user(repository, user_id)
            Space.from_record(space).require_grant(user_id, role)
            previous = repository.membership(space_id, user_id)
            if previous != role:
                repository.set_membership(space_id, user_id, role)
                self._bump_epoch(repository, space_id)
                self._event(repository, actor, "space_access_granted", "space", space_id)
            return {"space_id": space_id, "user_id": user_id, "role": role}

    def revoke_space(self, actor: str, space_id: str, user_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            space = self._space(repository, actor, space_id, manage=True)
            user = self._user(repository, user_id)
            Space.from_record(space).require_revocation(User.from_record(user))
            changed = repository.delete_where("space_members", space_id=space_id, user_id=user_id)
            if changed:
                self._bump_epoch(repository, space_id)
                self._event(repository, actor, "space_access_revoked", "space", space_id)
            return {"space_id": space_id, "user_id": user_id, "revoked": bool(changed)}
