"""Identity, invitations, account lifecycle and trusted local recovery."""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.models import User

from .common import new_id


class IdentityUseCases:
    def bootstrap(self, email: str, password: str, project_name: str) -> dict:
        email = policies.email(email)
        project_name = policies.text(project_name, "project name", 200).strip()
        password_hash = self._security.hash_password(policies.password(password))
        with self._uow_factory() as uow:
            repository = uow.repository
            if repository.find("users"):
                raise DomainError(409, "课题已初始化，不能重复初始化")
            user_id, project_id = new_id(), new_id()
            repository.add("users", {"id": user_id, "email": email, "password_hash": password_hash, "role": "owner", "created_at": self._clock()})
            repository.add("projects", {"id": project_id, "name": project_name, "owner_id": user_id})
            self._new_space(repository, user_id, "课题知识库", "basis")
            self._new_space(repository, user_id, "成果知识库", "results")
            self._new_space(repository, user_id, "个人知识库", "private")
            self._event(repository, user_id, "bootstrap", "project", project_id)
            return {"user": self._user(repository, user_id), "project": self._project(repository)}

    def login(self, email: str, password: str) -> dict:
        email = policies.email(email)
        failure = None
        result = None
        with self._uow_factory() as uow:
            repository, now = uow.repository, self._clock()
            throttle = repository.get("login_failures", email)
            if throttle and throttle["locked_until"] > now:
                failure = DomainError(429, "登录尝试过多，请稍后再试")
            else:
                user = repository.find("users", email=email)
                valid = self._security.verify_password(password, user["password_hash"] if user else self._dummy_password)
                if not user or not valid or user["disabled_at"] is not None:
                    repository.save_login_failure(email, policies.login_failure(throttle, now))
                    failure = DomainError(401, "邮箱或密码不正确")
                else:
                    repository.delete_where("login_failures", email=email)
                    token = self._security.new_token()
                    repository.add("sessions", {"token_hash": self._security.hash_token(token), "user_id": user["id"], "expires_at": now + policies.TOKEN_TTL_SECONDS, "created_at": now})
                    self._event(repository, user["id"], "login", "user", user["id"])
                    result = {"token": token, "user": self._user(repository, user["id"]), "project": self._project(repository)}
        # A rejected login still commits its throttle counters.
        if failure:
            raise failure
        return result

    def authenticate(self, token: str) -> dict:
        policies.session_token(token)
        with self._uow_factory() as uow:
            session = uow.repository.get("sessions", self._security.hash_token(token))
            if not session or session["expires_at"] <= self._clock():
                raise DomainError(401, "登录状态已失效，请重新登录")
            return self._user(uow.repository, session["user_id"])

    def logout(self, token: str) -> dict:
        policies.session_token(token)
        with self._uow_factory() as uow:
            uow.repository.delete_where("sessions", token_hash=self._security.hash_token(token))
            return {"logged_out": True}

    def get_me(self, actor: str) -> dict:
        with self._uow_factory() as uow:
            return {"user": self._user(uow.repository, actor), "project": self._project(uow.repository)}

    def list_members(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            self._user(uow.repository, actor)
            return [User.from_record(row).public() for row in uow.repository.list("users", disabled_at=None)]

    def remove_member(self, actor: str, user_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            self._owner(repository, actor)
            user = repository.get("users", user_id)
            if not user:
                raise DomainError(404, "未找到该成员，请刷新列表")
            if user["role"] == "owner":
                raise DomainError(403, "课题负责人不能被移除")
            if user["disabled_at"] is not None:
                return {"user_id": user_id, "disabled_at": user["disabled_at"]}
            now = self._clock()
            repository.update("users", user_id, {"disabled_at": now})
            repository.delete_where("sessions", user_id=user_id)
            for space_id in repository.member_space_ids(user_id):
                self._bump_epoch(repository, space_id)
            repository.delete_where("space_members", user_id=user_id)
            for invite in repository.list("invites", email=user["email"], accepted_by=None):
                repository.update("invites", invite["id"], {"expires_at": 0})
            for task in repository.list("tasks", assignee_id=user_id):
                if task["status"] != "completed":
                    repository.update("tasks", task["id"], {"assignee_id": None, "status": "paused", "updated_at": now})
            self._event(repository, actor, "member_removed", "user", user_id)
            return {"user_id": user_id, "disabled_at": now}

    def change_password(self, actor: str, current_password: str, new_password: str) -> dict:
        # Read credentials briefly; PBKDF2 verification and hashing must not hold
        # the unit of work's serialized database transaction.
        with self._uow_factory() as uow:
            repository = uow.repository
            user = self._user(repository, actor)
            email = user["email"]
            throttle = repository.get("login_failures", email)
            if throttle and throttle["locked_until"] > self._clock():
                raise DomainError(429, "登录尝试过多，请稍后再试")
            original_hash = repository.get("users", actor)["password_hash"]

        valid = self._security.verify_password(current_password, original_hash)
        password_hash = None
        if valid:
            new_password = policies.password(new_password)
            if new_password == current_password:
                raise DomainError(422, "新密码不能与当前密码相同")
            password_hash = self._security.hash_password(new_password)

        failure = None
        with self._uow_factory() as uow:
            repository, now = uow.repository, self._clock()
            self._user(repository, actor)
            throttle = repository.get("login_failures", email)
            # Recheck after slow work: another login failure, reset, account
            # removal or password change may have won the race.
            if throttle and throttle["locked_until"] > now:
                failure = DomainError(429, "登录尝试过多，请稍后再试")
            elif repository.get("users", actor)["password_hash"] != original_hash:
                failure = DomainError(409, "账号凭证已变更，请重新登录")
            elif not valid:
                repository.save_login_failure(email, policies.login_failure(throttle, now))
                failure = DomainError(401, "当前密码不正确")
            else:
                repository.update("users", actor, {"password_hash": password_hash})
                repository.delete_where("sessions", user_id=actor)
                repository.delete_where("login_failures", email=email)
                self._event(repository, actor, "password_changed", "user", actor)
        # Raising inside the transaction would roll back the failure counter.
        if failure:
            raise failure
        return {"password_changed": True, "reauthenticate": True}

    def reset_password(self, actor: str, user_id: str, password: str) -> dict:
        password_hash = self._security.hash_password(policies.password(password))
        with self._uow_factory() as uow:
            return self._reset_password(uow.repository, actor, user_id, password_hash)

    def _reset_password(self, repository, actor, user_id, password_hash):
        self._owner(repository, actor)
        user = self._user(repository, user_id)
        repository.update("users", user_id, {"password_hash": password_hash})
        repository.delete_where("sessions", user_id=user_id)
        repository.delete_where("login_failures", email=user["email"])
        self._event(repository, actor, "password_reset", "user", user_id)
        return {"user_id": user_id, "password_reset": True}

    def recover_password(self, email: str, password: str) -> dict:
        """Trusted local CLI only: filesystem access is its authentication boundary.

        Do not expose this use case through a remote transport.
        """
        email = policies.email(email)
        password_hash = self._security.hash_password(policies.password(password))
        with self._uow_factory() as uow:
            owner = uow.repository.find("users", role="owner", disabled_at=None)
            target = uow.repository.find("users", email=email, disabled_at=None)
            if not owner or not target:
                raise DomainError(404, "未找到课题负责人或目标账号")
            return self._reset_password(uow.repository, owner["id"], target["id"], password_hash)

    def create_invite(self, actor: str, email: str, role: str = "member") -> dict:
        email = policies.email(email)
        if role != "member":
            raise DomainError(422, "只支持邀请成员加入")
        with self._uow_factory() as uow:
            repository = uow.repository
            self._owner(repository, actor)
            existing = repository.find("users", email=email)
            if existing and existing["disabled_at"] is not None:
                raise DomainError(409, "该账号已停用，不能重新邀请")
            invite_id, token, now = new_id(), self._security.new_token(), self._clock()
            expires_at = now + policies.INVITE_TTL_SECONDS
            repository.add("invites", {"id": invite_id, "email": email, "role": role, "token_hash": self._security.hash_token(token), "expires_at": expires_at, "created_by": actor, "accepted_by": None, "accepted_at": None, "created_at": now})
            self._event(repository, actor, "invite_created", "invite", invite_id)
            return {"id": invite_id, "email": email, "role": role, "invite_token": token, "expires_at": expires_at, "user_exists": bool(existing)}

    def _invite(self, repository, token):
        policies.session_token(token, invitation=True)
        return policies.invitation(repository.find("invites", token_hash=self._security.hash_token(token)), self._clock())

    def register(self, email: str, password: str, invite_token: str) -> dict:
        email = policies.email(email)
        password_hash = self._security.hash_password(policies.password(password))
        with self._uow_factory() as uow:
            repository = uow.repository
            invite = self._invite(repository, invite_token)
            if email != invite["email"]:
                raise DomainError(403, "邀请邮箱不一致")
            if repository.find("users", email=email):
                raise DomainError(409, "账号已存在，请直接登录后接受邀请")
            user_id = new_id()
            repository.add("users", {"id": user_id, "email": email, "password_hash": password_hash, "role": "member", "created_at": self._clock()})
            repository.update("invites", invite["id"], {"accepted_by": user_id, "accepted_at": self._clock()})
            self._join(repository, user_id)
            self._event(repository, user_id, "invite_accepted", "invite", invite["id"])
            return {"user": self._user(repository, user_id), "project": self._project(repository)}

    def accept_invite(self, actor: str, token: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user, invite = self._user(repository, actor), self._invite(repository, token)
            if user["email"] != invite["email"]:
                raise DomainError(403, "邀请邮箱不一致")
            repository.update("invites", invite["id"], {"accepted_by": actor, "accepted_at": self._clock()})
            self._event(repository, actor, "invite_accepted", "invite", invite["id"])
            return {"user": user, "project": self._project(repository)}

    def audit(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            self._owner(uow.repository, actor)
            return uow.repository.list("audit_events")
