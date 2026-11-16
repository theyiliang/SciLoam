"""Assignments, constrained progress transitions and attachment visibility."""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.models import Task, User
from app.domain.policies import UNSET

from .common import new_id


class TaskUseCases:
    def list_tasks(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            self._user(repository, actor)
            result = []
            for task in repository.list("tasks"):
                progress = []
                for entry in repository.list("progress", task_id=task["id"]):
                    if entry["version_id"]:
                        try:
                            self._version(repository, actor, entry["version_id"])
                        except DomainError as exc:
                            if exc.status_code in (403, 404, 410):
                                continue
                            raise
                    progress.append(entry)
                task["progress"] = progress
                result.append(task)
            return result

    def create_task(self, actor: str, title: str, description: str = "", assignee_id: str = "", due_date=None) -> dict:
        title = policies.text(title, "task title", 500).strip()
        policies.text(description, "task description", 20000, empty=True)
        due_date = policies.due_date(due_date)
        with self._uow_factory() as uow:
            repository = uow.repository
            self._owner(repository, actor)
            if assignee_id:
                self._user(repository, assignee_id)
            task_id, now = new_id(), self._clock()
            repository.add("tasks", {"id": task_id, "title": title, "description": description, "assignee_id": assignee_id or None, "due_date": due_date, "status": "not_started", "created_by": actor, "created_at": now, "updated_at": now})
            self._event(repository, actor, "task_created", "task", task_id)
            return self._task(repository, actor, task_id)

    def update_task(self, actor: str, task_id: str, *, title=None, description=None, assignee_id=None, due_date=UNSET, status=None) -> dict:
        if title is not None:
            title = policies.text(title, "task title", 500).strip()
        if description is not None:
            policies.text(description, "task description", 20000, empty=True)
        if due_date is not UNSET:
            due_date = policies.due_date(due_date)
        if status is not None and status not in ("not_started", "in_progress", "pending_acceptance", "completed", "paused"):
            raise DomainError(422, "任务状态不正确")
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            row = self._task(repository, actor, task_id)
            task = Task.from_record(row)
            metadata = title is not None or description is not None or assignee_id is not None or due_date is not UNSET
            task.authorize_edit(user, metadata)
            if assignee_id is not None:
                if assignee_id:
                    self._user(repository, assignee_id)
                task.require_reassignment(assignee_id or None)
                row["assignee_id"] = assignee_id or None
                task = Task.from_record(row)
            if status is not None:
                row["status"] = task.transition(user, status)
            if title is not None:
                row["title"] = title
            if description is not None:
                row["description"] = description
            if due_date is not UNSET:
                row["due_date"] = due_date
            changes = {key: row[key] for key in ("title", "description", "assignee_id", "due_date", "status")}
            changes["updated_at"] = self._clock()
            repository.update("tasks", task_id, changes)
            self._event(repository, actor, "task_updated", "task", task_id)
            return self._task(repository, actor, task_id)

    def add_progress(self, actor: str, task_id: str, content: str, version_id: str | None = None) -> dict:
        policies.text(content, "progress content", 20000)
        with self._uow_factory() as uow:
            repository = uow.repository
            Task.from_record(self._task(repository, actor, task_id)).require_progress(actor)
            if version_id:
                version = self._version(repository, actor, version_id)
                if self._space(repository, actor, version["space_id"])["kind"] == "private":
                    raise DomainError(403, "个人知识库的版本不能关联到共享任务")
            progress = {"id": new_id(), "task_id": task_id, "author_id": actor, "content": content, "version_id": version_id, "created_at": self._clock()}
            repository.add("progress", progress)
            self._event(repository, actor, "progress_added", "progress", progress["id"])
            return progress
