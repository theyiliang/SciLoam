"""Real HTTP goal planning, contributor freedom and source-knowledge isolation."""
import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key, value in {"LLM_API_KEY": "", "RESEARCH_ENV": "development", "RESEARCH_COOKIE_SECURE": "0", "RESEARCH_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1", "RESEARCH_RETRIEVAL_MODE": "local", "RESEARCH_RAG_ALLOW_EXTERNAL": "0"}.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path / "db.sqlite", tmp_path / "frontend")
    with TestClient(app) as owner, TestClient(app) as member:
        app.state.service.bootstrap("owner@example.org", "goal-owner-password-123", "课题目标")
        def write(client, path, body=None, method="POST", key=None):
            csrf = client.get("/api/auth/me").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            if key: headers["Idempotency-Key"] = key
            return client.request(method, path, json=body, headers=headers)
        assert write(owner, "/api/auth/login", {"email": "owner@example.org", "password": "goal-owner-password-123"}).status_code == 200
        invitation = write(owner, "/api/invites", {"email": "member@example.org"}).json()
        assert write(member, "/api/auth/register", {"email": "member@example.org", "password": "goal-member-password-123", "invite_token": invitation["invite_token"]}).status_code == 200
        yield owner, member, write


def test_tree_planning_without_assignment_and_revision_fences(world):
    owner, member, write = world
    assert write(member, "/api/goals", {"title": "越权"}).status_code == 403
    root = write(owner, "/api/goals", {"title": "课题总目标", "acceptance_criteria": "核验所有研究目标"}, key="root-create-key")
    assert root.status_code == 200, root.text
    replay = write(owner, "/api/goals", {"title": "课题总目标", "acceptance_criteria": "核验所有研究目标"}, key="root-create-key")
    assert replay.status_code == 200, replay.text
    assert replay.headers["Idempotency-Replayed"] == "true"
    root = root.json()
    assert "assignee_id" not in root
    child = write(owner, "/api/goals", {"title": "验收目标", "parent_id": root["id"]}).json()
    assert member.get("/api/goals").json()[1]["path"] == "课题总目标 / 验收目标"
    assert write(member, f"/api/goals/{child['id']}", {"title": "越权", "expected_revision": child["revision"]}, "PATCH").status_code == 403
    changed = write(owner, f"/api/goals/{child['id']}", {"description": "课题全组可读的说明", "expected_revision": child["revision"]}, "PATCH")
    assert changed.status_code == 200, changed.text
    assert write(owner, f"/api/goals/{child['id']}", {"description": "旧编辑", "expected_revision": child["revision"]}, "PATCH").status_code == 409
    assert write(owner, f"/api/goals/{root['id']}", {"parent_id": child["id"], "expected_revision": root["revision"]}, "PATCH").status_code == 409
    assert write(member, f"/api/goals/{child['id']}/status", {"status": "in_progress", "reason": "尝试", "expected_revision": 2}).status_code == 403


def test_private_link_never_grants_access_and_goal_submission_needs_no_assignee(world):
    owner, member, write = world
    goal = write(owner, "/api/goals", {"title": "验收目标"}).json()
    private = next(s for s in member.get("/api/spaces").json() if s["kind"] == "private")
    doc = write(member, f"/api/spaces/{private['id']}/documents", {"title": "PRIVATE TITLE", "content": "PRIVATE EVIDENCE BODY"}).json()
    version = doc["latest_version"]["id"]
    link = write(member, f"/api/goals/{goal['id']}/knowledge", {"version_id": version}, key="link-knowledge-key")
    assert link.status_code == 200, link.text
    assert write(member, f"/api/goals/{goal['id']}/knowledge", {"version_id": version}, key="link-knowledge-key").headers["Idempotency-Replayed"] == "true"
    owner_view = owner.get(f"/api/goals/{goal['id']}")
    assert owner_view.json()["knowledge"] == []
    assert "PRIVATE TITLE" not in owner_view.text and version not in owner_view.text
    assert owner.get(f"/api/versions/{version}").status_code == 403
    update = write(member, f"/api/goals/{goal['id']}/updates", {"content": "可公开的研究进展"}, key="goal-update-key")
    assert update.status_code == 200, update.text
    assert write(member, f"/api/goals/{goal['id']}/updates", {"content": "可公开的研究进展"}, key="goal-update-key").headers["Idempotency-Replayed"] == "true"
    private_update = write(member, f"/api/goals/{goal['id']}/updates", {"content": "PRIVATE UPDATE", "version_id": version})
    assert private_update.status_code == 200
    assert "PRIVATE UPDATE" not in owner.get(f"/api/goals/{goal['id']}").text
    review = write(member, "/api/reviews", {"version_id": version, "goal_id": goal["id"]})
    assert review.status_code == 200, review.text
    assert review.json()["goal_title"] == "验收目标"
    assert review.json()["goal_id"] == goal["id"]
    assert write(owner, f"/api/reviews/{review.json()['id']}/decision", {"decision": "approved"}).status_code == 200
    assert owner.get(f"/api/goals/{goal['id']}").json()["status"] == "not_started"
    assert owner.get(f"/api/versions/{version}").status_code == 403
    shared = write(member, "/api/spaces", {"name": "协作知识", "goal_id": goal["id"]})
    assert shared.status_code == 200, shared.text
    assert shared.json()["goal_id"] == goal["id"]
    assert owner.get(f"/api/spaces/{shared.json()['id']}/documents").status_code == 403
