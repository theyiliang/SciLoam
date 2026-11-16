from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app


@pytest.fixture()
def clients(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_ENV", "development")
    monkeypatch.setenv("RESEARCH_COOKIE_SECURE", "0")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("RESEARCH_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
    monkeypatch.setenv("RESEARCH_RETRIEVAL_MODE", "local")
    monkeypatch.setenv("RESEARCH_RAG_ALLOW_EXTERNAL", "0")
    app = create_app(tmp_path / "api.db", tmp_path / "frontend")
    with TestClient(app) as owner, TestClient(app) as member:
        # Each lifespan owns the same file; no real user data is touched.
        app.state.service.bootstrap("owner@example.org", "owner-password-123", "实验课题")
        yield owner, member


def write(client, url, body=None, method="POST"):
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    return client.request(method, url, json=body, headers={"X-CSRF-Token": csrf})


def login(client):
    response = write(client, "/api/auth/login", {"email": "owner@example.org", "password": "owner-password-123"})
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    return response.json()["user"]


def test_complete_research_workflow(clients):
    owner, member = clients
    owner_user = login(owner)
    invitation = write(owner, "/api/invites", {"email": "member@example.org"}).json()
    registered = write(member, "/api/auth/register", {"email": "member@example.org", "password": "member-password-123", "invite_token": invitation["invite_token"]})
    assert registered.status_code == 200, registered.text
    assert member.get("/api/auth/me").json()["user"]["email"] == "member@example.org"
    member_id = registered.json()["user"]["id"]
    private = next(s for s in member.get("/api/spaces").json() if s["kind"] == "private")
    document = write(member, f"/api/spaces/{private['id']}/documents", {"title": "实验记录", "content": "样本研究使用数据集v2，评价指标为召回率。"}).json()
    version = document["latest_version"]["id"]
    assert owner.get(f"/api/versions/{version}").status_code == 403
    collab = write(member, "/api/spaces", {"name": "任务协作"}).json()
    assert owner.get(f"/api/spaces/{collab['id']}/documents").status_code == 403
    copied = write(member, f"/api/versions/{version}/share", {"target_space_id": collab["id"]})
    assert copied.status_code == 200, copied.text
    copy_version = copied.json()["latest_version"]["id"]
    review = write(member, "/api/reviews", {"version_id": version}).json()
    snapshot = owner.get(f"/api/reviews/{review['id']}/snapshot")
    assert snapshot.status_code == 200
    assert snapshot.json()["content"] == "样本研究使用数据集v2，评价指标为召回率。"
    assert owner.get(f"/api/versions/{version}").status_code == 403
    assert owner.get(f"/api/versions/{copy_version}").status_code == 403
    forbidden = write(member, f"/api/reviews/{review['id']}/decision", {"decision": "approved"})
    assert forbidden.status_code == 403
    approved = write(owner, f"/api/reviews/{review['id']}/decision", {"decision": "approved", "comment": "允许归档"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"
    results = next(s for s in member.get("/api/spaces").json() if s["kind"] == "results")
    assert len(member.get(f"/api/spaces/{results['id']}/documents").json()) == 1
    conv = write(member, "/api/conversations", {"space_id": results["id"]}).json()
    answer = write(member, "/api/chat", {"space_id": results["id"], "conversation_id": conv["id"], "question": "评价指标", "mode": "qa"})
    assert answer.status_code == 200, answer.text
    assert answer.json()["fallback"] is True
    assert answer.json()["citations"] == []
    assert "未配置" in answer.json()["answer"]
    assert "召回率" not in answer.json()["answer"]
    assert len(member.get(f"/api/conversations/{conv['id']}").json()["messages"]) == 2
    task = write(owner, "/api/tasks", {"title": "评价实验", "assignee_id": member_id}).json()
    assert write(member, f"/api/tasks/{task['id']}", {"status": "in_progress"}, "PATCH").status_code == 200
    assert write(member, f"/api/tasks/{task['id']}/progress", {"content": "完成初步实验"}).status_code == 200
    assert len(member.get(f"/api/tasks/{task['id']}/progress").json()) == 1
    assert write(member, f"/api/tasks/{task['id']}", {"status": "pending_acceptance"}, "PATCH").status_code == 200
    assert write(owner, f"/api/tasks/{task['id']}", {"status": "completed"}, "PATCH").status_code == 200
    assert owner.get("/api/audit").status_code == 200
    assert member.get("/api/audit").status_code == 403
    assert write(owner, f"/api/members/{member_id}", method="DELETE").status_code == 200
    assert member.get("/api/spaces").status_code == 401
    assert owner_user["role"] == "owner"


def test_auth_csrf_validation_and_legacy_isolation(clients):
    client, _ = clients
    assert client.get("/api/spaces").status_code == 401
    assert client.post("/api/auth/login", json={"email": "x", "password": "x"}).status_code == 403
    response = write(client, "/api/auth/login", {"email": "owner@example.org", "password": "SECRET", "role": "owner"})
    assert response.status_code == 422
    assert "SECRET" not in response.text
    assert client.get("/library/records", params={"q": "secret"}).status_code == 404
    assert client.get("/health").json()["product"] == "research"
    assert "成果审核" in {tag["name"] for tag in client.get("/openapi.json").json()["tags"]} if "tags" in client.get("/openapi.json").json() else True
    login(client)
    assert write(client, "/api/auth/logout").status_code == 200
    assert client.get("/api/spaces").status_code == 401


def test_csrf_is_stable_across_tabs_and_password_is_not_trimmed(clients):
    client, _ = clients
    first = client.get("/api/auth/me").json()["csrf_token"]
    second = client.get("/api/auth/me").json()["csrf_token"]
    assert first == second
    service = client.app.state.service
    owner = service.login("owner@example.org", "owner-password-123")["user"]
    service.reset_password(owner["id"], owner["id"], "  preserved password  ")
    response = write(client, "/api/auth/login", {"email": "owner@example.org", "password": "  preserved password  "})
    assert response.status_code == 200


def test_idempotency_is_connected_to_real_api(clients):
    client, _ = clients
    login(client)
    csrf = client.get("/api/auth/me").json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "resource-test-operation-001"}
    space = next(s for s in client.get("/api/spaces").json() if s["kind"] == "private")
    route = f"/api/spaces/{space['id']}/documents"
    body = {"title": "幂等资料", "content": "相同请求只生成一个版本"}
    first = client.post(route, json=body, headers=headers)
    second = client.post(route, json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers["Idempotency-Replayed"] == "true"
    assert len(client.get(route).json()) == 1
    assert client.post(route, json={**body, "content": "changed"}, headers=headers).status_code == 409


def test_self_service_password_change_requires_old_password_and_revokes_sessions(clients):
    owner, member = clients
    login(owner)
    login(member)
    wrong = write(owner, "/api/auth/password", {"current_password": "wrong", "new_password": "replacement-password-456"})
    assert wrong.status_code == 401
    assert owner.get("/api/auth/me").json()["user"] is not None
    changed = write(owner, "/api/auth/password", {"current_password": "owner-password-123", "new_password": "replacement-password-456"})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"password_changed": True, "reauthenticate": True}
    assert owner.get("/api/spaces").status_code == 401
    assert member.get("/api/spaces").status_code == 401
    assert "owner-password-123" not in changed.text
    assert write(owner, "/api/auth/login", {"email": "owner@example.org", "password": "replacement-password-456"}).status_code == 200


def test_personal_sharing_is_explicit_and_revocation_invalidates_history(clients):
    owner, member = clients
    owner_user = login(owner)
    invitation = write(owner, "/api/invites", {"email": "member@example.org"}).json()
    registered = write(member, "/api/auth/register", {"email": "member@example.org", "password": "member-password-123", "invite_token": invitation["invite_token"]})
    assert registered.status_code == 200
    private = next(s for s in member.get("/api/spaces").json() if s["kind"] == "private")
    document = write(member, f"/api/spaces/{private['id']}/documents", {"title": "个人知识", "content": "授权后才可见"}).json()
    version_id = document["latest_version"]["id"]
    assert owner.get(f"/api/versions/{version_id}").status_code == 403
    grant = f"/api/spaces/{private['id']}/members/{owner_user['id']}"
    assert write(member, grant, {"role": "reader"}, "PUT").status_code == 200
    assert owner.get(f"/api/versions/{version_id}").status_code == 200
    assert write(owner, f"/api/spaces/{private['id']}/documents", {"title": "越权编辑", "content": "不允许"}).status_code == 403
    conversation = write(owner, "/api/conversations", {"space_id": private["id"]}).json()
    assert write(member, grant, method="DELETE").status_code == 200
    assert owner.get(f"/api/versions/{version_id}").status_code == 403
    assert owner.get(f"/api/conversations/{conversation['id']}").status_code == 403


def test_production_requires_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_ENV", "production")
    monkeypatch.setenv("RESEARCH_COOKIE_SECURE", "0")
    with pytest.raises(RuntimeError, match="HTTPS"):
        create_app(tmp_path / "db")
