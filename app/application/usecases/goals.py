"""Shared goal planning with live source ACLs, never model-derived progress.

Goal descriptions and unbound updates are readable by all active project members.
Linking a private source never grants access; a source-bound update is wholly
hidden from any reader who cannot currently read that version.
"""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.goals import Goal, GoalTree, goal_parent, goal_position
from app.domain.models import User
from app.domain.policies import UNSET

from .common import new_id


class GoalUseCases:
    @staticmethod
    def _goal_record(repository, goal_id):
        row = repository.get("goal_nodes", goal_id)
        if row is None:
            raise DomainError(404, "未找到该目标，请刷新列表")
        return row

    def _goal(self, repository, actor, goal_id):
        """Load the public node only; source access is never implied by it."""
        self._user(repository, actor)
        return self._goal_record(repository, goal_id)

    @staticmethod
    def _goal_view(row, user, tree):
        result = dict(row)
        goal = Goal.from_record(row)
        result["path_titles"] = [ancestor["title"] for ancestor in tree.path(goal.id)]
        result["path"] = " / ".join(result["path_titles"])
        result["can_manage"] = user.role == "owner" and goal.archived_at is None
        result["can_contribute"] = goal.can_contribute
        return result

    def _goal_source(self, repository, actor, version_id):
        try:
            version = self._version(repository, actor, version_id)
        except DomainError as exc:
            if exc.status_code in (403, 404, 410):
                return None
            raise
        # No cached source/body and no inferred provenance: read display fields
        # solely from the currently authorized version, not a historical snapshot.
        return {"version_id": version_id, **{
            key: version[key] for key in ("document_id", "space_id", "title", "number", "source")
        }}

    def _goal_details(self, repository, actor, row, user, tree):
        result = self._goal_view(row, user, tree)
        knowledge = []
        for link in repository.list("goal_knowledge", goal_id=row["id"]):
            source = self._goal_source(repository, actor, link["version_id"])
            if source is not None:
                knowledge.append({**link, **source})
        updates = []
        for entry in repository.list("goal_updates", goal_id=row["id"]):
            if entry["version_id"] is not None:
                source = self._goal_source(repository, actor, entry["version_id"])
                if source is None:
                    continue
                entry = {**entry, "citation": source}
            updates.append(entry)
        result["knowledge"] = knowledge
        result["updates"] = updates
        result["status_history"] = repository.list("goal_status_history", goal_id=row["id"])
        generation_sources = []
        for receipt in repository.list('goal_generation_sources', goal_id=row['id']):
            node = receipt['node']
            receipt = {**receipt, 'current_revision': row['revision'],
                'current_differs_from_confirmation': any(row.get(field) != node.get(field)
                    for field in ('title', 'description', 'acceptance_criteria', 'due_date', 'deliverables', 'constraints', 'time_requirement'))}
            citations = [node.get('source')] + [c for values in node.get('field_sources', {}).values() for c in values]
            # A public formal goal never grants access to its underlying evidence.
            if all(c is None or self._goal_source(repository, actor, c['version_id']) is not None for c in citations):
                generation_sources.append(receipt)
            else:
                generation_sources.append({**receipt, 'node': {'id': receipt['node_id']}, 'source_details_redacted': True})
        result['generation_sources'] = generation_sources
        return result

    def list_goals(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            tree = GoalTree(repository.list("goal_nodes"))
            return [self._goal_view(row, user, tree) for row in tree.ordered()]

    def get_goal(self, actor: str, goal_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            row = self._goal_record(repository, goal_id)
            return self._goal_details(repository, actor, row, user, GoalTree(repository.list("goal_nodes")))

    def create_goal(self, actor: str, title: str, description: str = "", *, parent_id=None,
                    acceptance_criteria: str = "", due_date=None, position: int = 0,
                    deliverables: str = "", constraints: str = "", time_requirement: str = "") -> dict:
        title = policies.text(title, "goal title", 500).strip()
        policies.text(description, "goal description", 20000, empty=True)
        policies.text(acceptance_criteria, "goal acceptance criteria", 20000, empty=True)
        policies.text(deliverables, 'goal deliverables', 20000, empty=True)
        policies.text(constraints, 'goal constraints', 20000, empty=True)
        policies.text(time_requirement, 'goal time requirement', 20000, empty=True)
        due_date = policies.due_date(due_date)
        goal_parent(parent_id)
        goal_position(position)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            tree = GoalTree(repository.list("goal_nodes"))
            tree.require_placement(None, parent_id)
            goal_id, now = new_id(), self._clock()
            row = {"id": goal_id, "parent_id": parent_id, "title": title, "description": description,
                   "acceptance_criteria": acceptance_criteria, "due_date": due_date,
                   "deliverables": deliverables, "constraints": constraints, 'time_requirement': time_requirement,
                   "status": "not_started", "position": position, "revision": 1,
                   "created_by": actor, "created_at": now, "updated_at": now, "archived_at": None,
                   "archived_reason": ""}
            repository.add("goal_nodes", row)
            self._event(repository, actor, "goal_created", "goal", goal_id)
            return self._goal_details(repository, actor, self._goal_record(repository, goal_id), user,
                                      GoalTree(repository.list("goal_nodes")))

    def update_goal(self, actor: str, goal_id: str, expected_revision: int, *, title=UNSET,
                    description=UNSET, parent_id=UNSET, acceptance_criteria=UNSET,
                    due_date=UNSET, position=UNSET, deliverables=UNSET, constraints=UNSET, time_requirement=UNSET) -> dict:
        changes = {}
        if title is not UNSET:
            changes["title"] = policies.text(title, "goal title", 500).strip()
        for name, value in (("description", description), ("acceptance_criteria", acceptance_criteria),
                            ('deliverables', deliverables), ('constraints', constraints), ('time_requirement', time_requirement)):
            if value is not UNSET:
                changes[name] = policies.text(value, f"goal {name}", 20000, empty=True)
        if due_date is not UNSET:
            changes["due_date"] = policies.due_date(due_date)
        if position is not UNSET:
            changes["position"] = goal_position(position)
        if parent_id is not UNSET:
            changes["parent_id"] = goal_parent(parent_id)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            row = self._goal_record(repository, goal_id)
            Goal.from_record(row).require_management(user, expected_revision)
            tree = GoalTree(repository.list("goal_nodes"))
            if parent_id is not UNSET:
                tree.require_placement(goal_id, parent_id)
            if changes:
                changes.update(revision=row["revision"] + 1, updated_at=self._clock())
                repository.update("goal_nodes", goal_id, changes)
                self._event(repository, actor, "goal_updated", "goal", goal_id)
            return self._goal_details(repository, actor, self._goal_record(repository, goal_id), user,
                                      GoalTree(repository.list("goal_nodes")))

    def confirm_goal_status(self, actor: str, goal_id: str, status: str, reason: str,
                            expected_revision: int) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            row = self._goal_record(repository, goal_id)
            goal = Goal.from_record(row)
            goal.require_management(user, expected_revision)
            goal.transition(status, reason, repository.list("goal_nodes", parent_id=goal_id))
            if goal.status == "completed":
                GoalTree(repository.list("goal_nodes")).require_reopening(goal_id)
            now = self._clock()
            repository.update("goal_nodes", goal_id, {"status": status, "revision": goal.revision + 1, "updated_at": now})
            repository.add("goal_status_history", {"id": new_id(), "goal_id": goal_id, "actor_id": actor,
                           "from_status": goal.status, "to_status": status, "reason": reason, "created_at": now})
            self._event(repository, actor, "goal_status_confirmed", "goal", goal_id)
            return self._goal_details(repository, actor, self._goal_record(repository, goal_id), user,
                                      GoalTree(repository.list("goal_nodes")))

    def archive_goal(self, actor: str, goal_id: str, expected_revision: int, reason: str) -> dict:
        policies.text(reason, "goal archive reason", 10000)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._owner(repository, actor))
            row = self._goal_record(repository, goal_id)
            goal = Goal.from_record(row)
            goal.require_management(user, expected_revision)
            goal.require_archive(repository.list("goal_nodes", parent_id=goal_id))
            now = self._clock()
            repository.update("goal_nodes", goal_id, {"archived_at": now, "archived_reason": reason,
                              "updated_at": now, "revision": goal.revision + 1})
            # Archive does not invent a status or modify the status-transition
            # history. The immutable, content-free audit records the operation.
            self._event(repository, actor, "goal_archived", "goal", goal_id)
            return self._goal_details(repository, actor, self._goal_record(repository, goal_id), user,
                                      GoalTree(repository.list("goal_nodes")))

    def link_goal_knowledge(self, actor: str, goal_id: str, version_id: str) -> dict:
        policies.text(version_id, "knowledge version id", 128)
        with self._uow_factory() as uow:
            repository = uow.repository
            self._user(repository, actor)
            Goal.from_record(self._goal_record(repository, goal_id)).require_contribution()
            self._version(repository, actor, version_id)
            existing = repository.find("goal_knowledge", goal_id=goal_id, version_id=version_id)
            if existing is not None:
                return {**existing, **self._goal_source(repository, actor, version_id)}
            link = {"id": new_id(), "goal_id": goal_id, "version_id": version_id,
                    "linked_by": actor, "created_at": self._clock()}
            repository.add("goal_knowledge", link)
            self._event(repository, actor, "goal_knowledge_linked", "goal", goal_id)
            return {**link, **self._goal_source(repository, actor, version_id)}

    def unlink_goal_knowledge(self, actor: str, goal_id: str, link_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = User.from_record(self._user(repository, actor))
            Goal.from_record(self._goal_record(repository, goal_id)).require_contribution()
            link = repository.get("goal_knowledge", link_id)
            if link is None or link["goal_id"] != goal_id:
                raise DomainError(404, "未找到这条目标知识关联，请刷新列表")
            if link["linked_by"] != actor:
                if user.role != "owner":
                    raise DomainError(403, "只有关联作者或课题负责人可以移除该知识关联")
                # Owners may manage another author's visible links, not probe or
                # remove hidden private references. Authors can always clean up
                # their own links after revocation or source withdrawal.
                if self._goal_source(repository, actor, link["version_id"]) is None:
                    raise DomainError(403, "需要先获得来源知识库的访问权限，才能移除他人添加的关联")
            repository.delete_where("goal_knowledge", id=link_id, goal_id=goal_id)
            self._event(repository, actor, "goal_knowledge_unlinked", "goal", goal_id)
            # Management is not source-read authority: never return the removed
            # version id, title, source or metadata, even to the project owner.
            return {"unlinked": True}

    def add_goal_update(self, actor: str, goal_id: str, content: str, version_id=None) -> dict:
        policies.text(content, "goal update content", 20000)
        if version_id is not None:
            policies.text(version_id, "knowledge version id", 128)
        with self._uow_factory() as uow:
            repository = uow.repository
            self._user(repository, actor)
            Goal.from_record(self._goal_record(repository, goal_id)).require_contribution()
            citation = None
            if version_id is not None:
                self._version(repository, actor, version_id)
                citation = self._goal_source(repository, actor, version_id)
            entry = {"id": new_id(), "goal_id": goal_id, "author_id": actor, "content": content,
                     "version_id": version_id, "created_at": self._clock()}
            repository.add("goal_updates", entry)
            self._event(repository, actor, "goal_update_added", "goal", goal_id)
            return {**entry, "citation": citation} if citation is not None else entry
