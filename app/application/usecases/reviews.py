"""Snapshot-bound review submission and atomic publication use cases."""
from app.domain import policies
from app.domain.errors import DomainError
from app.domain.goals import Goal, GoalTree
from app.domain.models import Review
from app.domain.review_snapshot import ReviewSnapshot

from .common import new_id


class ReviewUseCases:
    @staticmethod
    def _review_snapshot(repository, review_id):
        row = repository.get("review_snapshots", review_id)
        if not row:
            # Never silently reconstruct evidence from a later source state.
            raise DomainError(409, "审核内容快照不可用，请刷新后重试")
        return ReviewSnapshot.from_record(row)

    @staticmethod
    def _review_receipt(repository, user, review, snapshot):
        can_review = (user["role"] == "owner" and review["status"] == "pending"
                      and user["id"] != review["submitted_by"]
                      and user["id"] not in snapshot.lineage_authors)
        goal_id = review.get("goal_id")
        goal = {"goal_id": goal_id, "goal_title": None, "goal_path": None, "goal_archived": None}
        if goal_id is not None:
            # Read retained public planning records, including archived history,
            # never linked sources/updates or a title inferred from the snapshot.
            row = repository.get("goal_nodes", goal_id)
            if row is not None:
                tree = GoalTree(repository.list("goal_nodes"))
                goal.update(goal_title=row["title"],
                            goal_path=" / ".join(node["title"] for node in tree.path(goal_id)),
                            goal_archived=row["archived_at"] is not None)
        return {**review, **snapshot.receipt(goal), "can_review": can_review}

    @staticmethod
    def _same_review_association(repository, existing, task_id, goal_id):
        if existing.get("task_id") != task_id:
            return False
        if existing.get("goal_id") == goal_id:
            return True
        # Schema-five backfilled old task associations to the same goal ID.
        # Keep retries through the original API; do not permit goal rebinding.
        if goal_id is None and task_id is not None and existing.get("goal_id") == task_id:
            migrated = repository.get("goal_nodes", task_id)
            return migrated is not None and migrated.get("legacy_task_id") == task_id
        return False

    def list_reviews(self, actor: str) -> list[dict]:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = self._user(repository, actor)
            rows = repository.list("reviews", **({} if user["role"] == "owner" else {"submitted_by": actor}))
            return [self._review_receipt(repository, user, row, self._review_snapshot(repository, row["id"])) for row in rows]

    def read_review_snapshot(self, actor: str, review_id: str) -> dict:
        with self._uow_factory() as uow:
            repository = uow.repository
            user = self._user(repository, actor)
            review = repository.get("reviews", review_id)
            if not review:
                raise DomainError(404, "未找到这条审核记录，请刷新列表")
            if user["role"] != "owner" and review["submitted_by"] != actor:
                raise DomainError(403, "无权查看这条审核记录")
            snapshot = self._review_snapshot(repository, review_id)
            return {**snapshot.public(), **self._review_receipt(repository, user, review, snapshot),
                    "submitted_at": review["created_at"]}

    def submit_review(self, actor: str, version_id: str, task_id: str | None = None,
                      goal_id: str | None = None) -> dict:
        if task_id is not None and goal_id is not None:
            raise DomainError(422, "关联任务与关联目标只能选择其一")
        if goal_id is not None:
            policies.text(goal_id, "goal id", 128)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = self._user(repository, actor)
            existing = repository.find("reviews", version_id=version_id)
            if existing and existing["submitted_by"] == actor and self._same_review_association(repository, existing, task_id, goal_id):
                # A retry acknowledges the already-frozen submission, even after
                # source revocation. It cannot create a new copy or expose the source.
                return self._review_receipt(repository, user, existing, self._review_snapshot(repository, existing["id"]))
            version = self._version(repository, actor, version_id, write=True)
            space = self._space(repository, actor, version["space_id"], write=True)
            if space["kind"] not in ("private", "collaboration"):
                raise DomainError(403, "仅个人或协作知识库中的版本可以送审")
            if task_id and self._task(repository, actor, task_id)["assignee_id"] != actor:
                raise DomainError(403, "仅任务承办人可以提交该任务的成果")
            if goal_id is not None:
                Goal.from_record(self._goal(repository, actor, goal_id)).require_active()
            if existing:
                raise DomainError(409, "该版本已提交过审核，不能重复送审；如内容有更新，请新增版本后再次提交")
            review = {"id": new_id(), "version_id": version_id, "task_id": task_id, "goal_id": goal_id, "submitted_by": actor, "status": "pending", "reviewed_by": None, "comment": "", "published_version_id": None, "created_at": self._clock(), "reviewed_at": None}
            snapshot = ReviewSnapshot.capture(review["id"], version, space["kind"],
                                              repository.lineage_authors(version_id), review["created_at"])
            repository.add("reviews", review)
            repository.add("review_snapshots", snapshot.record())
            self._event(repository, actor, "review_submitted", "review", review["id"])
            return self._review_receipt(repository, user, review, snapshot)

    def decide_review(self, actor: str, review_id: str, decision: str, comment: str = "") -> dict:
        policies.review_decision(decision, comment)
        with self._uow_factory() as uow:
            repository = uow.repository
            user = self._owner(repository, actor)
            review = repository.get("reviews", review_id)
            if not review:
                raise DomainError(404, "未找到这条审核记录，请刷新列表")
            snapshot = self._review_snapshot(repository, review_id)
            status, repeated = Review.from_record(review).decision(actor, decision, comment, set(snapshot.lineage_authors))
            if repeated:
                return self._review_receipt(repository, user, review, snapshot)
            published_id = None
            if decision == "approve":
                target = repository.find("spaces", kind="results")
                if not target:
                    raise DomainError(409, "成果知识库不可用，暂时无法发布审核通过的版本")
                _, published_id = self._insert_document(repository, actor, target["id"], snapshot.title,
                                                        snapshot.content, snapshot.filename, snapshot.source,
                                                        snapshot.version_id)
            repository.update("reviews", review_id, {"status": status, "reviewed_by": actor, "comment": comment, "published_version_id": published_id, "reviewed_at": self._clock()})
            self._event(repository, actor, f"review_{status}", "review", review_id)
            return self._review_receipt(repository, user, repository.get("reviews", review_id), snapshot)
