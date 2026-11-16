"""Pure goal-tree, optimistic-revision and owner-confirmed status rules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import policies
from .errors import DomainError
from .models import User

MAX_GOAL_DEPTH = 8
GOAL_STATUSES = frozenset({"not_started", "in_progress", "pending_acceptance", "completed", "paused"})
GOAL_TRANSITIONS = {
    "not_started": frozenset({"in_progress", "paused"}),
    "in_progress": frozenset({"pending_acceptance", "paused"}),
    "pending_acceptance": frozenset({"completed", "in_progress"}),
    "paused": frozenset({"in_progress"}),
    "completed": frozenset({"in_progress"}),
}


def goal_position(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 10000:
        raise DomainError(422, "Goal position must be an integer from 0 to 10000")
    return value


def goal_parent(value: Any) -> str | None:
    if value is not None:
        policies.text(value, "goal parent id", 128)
    return value


@dataclass(frozen=True)
class Goal:
    id: str
    parent_id: str | None
    status: str
    revision: int
    archived_at: float | None = None
    archived_reason: str = ""

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> "Goal":
        return cls(**{name: row[name] for name in ("id", "parent_id", "status", "revision", "archived_at")},
                   archived_reason=row.get("archived_reason") or "")

    @property
    def can_contribute(self) -> bool:
        return self.archived_at is None and self.status != "completed"

    def require_active(self) -> None:
        if self.archived_at is not None:
            raise DomainError(409, "Archived goals cannot be changed")

    def require_revision(self, expected_revision: Any) -> None:
        if type(expected_revision) is not int or expected_revision < 1:
            raise DomainError(422, "expected_revision must be a positive integer")
        if expected_revision != self.revision:
            raise DomainError(409, "Goal revision is stale; reload before changing the goal")

    def require_management(self, user: User, expected_revision: Any) -> None:
        user.require_owner()
        self.require_revision(expected_revision)
        self.require_active()

    def require_contribution(self) -> None:
        self.require_active()
        if self.status == "completed":
            raise DomainError(409, "Completed goals cannot receive contributions")

    def transition(self, status: str, reason: str, children: list[Mapping[str, Any]]) -> str:
        self.require_active()
        if not isinstance(status, str) or status not in GOAL_STATUSES:
            raise DomainError(422, "Invalid goal status")
        policies.text(reason, "goal status reason", 10000, empty=True)
        if status not in GOAL_TRANSITIONS[self.status]:
            raise DomainError(409, "Invalid goal status transition")
        if status == "completed":
            policies.text(reason, "completion acceptance summary", 10000)
            if any(child["archived_at"] is None and child["status"] != "completed" for child in children):
                raise DomainError(409, "All active child goals must be completed before acceptance")
        elif self.status == "completed":
            policies.text(reason, "goal reopening reason", 10000)
        return status

    def require_archive(self, children: list[Mapping[str, Any]]) -> None:
        self.require_active()
        if any(child["archived_at"] is None for child in children):
            raise DomainError(409, "Archive active child goals before archiving their parent")


class GoalTree:
    """A detached tree view; root depth is one, including archived history."""

    def __init__(self, records: list[Mapping[str, Any]]):
        self.nodes = {row["id"]: row for row in records}
        self.children: dict[str | None, list[Mapping[str, Any]]] = {}
        for row in records:
            self.children.setdefault(row["parent_id"], []).append(row)
        for children in self.children.values():
            children.sort(key=lambda row: (row["position"], row["created_at"], row["id"]))

    def path(self, goal_id: str) -> list[Mapping[str, Any]]:
        result, visited = [], set()
        current = goal_id
        while current is not None:
            if current in visited:
                raise DomainError(409, "Goal tree contains a cycle")
            visited.add(current)
            row = self.nodes.get(current)
            if row is None:
                raise DomainError(409, "Goal tree contains a missing parent")
            result.append(row)
            current = row["parent_id"]
        return list(reversed(result))

    def require_placement(self, goal_id: str | None, parent_id: str | None) -> None:
        goal_parent(parent_id)
        parent_path = []
        if parent_id is not None:
            parent = self.nodes.get(parent_id)
            if parent is None:
                raise DomainError(404, "Parent goal not found")
            Goal.from_record(parent).require_active()
            parent_path = self.path(parent_id)
            if any(row["status"] == "completed" for row in parent_path):
                raise DomainError(409, "Reopen completed ancestor goals before adding or moving children")
        if goal_id is not None and any(row["id"] == goal_id for row in parent_path):
            raise DomainError(409, "A goal cannot be moved under itself or its descendants")
        height = self._height(goal_id, set()) if goal_id is not None else 1
        if len(parent_path) + height > MAX_GOAL_DEPTH:
            raise DomainError(409, "Goal tree depth cannot exceed eight levels")

    def require_reopening(self, goal_id: str) -> None:
        if any(row["status"] == "completed" for row in self.path(goal_id)[:-1]):
            raise DomainError(409, "Reopen completed ancestor goals before reopening a child goal")

    def _height(self, goal_id: str, visited: set[str]) -> int:
        if goal_id in visited:
            raise DomainError(409, "Goal tree contains a cycle")
        branch = visited | {goal_id}
        return 1 + max((self._height(child["id"], branch) for child in self.children.get(goal_id, [])), default=0)

    def ordered(self, *, include_archived: bool = False) -> list[Mapping[str, Any]]:
        result = []

        def visit(parent_id, depth):
            if depth > MAX_GOAL_DEPTH and self.children.get(parent_id):
                raise DomainError(409, "Goal tree depth cannot exceed eight levels")
            for row in self.children.get(parent_id, []):
                if include_archived or row["archived_at"] is None:
                    result.append(row)
                visit(row["id"], depth + 1)

        # Validate disconnected/cyclic historical data rather than silently dropping it.
        for goal_id in self.nodes:
            self.path(goal_id)
        visit(None, 1)
        return result
