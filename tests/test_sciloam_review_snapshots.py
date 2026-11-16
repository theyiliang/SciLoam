"""Phase-five frozen review evidence and transactional publication boundaries."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import json
import sqlite3

import pytest

from app.domain.review_snapshot import ReviewSnapshot
from app.infrastructure.persistence.factory import build_research_service
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from tests.test_research_service import denied, space_of, world


def submitted_private(world, *, task_id=None):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    document = service.add_document(alice, private["id"], "冻结标题", "submitted evidence", "paper.md", "local notes")
    version_id = document["latest_version"]["id"]
    review = service.submit_review(alice, version_id, task_id)
    return private, document, version_id, review


def test_private_submission_requires_no_share_and_does_not_grant_source_access(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    assert review["title"] == "冻结标题"
    assert review["number"] == 1
    assert review["source_space_kind"] == "private"
    assert review["mode"] == "snapshot"
    assert review["target_visibility"] == "results"
    assert review["can_review"] is False
    assert "content" not in review
    assert private["id"] not in {row["id"] for row in service.list_spaces(owner)}
    denied(403, service.get_version, owner, version_id)
    denied(403, service.list_documents, owner, private["id"])
    receipt = service.list_reviews(owner)[0]
    assert receipt["id"] == review["id"] and receipt["can_review"] is True
    snapshot = service.read_review_snapshot(owner, review["id"])
    assert snapshot["content"] == "submitted evidence"
    assert snapshot["version_id"] == version_id
    assert snapshot["submitted_by"] == alice
    assert snapshot["submitted_at"] == review["created_at"]
    assert snapshot["source"] == "local notes"
    assert snapshot["target_visibility"] == "results"
    assert not {"lineage_authors", "email", "owner_id", "private_space_id", "private_root"} & snapshot.keys()
    assert service.read_review_snapshot(alice, review["id"])["content"] == snapshot["content"]
    approved = service.decide_review(owner, review["id"], "approve")
    published = service.get_version(bob, approved["published_version_id"])
    assert published["id"] != version_id
    assert published["content"] == snapshot["content"]
    assert published["copied_from"] == version_id
    denied(403, service.get_version, owner, version_id)
    denied(403, service.get_version, bob, version_id)


def test_collaboration_snapshot_is_visible_without_source_membership(world):
    service, owner, alice, bob = world
    source = service.create_space(alice, "未共享的协作空间")
    document = service.add_document(alice, source["id"], "协作送审", "collaboration evidence")
    review = service.submit_review(alice, document["latest_version"]["id"])
    denied(403, service.get_version, owner, review["version_id"])
    assert service.list_reviews(owner)[0]["source_space_kind"] == "collaboration"
    assert service.read_review_snapshot(owner, review["id"])["content"] == "collaboration evidence"
    assert service.decide_review(owner, review["id"], "approve")["status"] == "approved"


def test_snapshot_access_is_only_owner_or_submitter_not_source_readers(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    service.grant_space(alice, private["id"], bob, "editor")
    assert service.get_version(bob, version_id)["content"] == "submitted evidence"
    assert service.list_reviews(bob) == []
    denied(403, service.read_review_snapshot, bob, review["id"])
    denied(403, service.decide_review, bob, review["id"], "approve")
    denied(401, service.read_review_snapshot, "missing-user", review["id"])
    denied(401, service.list_reviews, {"id": owner, "role": "owner"})
    denied(404, service.read_review_snapshot, owner, "missing-review")


@pytest.mark.parametrize("kind", ["private", "collaboration"])
def test_submission_requires_current_write_access_not_authorship(kind, world):
    service, owner, alice, bob = world
    space = space_of(service, alice, "private") if kind == "private" else service.create_space(alice, "权限测试")
    document = service.add_document(alice, space["id"], "作者不是提交人", "original author evidence")
    version_id = document["latest_version"]["id"]
    service.grant_space(alice, space["id"], bob, "reader")
    denied(403, service.submit_review, bob, version_id)
    denied(403, service.submit_review, owner, version_id)
    service.grant_space(alice, space["id"], bob, "editor")
    review = service.submit_review(bob, version_id)
    assert review["submitted_by"] == bob
    assert service.read_review_snapshot(bob, review["id"])["source_created_by"] == alice
    service.revoke_space(alice, space["id"], bob)
    assert service.list_reviews(bob)[0]["id"] == review["id"]
    assert service.read_review_snapshot(bob, review["id"])["content"] == "original author evidence"
    assert service.submit_review(bob, version_id) == review
    denied(403, service.get_version, bob, version_id)


def test_source_withdrawal_does_not_remove_receipt_or_block_snapshot_publication(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    service.withdraw_document(alice, document["id"])
    denied(410, service.get_version, alice, version_id)
    assert service.list_reviews(alice)[0]["id"] == review["id"]
    assert service.list_reviews(owner)[0]["id"] == review["id"]
    assert service.submit_review(alice, version_id) == review
    assert service.read_review_snapshot(alice, review["id"])["content"] == "submitted evidence"
    approved = service.decide_review(owner, review["id"], "approve")
    assert service.get_version(bob, approved["published_version_id"])["content"] == "submitted evidence"
    restarted = build_research_service(service.db_path)
    assert restarted.read_review_snapshot(owner, review["id"])["content"] == "submitted evidence"
    assert restarted.list_reviews(alice)[0]["status"] == "approved"


def test_source_space_deletion_retains_snapshot_and_independent_result(world):
    service, owner, alice, bob = world
    source = service.create_space(alice, "待删除的协作库")
    document = service.add_document(alice, source["id"], "库删除前送审", "retained review evidence")
    version_id = document["latest_version"]["id"]
    review = service.submit_review(alice, version_id)
    service.delete_space(alice, source["id"])
    assert source["id"] not in {row["id"] for row in service.list_spaces(alice)}
    denied(410, service.get_version, alice, version_id)
    assert service.list_reviews(alice)[0]["id"] == review["id"]
    assert service.read_review_snapshot(owner, review["id"])["content"] == "retained review evidence"
    approved = service.decide_review(owner, review["id"], "approve")
    assert service.get_version(bob, approved["published_version_id"])["content"] == "retained review evidence"
    with service.db.transaction() as connection:
        assert connection.execute("SELECT id FROM versions WHERE id=?", (version_id,)).fetchone()


def test_withdrawn_source_version_keeps_review_evidence_and_publication(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    latest = service.add_version(alice, document["id"], "new active version")
    service.withdraw_version(alice, version_id, "Superseded but retained for audit")
    denied(410, service.get_version, alice, version_id)
    assert service.get_version(alice, latest["id"])["content"] == "new active version"
    assert service.read_review_snapshot(owner, review["id"])["content"] == "submitted evidence"
    approved = service.decide_review(owner, review["id"], "approve")
    assert service.get_version(bob, approved["published_version_id"])["content"] == "submitted evidence"
    denied(410, service.get_version, alice, version_id)


def test_new_source_version_and_title_change_cannot_replace_pending_evidence(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    service.add_version(alice, document["id"], "not reviewed", "changed.md", "new source")
    with service.db.transaction() as connection:
        connection.execute("UPDATE documents SET title='Later title' WHERE id=?", (document["id"],))
    snapshot = service.read_review_snapshot(owner, review["id"])
    assert (snapshot["title"], snapshot["number"], snapshot["content"], snapshot["filename"], snapshot["source"]) == (
        "冻结标题", 1, "submitted evidence", "paper.md", "local notes")
    approved = service.decide_review(owner, review["id"], "approve")
    published = service.get_version(bob, approved["published_version_id"])
    assert (published["title"], published["content"], published["filename"], published["source"]) == (
        "冻结标题", "submitted evidence", "paper.md", "local notes")
    assert published["copied_from"] == version_id


def test_owner_may_read_own_snapshot_but_cannot_decide_own_submission(world):
    service, owner, alice, bob = world
    private = space_of(service, owner, "private")
    document = service.add_document(owner, private["id"], "负责人自有", "own work")
    review = service.submit_review(owner, document["latest_version"]["id"])
    assert service.read_review_snapshot(owner, review["id"])["content"] == "own work"
    assert service.list_reviews(owner)[0]["can_review"] is False
    denied(403, service.decide_review, owner, review["id"], "approve")
    denied(403, service.decide_review, owner, review["id"], "reject", "return my own work")


def test_frozen_copied_lineage_blocks_self_review_even_without_source_access(world, monkeypatch):
    service, owner, alice, bob = world
    source = service.create_space(owner, "负责人原文")
    service.grant_space(owner, source["id"], alice, "editor")
    original = service.add_document(owner, source["id"], "原作", "owner authorship")
    target = service.create_space(alice, "成员副本")
    copy = service.share_version(alice, original["latest_version"]["id"], target["id"])
    review = service.submit_review(alice, copy["latest_version"]["id"])
    service.withdraw_document(owner, original["id"])
    denied(403, service.get_version, owner, review["version_id"])
    monkeypatch.setattr(SQLiteResearchRepository, "lineage_authors", lambda *_: set())
    assert service.read_review_snapshot(owner, review["id"])["content"] == "owner authorship"
    assert service.list_reviews(owner)[0]["can_review"] is False
    denied(403, service.decide_review, owner, review["id"], "approve")
    with service.db.transaction() as connection:
        authors = json.loads(connection.execute("SELECT lineage_authors FROM review_snapshots WHERE review_id=?", (review["id"],)).fetchone()[0])
    assert set(authors) == {owner, alice}


def test_review_reads_and_decisions_never_load_source_versions(world, monkeypatch):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    original_get = SQLiteResearchRepository.get

    def no_source_get(repository, entity, key):
        assert (entity, key) not in {
            ("versions", version_id), ("documents", document["id"]),
            ("document_withdrawals", document["id"]), ("spaces", private["id"]),
        }
        return original_get(repository, entity, key)

    def forbidden(*args, **kwargs):
        pytest.fail("Review operations must not load source versions or live lineage")

    monkeypatch.setattr(SQLiteResearchRepository, "get", no_source_get)
    monkeypatch.setattr(SQLiteResearchRepository, "version_details", forbidden)
    monkeypatch.setattr(SQLiteResearchRepository, "lineage_authors", forbidden)
    assert service.list_reviews(owner)[0]["id"] == review["id"]
    assert service.read_review_snapshot(owner, review["id"])["content"] == "submitted evidence"
    assert service.decide_review(owner, review["id"], "approve")["status"] == "approved"


def test_rejection_retry_and_conflict_require_a_new_version_for_resubmission(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    denied(422, service.decide_review, owner, review["id"], "reject", "  ")
    denied(422, service.decide_review, owner, review["id"], "publish", "reason")
    rejected = service.decide_review(owner, review["id"], "reject", "Need reproducible evidence")
    assert rejected["published_version_id"] is None and rejected["can_review"] is False
    assert service.decide_review(owner, review["id"], "reject", "Need reproducible evidence") == rejected
    denied(409, service.decide_review, owner, review["id"], "approve")
    denied(409, service.decide_review, owner, review["id"], "reject", "Different reason")
    assert service.submit_review(alice, version_id)["status"] == "rejected"
    revised = service.add_version(alice, document["id"], "revised evidence")
    next_review = service.submit_review(alice, revised["id"])
    assert next_review["id"] != review["id"]
    assert next_review["number"] == 2 and next_review["status"] == "pending"
    assert len(service.list_reviews(alice)) == 2


def test_task_submission_retry_is_idempotent_and_task_identity_cannot_change(world):
    service, owner, alice, bob = world
    task = service.create_task(owner, "送审任务", assignee_id=alice)
    private, document, version_id, review = submitted_private(world, task_id=task["id"])
    assert service.submit_review(alice, version_id, task["id"]) == review
    denied(409, service.submit_review, alice, version_id)
    other = service.create_task(owner, "另一个任务", assignee_id=alice)
    denied(409, service.submit_review, alice, version_id, other["id"])
    service.grant_space(alice, private["id"], bob, "editor")
    denied(403, service.submit_review, bob, version_id, task["id"])
    new_version = service.add_version(bob, document["id"], "new task evidence")
    denied(403, service.submit_review, bob, new_version["id"], task["id"])
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1


def test_concurrent_retries_create_one_snapshot_and_one_published_copy(world):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    document = service.add_document(alice, private["id"], "并发送审", "exactly one copy")
    version_id = document["latest_version"]["id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        submitted = list(pool.map(lambda _: service.submit_review(alice, version_id), range(3)))
    assert len({review["id"] for review in submitted}) == 1
    review_id = submitted[0]["id"]
    with ThreadPoolExecutor(max_workers=3) as pool:
        approved = list(pool.map(lambda _: service.decide_review(owner, review_id, "approve"), range(3)))
    assert len({review["published_version_id"] for review in approved}) == 1
    assert len(service.list_documents(owner, space_of(service, owner, "results")["id"])) == 1
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1


def test_concurrent_conflicting_decisions_have_one_winner_and_one_snapshot(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)

    def decide(decision):
        from app.domain.errors import DomainError
        try:
            return service.decide_review(owner, review["id"], decision, "Decision reason")
        except DomainError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(decide, ("approve", "reject")))
    winner = next(outcome for outcome in outcomes if isinstance(outcome, dict))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert 409 in outcomes
    results = space_of(service, owner, "results")
    assert len(service.list_documents(owner, results["id"])) == int(winner["status"] == "approved")
    assert service.read_review_snapshot(alice, review["id"])["content"] == "submitted evidence"
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1


def test_missing_frozen_snapshot_fails_closed_instead_of_reloading_source(world, monkeypatch):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    original_get = SQLiteResearchRepository.get

    def missing_snapshot(repository, entity, key):
        return None if entity == "review_snapshots" else original_get(repository, entity, key)

    monkeypatch.setattr(SQLiteResearchRepository, "get", missing_snapshot)
    denied(409, service.list_reviews, owner)
    denied(409, service.read_review_snapshot, owner, review["id"])
    denied(409, service.decide_review, owner, review["id"], "approve")
    assert service.list_documents(owner, space_of(service, owner, "results")["id"]) == []


def test_submission_snapshot_and_audit_roll_back_together(world, monkeypatch):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    document = service.add_document(alice, private["id"], "原子送审", "evidence")
    original_add = SQLiteResearchRepository.add

    def fail_snapshot(repository, entity, record):
        if entity == "review_snapshots":
            raise RuntimeError("snapshot write failed")
        return original_add(repository, entity, record)

    monkeypatch.setattr(SQLiteResearchRepository, "add", fail_snapshot)
    with pytest.raises(RuntimeError, match="snapshot write failed"):
        service.submit_review(alice, document["latest_version"]["id"])
    assert service.list_reviews(owner) == []
    assert not any(row["action"] == "review_submitted" for row in service.audit(owner))
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 0


def test_publication_failure_rolls_back_result_review_and_audit(world, monkeypatch):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    before_audit = service.audit(owner)
    results = space_of(service, owner, "results")
    original_event = service._event

    def fail_approval_event(repository, actor, action, resource_type, resource_id):
        if action == "review_approved":
            raise RuntimeError("approval audit failed")
        return original_event(repository, actor, action, resource_type, resource_id)

    monkeypatch.setattr(service, "_event", fail_approval_event)
    with pytest.raises(RuntimeError, match="approval audit failed"):
        service.decide_review(owner, review["id"], "approve")
    assert service.list_reviews(alice)[0]["status"] == "pending"
    assert service.list_reviews(alice)[0]["published_version_id"] is None
    assert service.list_documents(owner, results["id"]) == []
    assert service.audit(owner) == before_audit
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1
    monkeypatch.setattr(service, "_event", original_event)
    assert service.decide_review(owner, review["id"], "approve")["status"] == "approved"


def test_snapshot_database_and_domain_records_are_immutable(world):
    service, owner, alice, bob = world
    private, document, version_id, review = submitted_private(world)
    with service._uow_factory() as uow:
        row = uow.repository.get("review_snapshots", review["id"])
    snapshot = ReviewSnapshot.from_record(row)
    with pytest.raises(FrozenInstanceError):
        snapshot.content = "overwrite"
    detached = snapshot.record()
    detached["lineage_authors"].append(owner)
    assert owner not in snapshot.lineage_authors
    assert snapshot.content == "submitted evidence"
    with service.db.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE review_snapshots SET content='overwrite' WHERE review_id=?", (review["id"],))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM review_snapshots WHERE review_id=?", (review["id"],))
    assert service.read_review_snapshot(owner, review["id"])["content"] == "submitted evidence"
    audit = json.dumps(service.audit(owner), ensure_ascii=False)
    for value in ("submitted evidence", "冻结标题", "local notes", "paper.md"):
        assert value not in audit
