"""Phase-five HTTP: lifecycle changes never grant source-space access."""
import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app


@pytest.fixture
def world(tmp_path, monkeypatch):
    for key, value in {"LLM_API_KEY": "", "RESEARCH_ENV": "development", "RESEARCH_COOKIE_SECURE": "0", "RESEARCH_ALLOWED_HOSTS": "testserver,localhost,127.0.0.1", "RESEARCH_RETRIEVAL_MODE": "local", "RESEARCH_RAG_ALLOW_EXTERNAL": "0"}.items():
        monkeypatch.setenv(key, value)
    app = create_app(tmp_path / "db", tmp_path / "frontend")
    with TestClient(app) as owner, TestClient(app) as member:
        app.state.service.bootstrap("owner@example.org", "phase-five-owner-pass", "快照与生命周期")
        def send(client, path, body=None, method="POST"):
            token = client.get("/api/auth/me").json()["csrf_token"]
            return client.request(method, path, json=body, headers={"X-CSRF-Token": token})
        assert send(owner, "/api/auth/login", {"email": "owner@example.org", "password": "phase-five-owner-pass"}).status_code == 200
        invitation = send(owner, "/api/invites", {"email": "member@example.org"}).json()
        assert send(member, "/api/auth/register", {"email": "member@example.org", "password": "phase-five-member-pass", "invite_token": invitation["invite_token"]}).status_code == 200
        yield owner, member, send


def test_personal_submission_snapshot_survives_source_withdrawal_without_acl(world):
    owner, member, send = world
    space = next(s for s in member.get("/api/spaces").json() if s["kind"] == "private")
    doc = send(member, f"/api/spaces/{space['id']}/documents", {"title": "个人成果", "content": "送审时确认的正文"}).json()
    version = doc["latest_version"]["id"]
    review = send(member, "/api/reviews", {"version_id": version}).json()
    snapshot = f"/api/reviews/{review['id']}/snapshot"
    assert owner.get(snapshot).json()["content"] == "送审时确认的正文"
    assert owner.get(f"/api/versions/{version}").status_code == 403
    assert send(member, f"/api/versions/{version}", {}, "DELETE").status_code == 409
    assert send(member, f"/api/documents/{doc['id']}", method="DELETE").status_code == 200
    assert member.get(f"/api/versions/{version}").status_code == 410
    assert owner.get(snapshot).json()["content"] == "送审时确认的正文"
    approved = send(owner, f"/api/reviews/{review['id']}/decision", {"decision": "approved", "comment": "允许发布"})
    assert approved.status_code == 200, approved.text
    published = approved.json()["published_version_id"]
    assert member.get(f"/api/versions/{published}").json()["content"] == "送审时确认的正文"
    assert owner.get(f"/api/versions/{version}").status_code == 403


def test_version_deletion_requires_explicit_head_and_collaboration_owner_can_delete_space(world):
    owner, member, send = world
    space = send(member, "/api/spaces", {"name": "实验协作"}).json()
    doc = send(member, f"/api/spaces/{space['id']}/documents", {"title": "版本知识", "content": "第一版"}).json()
    first = doc["latest_version"]["id"]
    second = send(member, f"/api/documents/{doc['id']}/versions", {"content": "第二版"}).json()["id"]
    assert send(member, f"/api/versions/{second}", {}, "DELETE").status_code == 409
    deleted = send(member, f"/api/versions/{second}", {"replacement_version_id": first, "reason": "不再使用"}, "DELETE")
    assert deleted.status_code == 200, deleted.text
    docs = member.get(f"/api/spaces/{space['id']}/documents").json()
    assert docs[0]["latest_version"]["id"] == first
    assert member.get(f"/api/versions/{second}").status_code == 410
    assert send(owner, f"/api/spaces/{space['id']}", method="DELETE").status_code == 403
    removed = send(member, f"/api/spaces/{space['id']}", method="DELETE")
    assert removed.status_code == 200, removed.text
    assert space["id"] not in {s["id"] for s in member.get("/api/spaces").json()}
    assert member.get(f"/api/spaces/{space['id']}/documents").status_code == 410
    personal = next(s for s in member.get("/api/spaces").json() if s["kind"] == "private")
    assert send(member, f"/api/spaces/{personal['id']}", method="DELETE").status_code == 403
