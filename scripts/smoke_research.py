"""Run the real research HTTP server with temporary accounts/data; no external AI calls."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import http.cookiejar

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    from app.infrastructure.persistence.factory import build_research_service
    with tempfile.TemporaryDirectory(prefix="research-smoke-") as directory:
        db = Path(directory) / "research.db"
        build_research_service(db).bootstrap("owner@example.org", "smoke-owner-pass-123", "验证课题")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, RESEARCH_DATA_DIR=directory, RESEARCH_ENV="development", RESEARCH_COOKIE_SECURE="0", RESEARCH_ALLOWED_HOSTS="localhost,127.0.0.1", LLM_API_KEY="", RESEARCH_RETRIEVAL_MODE="local", RESEARCH_RAG_ALLOW_EXTERNAL="0", PYTHONUTF8="1")
        with (Path(directory) / "server.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.presentation.server:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            base = f"http://127.0.0.1:{port}"
            owner = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            member = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            collaborator = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def request(client, path, body=None, method=None):
                headers = {}
                if body is not None or method in ("POST", "PUT", "PATCH", "DELETE"):
                    csrf = request(client, "/api/auth/me")["csrf_token"]
                    headers = {"Content-Type": "application/json", "X-CSRF-Token": csrf}
                req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
                with client.open(req, timeout=30) as response:
                    return json.load(response)

            try:
                deadline = time.monotonic() + 60
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("API启动失败")
                    try:
                        health = request(owner, "/health")
                        break
                    except (OSError, urllib.error.URLError):
                        if time.monotonic() > deadline:
                            raise RuntimeError("API启动超时")
                        time.sleep(.25)
                assert health["product"] == "research"
                request(owner, "/api/auth/login", {"email": "owner@example.org", "password": "smoke-owner-pass-123"})
                invite = request(owner, "/api/invites", {"email": "student@example.org"})
                member_account = request(member, "/api/auth/register", {"email": "student@example.org", "password": "smoke-member-pass-123", "invite_token": invite["invite_token"]})
                invite = request(owner, "/api/invites", {"email": "collaborator@example.org"})
                collaborator_account = request(collaborator, "/api/auth/register", {"email": "collaborator@example.org", "password": "smoke-collaborator-pass-123", "invite_token": invite["invite_token"]})

                def forbidden(client, path, body=None, method=None, statuses=(403,)):
                    try:
                        request(client, path, body, method)
                    except urllib.error.HTTPError as error:
                        assert error.code in statuses, (path, error.code)
                    else:
                        raise AssertionError("Unexpected access: " + path)

                spaces = request(member, "/api/spaces")
                private = next(s for s in spaces if s["kind"] == "private")
                doc = request(member, f"/api/spaces/{private['id']}/documents", {"title": "实验记录", "content": "研究评价采用召回率，实验使用数据集v2。"})
                version = doc["latest_version"]["id"]
                try:
                    request(owner, f"/api/versions/{version}")
                    raise AssertionError("私有资料被越权读取")
                except urllib.error.HTTPError as error:
                    assert error.code == 403
                forbidden(collaborator, f"/api/versions/{version}")
                basis = next(s for s in request(owner, "/api/spaces") if s["kind"] == "basis")
                proposal = request(owner, f"/api/spaces/{basis['id']}/documents", {
                    "title": "立项要求", "content": "# 研究目标\n1. 建立评价方法\n交付物：形成研究报告。\n约束：不得使用未授权数据。"})
                draft = request(owner, "/api/goal-drafts", {"version_ids": [proposal["latest_version"]["id"]]})
                forbidden(member, "/api/goal-drafts", {"version_ids": [proposal["latest_version"]["id"]]})
                forbidden(owner, f"/api/goal-drafts/{draft['id']}/publish", {"expected_revision": draft["revision"], "confirmed": False}, statuses=(422,))
                receipt = request(owner, f"/api/goal-drafts/{draft['id']}/publish", {"expected_revision": draft["revision"], "confirmed": True})
                assert request(owner, f"/api/goal-drafts/{draft['id']}/publish", {"expected_revision": draft["revision"], "confirmed": True}) == receipt
                before_tree = request(owner, "/api/goals")
                revised_proposal = request(owner, f"/api/documents/{proposal['id']}/versions", {
                    "content": "# 研究目标\n1. 建立评价方法\n交付物：形成研究报告及数据说明。\n约束：不得使用未授权数据。"})
                assert request(owner, "/api/goal-drafts"), "An old source must not block the entire draft list"
                published_draft = request(owner, f"/api/goal-drafts/{draft['id']}")
                changes = request(owner, f"/api/goal-drafts/{draft['id']}/regenerate", {
                    "version_ids": [revised_proposal["id"]], "expected_revision": published_draft["revision"]})
                assert changes["suggestions"], "Regeneration must produce explicit suggestions"
                assert request(owner, "/api/goals") == before_tree, "Generating suggestions changed the published tree"
                assert all(suggestion["status"] == "pending" for suggestion in changes["suggestions"])
                goal = request(member, f"/api/goals/{receipt['goal_ids'][0]}")
                request(member, f"/api/goals/{goal['id']}/knowledge", {"version_id": version})
                assert version not in json.dumps(request(owner, f"/api/goals/{goal['id']}"))
                collaborator_id = collaborator_account["user"]["id"]
                request(member, f"/api/spaces/{private['id']}/members/{collaborator_id}", {"role": "reader"}, "PUT")
                assert request(collaborator, f"/api/versions/{version}")["content"] == "研究评价采用召回率，实验使用数据集v2。"
                request(member, f"/api/spaces/{private['id']}/members/{collaborator_id}", method="DELETE")
                forbidden(collaborator, f"/api/versions/{version}")
                collab = request(member, "/api/spaces", {"name": "实验协作知识", "goal_id": goal["id"]})
                request(member, f"/api/spaces/{collab['id']}/members/{collaborator_id}", {"role": "reader"}, "PUT")
                published = request(member, f"/api/versions/{version}/share", {"target_space_id": collab["id"]})
                assert len(request(collaborator, f"/api/spaces/{collab['id']}/documents")) == 1
                forbidden(owner, f"/api/spaces/{collab['id']}/documents")
                review = request(member, "/api/reviews", {"version_id": version, "goal_id": goal["id"]})
                forbidden(collaborator, f"/api/reviews/{review['id']}/snapshot", statuses=(403, 404))
                snapshot = request(owner, f"/api/reviews/{review['id']}/snapshot")
                assert snapshot["content"] == "研究评价采用召回率，实验使用数据集v2。"
                try:
                    request(owner, f"/api/versions/{version}")
                    raise AssertionError("审核不应授予原知识库权限")
                except urllib.error.HTTPError as error:
                    assert error.code == 403
                approved = request(owner, f"/api/reviews/{review['id']}/decision", {"decision": "approved", "comment": "允许归档"})
                assert approved["status"] == "approved"
                assert request(owner, f"/api/goals/{goal['id']}")["status"] == "not_started"
                request(member, f"/api/documents/{doc['id']}", method="DELETE")
                assert request(owner, f"/api/reviews/{review['id']}/snapshot")["content"] == "研究评价采用召回率，实验使用数据集v2。"
                results = next(s for s in request(member, "/api/spaces") if s["kind"] == "results")
                request(member, f"/api/spaces/{results['id']}/documents", {"title": "成果", "content": "研究评价采用召回率，实验使用数据集v2。"})
                conversation = request(member, "/api/conversations", {"space_id": results["id"]})
                answer = request(member, "/api/chat", {"space_id": results["id"], "conversation_id": conversation["id"], "question": "评价指标", "mode": "qa"})
                assert answer["fallback"] and "召回率" in answer["answer"]
                stream_conversation = request(collaborator, "/api/conversations", {"space_id": results["id"]})
                run_payload = {"space_id": results["id"], "conversation_id": stream_conversation["id"], "question": "评价指标", "mode": "qa", "request_id": "three-account-stream"}
                run = request(collaborator, "/api/chat/runs", run_payload)
                assert request(collaborator, "/api/chat/runs", run_payload)["run_id"] == run["run_id"]
                forbidden(member, f"/api/chat/runs/{run['run_id']}", statuses=(403, 404))
                events, frame = [], []
                with collaborator.open(base + f"/api/chat/runs/{run['run_id']}/events", timeout=30) as stream:
                    for raw in stream:
                        line = raw.decode("utf-8").rstrip("\r\n")
                        if line.startswith("data: "):
                            frame.append(line[6:])
                        elif not line and frame:
                            event = json.loads("\n".join(frame)); frame = []; events.append(event)
                assert events and events[-1]["type"] == "answer.final", events
                assert not any(event["type"] == "answer.delta" for event in events), "Local mode must not fake model deltas"
                assert len({event["seq"] for event in events}) == len(events)
                assert all(event["run_id"] == run["run_id"] and event["conversation_id"] == stream_conversation["id"] for event in events)
                final = events[-1]["data"]
                assert final["replace"] and final["citations"]
                history = request(collaborator, f"/api/conversations/{stream_conversation['id']}")["messages"]
                assert len([message for message in history if message["role"] == "assistant"]) == 1
                assert request(collaborator, "/api/chat/runs", run_payload)["run_id"] == run["run_id"]
                with owner.open(base + "/", timeout=10) as page:
                    assert "SciLoam" in page.read().decode("utf-8")
                print(json.dumps({"http": "ok", "accounts": 3, "private_isolation": "ok", "grant_revoke": "ok", "goal_confirm_publish": "ok", "review_publish": "ok", "source_withdrawal_snapshot": "ok", "sse_final_once": "ok", "sse_events": len(events), "local_evidence": "ok", "external_llm_called": False}, ensure_ascii=False))
            except BaseException:
                log.flush(); log.seek(0); print(log.read(), file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT) if os.name == "nt" else process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=10)


if __name__ == "__main__":
    main()
