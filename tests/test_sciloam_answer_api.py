"""Stage-four answer states on real authorized HTTP resources, with no external AI."""
import pytest
from fastapi.testclient import TestClient

from app.presentation.server import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    for key, value in {"LLM_API_KEY": "", "RESEARCH_ENV": "development", "RESEARCH_COOKIE_SECURE": "0", "RESEARCH_ALLOWED_HOSTS": "testserver,127.0.0.1,localhost", "RESEARCH_RETRIEVAL_MODE": "local", "RESEARCH_RAG_ALLOW_EXTERNAL": "0"}.items():
        monkeypatch.setenv(key, value)
    with TestClient(create_app(tmp_path / "db.sqlite", tmp_path / "frontend")) as client:
        client.app.state.service.bootstrap("owner@example.org", "stage-four-password-123", "可信知识问答")
        def post(path, body):
            token = client.get("/api/auth/me").json()["csrf_token"]
            return client.post(path, json=body, headers={"X-CSRF-Token": token})
        assert post("/api/auth/login", {"email": "owner@example.org", "password": "stage-four-password-123"}).status_code == 200
        client.write = post
        yield client


def ask(client, space_id, question, conversation=None):
    conversation = conversation or client.write("/api/conversations", {"space_id": space_id}).json()["id"]
    result = client.write("/api/chat", {"space_id": space_id, "conversation_id": conversation, "question": question, "mode": "qa"})
    assert result.status_code == 200, result.text
    return result.json(), conversation


def test_absent_or_weak_evidence_is_not_forced_into_a_sourced_answer(client):
    space = next(s for s in client.get("/api/spaces").json() if s["kind"] == "private")
    empty, _ = ask(client, space["id"], "细胞培养基的保存温度是多少？")
    assert empty["answer_status"] == "insufficient"
    assert empty["citations"] == []
    client.write(f"/api/spaces/{space['id']}/documents", {"title": "检索系统实验", "content": "本实验采用召回率和精确率评价文档检索，数据版本为v2。"})
    weak, conversation = ask(client, space["id"], "细胞培养实验的温度条件是多少？")
    assert weak["answer_status"] == "insufficient", weak
    assert weak["citations"] == []
    assert "召回率" not in weak["answer"]
    assert set(weak["suggested_actions"]) <= {"switch_library", "add_knowledge", "rephrase_question"}
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert history[-1]["answer_status"] == "insufficient"
    assert history[-1]["citations"] == []


def test_unconfigured_model_does_not_force_local_excerpt_and_history_matches(client):
    space = next(s for s in client.get("/api/spaces").json() if s["kind"] == "private")
    doc = client.write(f"/api/spaces/{space['id']}/documents", {"title": "评价指标", "content": "检索评价指标包括召回率。当前记录没有给出数值。"}).json()
    result, conversation = ask(client, space["id"], "评价指标")
    assert result["answer_status"] == "insufficient", result
    assert result["fallback"] is True
    assert result["citations"] == []
    assert "未配置" in result["answer"] and "召回率" not in result["answer"]
    history = client.get(f"/api/conversations/{conversation}").json()["messages"]
    assert history[-1]["answer_status"] == "insufficient"
    assert history[-1]["content"] == result["answer"]
    assert history[-1]["fallback"] is True
    assert history[-1]["events"]


def test_switching_space_cannot_reuse_previous_conversation_or_evidence(client):
    personal = next(s for s in client.get("/api/spaces").json() if s["kind"] == "private")
    _, conversation = ask(client, personal["id"], "评价指标")
    other = client.write("/api/spaces", {"name": "第二知识库"}).json()
    response = client.write("/api/chat", {"space_id": other["id"], "conversation_id": conversation, "question": "评价指标", "mode": "qa"})
    assert response.status_code == 403
