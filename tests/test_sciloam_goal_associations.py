"""Stage-six goal associations and metadata-only AI progress boundaries."""
import json

import pytest

from app.application.agents.evidence import _Request
from app.application.agents.legacy_orchestrator import ResearchAssistant
from app.application.agents.tools import describe_agents
from app.domain.errors import DomainError
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from tests.test_research_service import denied, space_of, world
from tests.test_sciloam_answerability import EvidenceService, OfflineRuntime


def private_version(service, actor, title="Submitted evidence"):
    private = space_of(service, actor, "private")
    document = service.add_document(actor, private["id"], title, "FROZEN-PRIVATE-BODY")
    return private, document["latest_version"]["id"]


def test_space_goal_association_does_not_grant_membership_or_source_access(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Public goal")
    space = service.create_space(alice, "Goal collaboration", goal_id=goal["id"])
    assert space["goal_id"] == goal["id"] and space["task_id"] is None
    version = service.add_document(alice, space["id"], "Private research", "SECRET")
    for actor in (owner, bob):
        assert space["id"] not in {item["id"] for item in service.list_spaces(actor)}
        denied(403, service.get_version, actor, version["latest_version"]["id"])
    assert service.get_goal(bob, goal["id"])["status"] == "not_started"


def test_space_association_active_auth_and_kind_guards_preserve_old_task_api(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Goal")
    task = service.create_task(owner, "Old task", assignee_id=alice)
    legacy = service.create_space(bob, "Old API", "collaboration", task["id"])
    assert legacy["task_id"] == task["id"]
    denied(422, service.create_space, alice, "Both", task_id=task["id"], goal_id=goal["id"])
    denied(422, service.create_space, owner, "Bad kind", "results", goal_id=goal["id"])
    denied(404, service.create_space, alice, "Missing", goal_id="missing")
    denied(401, service.create_space, "missing-user", "Unauthenticated", goal_id=goal["id"])
    service.archive_goal(owner, goal["id"], goal["revision"], "Retained history")
    denied(409, service.create_space, alice, "Archived", goal_id=goal["id"])


def test_goal_review_has_no_assignee_requirement_or_source_permission_escalation(world):
    service, owner, alice, bob = world
    parent = service.create_goal(owner, "Public parent")
    goal = service.create_goal(owner, "Public child", parent_id=parent["id"])
    private, version_id = private_version(service, alice)
    review = service.submit_review(alice, version_id, goal_id=goal["id"])
    assert review["task_id"] is None
    assert review["goal_id"] == goal["id"]
    assert review["goal_title"] == "Public child"
    assert review["goal_path"] == "Public parent / Public child"
    assert review["goal_archived"] is False
    assert "content" not in review
    denied(403, service.get_version, owner, version_id)
    denied(403, service.get_version, bob, version_id)
    denied(403, service.read_review_snapshot, bob, review["id"])
    assert service.read_review_snapshot(owner, review["id"])["content"] == "FROZEN-PRIVATE-BODY"
    approved = service.decide_review(owner, review["id"], "approve")
    assert service.get_version(bob, approved["published_version_id"])["content"] == "FROZEN-PRIVATE-BODY"
    assert service.get_goal(owner, goal["id"])["status"] == "not_started"
    assert service.get_goal(owner, goal["id"])["status_history"] == []
    assert private["id"] not in {item["id"] for item in service.list_spaces(owner)}


def test_goal_review_idempotency_includes_goal_and_retains_frozen_retry_after_archive(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "First goal")
    other = service.create_goal(owner, "Second goal")
    private, version_id = private_version(service, alice)
    service.grant_space(alice, private["id"], bob, "editor")
    review = service.submit_review(bob, version_id, goal_id=goal["id"])
    assert service.submit_review(bob, version_id, goal_id=goal["id"]) == review
    denied(409, service.submit_review, bob, version_id)
    denied(409, service.submit_review, bob, version_id, goal_id=other["id"])
    denied(422, service.submit_review, bob, version_id, task_id="task", goal_id=goal["id"])
    service.archive_goal(owner, goal["id"], goal["revision"], "Keep association history")
    service.revoke_space(alice, private["id"], bob)
    retried = service.submit_review(bob, version_id, goal_id=goal["id"])
    assert retried["id"] == review["id"] and retried["goal_archived"] is True
    assert retried["goal_title"] == "First goal"
    assert service.read_review_snapshot(owner, review["id"])["content"] == "FROZEN-PRIVATE-BODY"
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM review_snapshots").fetchone()[0] == 1


def test_new_goal_submission_requires_active_goal_and_existing_source_write_access(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Goal")
    private, version_id = private_version(service, alice)
    denied(403, service.submit_review, bob, version_id, goal_id=goal["id"])
    denied(403, service.submit_review, owner, version_id, goal_id=goal["id"])
    denied(404, service.submit_review, alice, version_id, goal_id="missing")
    service.archive_goal(owner, goal["id"], goal["revision"], "Archive")
    denied(409, service.submit_review, alice, version_id, goal_id=goal["id"])
    assert service.list_reviews(owner) == []


def test_completed_unarchived_goal_can_be_associated_without_status_change(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Completed planning record")
    for status in ("in_progress", "pending_acceptance", "completed"):
        goal = service.confirm_goal_status(owner, goal["id"], status, "Owner confirmation", goal["revision"])
    space = service.create_space(alice, "Historical association", goal_id=goal["id"])
    _, version_id = private_version(service, alice)
    review = service.submit_review(alice, version_id, goal_id=goal["id"])
    service.decide_review(owner, review["id"], "approve")
    assert space["goal_id"] == goal["id"]
    assert service.get_goal(owner, goal["id"])["revision"] == goal["revision"]


def test_goal_receipt_never_invents_missing_goal_metadata_or_reads_goal_details(world, monkeypatch):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Actual goal title")
    _, version_id = private_version(service, alice, "Not the goal title")
    review = service.submit_review(alice, version_id, goal_id=goal["id"])
    original_get = SQLiteResearchRepository.get

    def missing_goal(repository, entity, key):
        if entity == "goal_nodes" and key == goal["id"]:
            return None
        assert entity not in {"goal_knowledge", "goal_updates"}
        return original_get(repository, entity, key)

    monkeypatch.setattr(SQLiteResearchRepository, "get", missing_goal)
    receipt = service.list_reviews(owner)[0]
    assert receipt["id"] == review["id"] and receipt["goal_id"] == goal["id"]
    assert receipt["goal_title"] is None and receipt["goal_path"] is None


def test_legacy_task_review_remains_assignee_bound_and_backfill_retry_compatible(world):
    service, owner, alice, bob = world
    task = service.create_task(owner, "Legacy task", assignee_id=alice)
    private, version_id = private_version(service, alice)
    service.grant_space(alice, private["id"], bob, "editor")
    denied(403, service.submit_review, bob, version_id, task["id"])
    review = service.submit_review(alice, version_id, task["id"])
    assert service.submit_review(alice, version_id, task["id"])["id"] == review["id"]
    goal = service.create_goal(owner, "Migrated task")
    # Reproduce precisely the schema-five migration association shape.
    with service.db.transaction() as connection:
        connection.execute("UPDATE goal_nodes SET id=?,legacy_task_id=? WHERE id=?", (task["id"], task["id"], goal["id"]))
        connection.execute("UPDATE reviews SET goal_id=? WHERE id=?", (task["id"], review["id"]))
    service.revoke_space(alice, private["id"], bob)
    assert service.submit_review(alice, version_id, task["id"])["id"] == review["id"]
    denied(409, service.submit_review, alice, version_id, goal_id=task["id"])


class ExpandedGoalService(EvidenceService):
    def __init__(self):
        super().__init__()
        self.goal_allowed = True
        self.goal_calls = 0
        self.goals = [{"id": "goal-1", "title": "Public goal", "status": "in_progress",
                       "due_date": "2026-12-31", "parent_id": "parent", "path": "Parent / Public goal",
                       "archived_at": None, "description": "PRIVATE-DESCRIPTION",
                       "acceptance_criteria": "PRIVATE-CRITERIA", "assignee_id": "PRIVATE-ASSIGNEE",
                       "knowledge": [{"content": "PRIVATE-LINK-BODY", "version_id": "secret-version"}],
                       "updates": [{"content": "PRIVATE-UPDATE-BODY"}]}]

    def list_goals(self, actor):
        self.goal_calls += 1
        if not self.goal_allowed:
            raise DomainError(403, "Goal read revoked")
        return self.goals

    def list_tasks(self, actor):
        pytest.fail("A service with list_goals must never read old tasks")


async def test_progress_prefers_public_goal_records_and_omits_expanded_private_data():
    service = ExpandedGoalService()
    runtime = OfflineRuntime({"claims": [{"text": "Goal record is in progress", "task_ids": ["goal-1"]}]})
    result = await ResearchAssistant(service, runtime).answer("alice", "space", "conversation", "进度", "progress")
    assert service.goal_calls > 1 and len(runtime.calls) == 2
    for _, payload in runtime.calls:
        tasks = payload["authorized_evidence"]["tasks"]
        assert tasks == [{key: service.goals[0][key] for key in ("id", "title", "status", "due_date", "parent_id", "path")}]
    encoded = json.dumps([runtime.calls, service.messages, result], ensure_ascii=False)
    for secret in ("PRIVATE-", "secret-version"):
        assert secret not in encoded
    assert "Goal record is in progress" in result["answer"]
    assert "目标记录整理" in result["answer"] and "目标记录 goal-1" in result["answer"]
    assert "任务记录" not in result["answer"]
    assert result["citations"] == []
    assert describe_agents(_Request(service, "alice", "space", "conversation"))["ProgressAgent"].tools[0].name == "list_tasks"


async def test_progress_fallback_uses_goal_path_without_assignment_language():
    result = await ResearchAssistant(ExpandedGoalService()).answer("alice", "space", "conversation", "进度", "progress")
    assert "目标记录 goal-1：Parent / Public goal" in result["answer"]
    assert "任务" not in result["answer"] and "负责人" not in result["answer"]
    assert "PRIVATE-" not in result["answer"] and result["answer_status"] == "partial"


async def test_progress_real_service_reads_actual_goals_not_legacy_tasks(world):
    service, owner, alice, bob = world
    goal = service.create_goal(owner, "Actual public goal")
    service.create_task(owner, "OLD-TASK-MUST-NOT-APPEAR", assignee_id=alice)
    private, version_id = private_version(service, alice)
    service.link_goal_knowledge(alice, goal["id"], version_id)
    service.add_goal_update(alice, goal["id"], "PRIVATE-UPDATE-BODY", version_id)
    service.add_goal_update(alice, goal["id"], "PUBLIC-UPDATE-ALSO-OMITTED")
    conversation_space = space_of(service, bob, "private")
    conversation = service.create_conversation(bob, conversation_space["id"])
    runtime = OfflineRuntime({"claims": [{"text": "Public goal record", "task_ids": [goal["id"]]}]})
    result = await ResearchAssistant(service, runtime).answer(bob, conversation_space["id"], conversation["id"], "目标进度", "progress")
    assert runtime.calls[0][1]["authorized_evidence"]["tasks"][0]["id"] == goal["id"]
    serialized = json.dumps([runtime.calls, result], ensure_ascii=False)
    for private_data in ("OLD-TASK-MUST-NOT-APPEAR", "FROZEN-PRIVATE-BODY", "PRIVATE-UPDATE-BODY", "PUBLIC-UPDATE-ALSO-OMITTED", version_id):
        assert private_data not in serialized


async def test_progress_rechecks_goal_status_before_next_model_attempt():
    service = ExpandedGoalService()
    runtime = OfflineRuntime({"claims": []}, hook=lambda: service.goals[0].update(status="completed"))
    with pytest.raises(DomainError) as caught:
        await ResearchAssistant(service, runtime).answer("alice", "space", "conversation", "进度", "progress")
    assert caught.value.status_code == 409 and len(runtime.calls) == 1
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_progress_rechecks_goal_permission_and_never_falls_back_to_old_tasks():
    service = ExpandedGoalService()
    runtime = OfflineRuntime({"claims": []}, hook=lambda: setattr(service, "goal_allowed", False))
    with pytest.raises(DomainError) as caught:
        await ResearchAssistant(service, runtime).answer("alice", "space", "conversation", "进度", "progress")
    assert caught.value.status_code == 403 and len(runtime.calls) == 1
    assert not any(message["role"] == "assistant" for message in service.messages)


async def test_progress_legacy_list_tasks_only_adapter_still_works():
    service = EvidenceService()
    service.list_tasks = lambda actor: [{"id": "old-task", "title": "Legacy record", "status": "active", "due_date": None,
                                       "assignee_id": "alice", "content": "PRIVATE-OLD-BODY"}]
    request = _Request(service, "alice", "space", "conversation")
    records = await request.list_tasks()
    assert records[0]["id"] == "old-task" and records[0]["assignee_id"] == "alice"
    assert "content" not in records[0]
    result = await ResearchAssistant(service).answer("alice", "space", "conversation", "进度", "progress")
    assert "Legacy record" in result["answer"] and "PRIVATE-OLD-BODY" not in result["answer"]
