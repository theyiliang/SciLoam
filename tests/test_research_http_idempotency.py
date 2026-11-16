"""HTTP adapter tests without modifying/registering production server routes."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from app.domain.errors import DomainError
from app.infrastructure.persistence.factory import build_research_service
from app.infrastructure.persistence.research_engineering import SQLiteResearchEngineering
from app.presentation.idempotency import execute_write


class Service:
    def __init__(self):
        self.user = {"id": "alice", "role": "member"}
        self.spaces = [{"id": "s1", "epoch": 1, "role": "editor"}]
        self.documents = [{"id": "d1", "space_id": "s1", "title": "PRIVATE title"}]
        self.versions = [{"id": "v1", "document_id": "d1", "content": "PRIVATE body"}]
        self.reviews = [{"id": "r1", "version_id": "v1", "published_version_id": None}]
        self.tasks = [{"id": "t1", "title": "task", "progress": [{"id": "p1", "task_id": "t1", "content": "PRIVATE progress"}]}]
        self.conversations = [{"id": "c1", "space_id": "s1", "messages": []}]
        self.auth_calls = 0
        self.denied = None

    def get_me(self, actor):
        self.auth_calls += 1
        if self.denied:
            raise DomainError(self.denied, "denied")
        assert actor == "alice"
        return {"user": deepcopy(self.user)}

    def list_spaces(self, actor):
        return deepcopy(self.spaces)

    def list_documents(self, actor, space_id):
        assert space_id == "s1"
        return deepcopy(self.documents)

    def get_version(self, actor, version_id):
        return deepcopy(next(row for row in self.versions if row["id"] == version_id))

    def list_reviews(self, actor):
        return deepcopy(self.reviews)

    def list_tasks(self, actor):
        return deepcopy(self.tasks)

    def get_conversation(self, actor, conversation_id):
        return deepcopy(next(row for row in self.conversations if row["id"] == conversation_id))


@pytest.fixture
def world(tmp_path):
    store = SQLiteResearchEngineering(tmp_path / "research.db")
    return Service(), store


def request(store, path="/api/spaces/s1/documents", key="unique-key", method="POST", query=b""):
    app = SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(engineering=store)))
    headers = [] if key is None else [(b"idempotency-key", key.encode())]
    return Request({"type": "http", "app": app, "method": method, "path": path,
                    "headers": headers, "query_string": query, "scheme": "http", "server": ("test", 80)})


def run(world, *, path="/api/spaces/s1/documents", key="unique-key", method="POST", payload=None, operation=None, query=b""):
    svc, store = world
    return execute_write(request(store, path, key, method, query), {"id": "alice"}, svc,
                         {"content": "PRIVATE body"} if payload is None else payload,
                         operation or (lambda: deepcopy(svc.documents[0])))


def must_not_run():
    pytest.fail("A replay must not repeat business effects")


def conflict(fn):
    with pytest.raises(DomainError) as caught:
        fn()
    assert caught.value.status_code == 409


@pytest.mark.parametrize("path,kind,attribute", [
    ("/api/spaces", "space", "spaces"),
    ("/api/spaces/s1/documents", "document", "documents"),
    ("/api/documents/d1/versions", "version", "versions"),
    ("/api/versions/v1/share", "document", "documents"),
    ("/api/reviews", "review", "reviews"),
    ("/api/reviews/r1/decision", "review", "reviews"),
    ("/api/tasks", "task", "tasks"),
    ("/api/tasks/t1/progress", "progress", "tasks"),
    ("/api/conversations", "conversation", "conversations"),
])
def test_allowlist_replays_live_resource(world, path, kind, attribute):
    svc, store = world
    response = deepcopy(getattr(svc, attribute)[0])
    if kind == "task":
        response.pop("progress")
    elif kind == "progress":
        response = response["progress"][0]
    first = run(world, path=path, operation=lambda: deepcopy(response))
    second = run(world, path=path, operation=must_not_run)
    assert json.loads(first.body) == json.loads(second.body) == response
    assert first.status_code == second.status_code == 200
    assert first.headers["Idempotency-Replayed"] == "false"
    assert second.headers["Idempotency-Replayed"] == "true"
    assert svc.auth_calls >= 4
    with sqlite3.connect(store.path) as connection:
        metadata = json.loads(connection.execute("SELECT response_metadata FROM research_request_ledger").fetchone()[0])
    assert metadata["resources"][0] == {"resource_type": kind, "resource_id": response["id"]}


def test_unkeyed_write_does_not_need_container_or_scope(world):
    svc, _ = world
    req = request(None, key=None)
    del req.app.state.container
    svc.list_spaces = must_not_run
    result = execute_write(req, "alice", svc, {}, lambda: {"ok": True})
    assert result == {"ok": True}
    assert svc.auth_calls == 1


@pytest.mark.parametrize("path,method", [
    ("/api/auth/login", "POST"), ("/api/invites", "POST"),
    ("/api/invites/accept", "POST"), ("/api/chat", "POST"),
    ("/api/tasks/t1", "PATCH"), ("/api/documents/d1", "DELETE"),
    ("/api/spaces/s1/members/alice", "PUT"),
    ("/api/tasks/extra", "POST"), ("/api/spaces/", "POST"),
])
def test_excluded_paths_never_claim(world, path, method):
    result = run(world, path=path, method=method)
    assert isinstance(result, dict)
    with sqlite3.connect(world[1].path) as connection:
        assert connection.execute("SELECT count(*) FROM research_request_ledger").fetchone()[0] == 0


@pytest.mark.parametrize("key", ["", " ", "x" * 257])
def test_invalid_key_is_domain_validation_error(world, key):
    with pytest.raises(DomainError) as caught:
        run(world, key=key, operation=must_not_run)
    assert caught.value.status_code == 422


def test_changed_payload_and_query_conflict(world):
    run(world)
    conflict(lambda: run(world, payload={"content": "different"}, operation=must_not_run))
    conflict(lambda: run(world, query=b"mode=changed", operation=must_not_run))


@pytest.mark.parametrize("change", ["epoch", "role", "membership", "user_role"])
def test_all_authorization_changes_block_replay(world, change):
    run(world)
    svc, _ = world
    if change == "epoch":
        svc.spaces[0]["epoch"] += 1
    elif change == "role":
        svc.spaces[0]["role"] = "reader"
    elif change == "membership":
        svc.spaces.append({"id": "other", "epoch": 1, "role": "reader"})
    else:
        svc.user["role"] = "owner"
    conflict(lambda: run(world, operation=must_not_run))


def test_space_order_does_not_change_scope(world):
    world[0].spaces.append({"id": "s2", "epoch": 5, "role": "reader"})
    run(world)
    world[0].spaces.reverse()
    assert run(world, operation=must_not_run).headers["Idempotency-Replayed"] == "true"


def test_replay_rechecks_scope_after_ledger_claim(world, monkeypatch):
    run(world)
    store = world[1]
    original = store.claim

    def claim(value):
        result = original(value)
        world[0].spaces[0]["epoch"] += 1
        return result

    monkeypatch.setattr(store, "claim", claim)
    conflict(lambda: run(world, operation=must_not_run))


def test_changed_deleted_or_unauthorized_resource_is_not_replayed(world):
    run(world)
    world[0].documents[0]["title"] = "changed"
    conflict(lambda: run(world, operation=must_not_run))
    world[0].documents.clear()
    conflict(lambda: run(world, operation=must_not_run))


def test_read_denial_on_replay_becomes_conflict(world):
    run(world)

    def denied(*args):
        raise DomainError(403, "private detail")

    world[0].list_documents = denied
    conflict(lambda: run(world, operation=must_not_run))


def test_authentication_precedes_ledger_and_operation(world):
    world[0].denied = 401
    with pytest.raises(DomainError) as caught:
        run(world, operation=must_not_run)
    assert caught.value.status_code == 401
    with sqlite3.connect(world[1].path) as connection:
        assert connection.execute("SELECT count(*) FROM research_request_ledger").fetchone()[0] == 0


def test_metadata_contains_no_body_title_or_raw_key(world):
    run(world)
    with sqlite3.connect(world[1].path) as connection:
        rows = json.dumps(connection.execute("SELECT * FROM research_request_ledger").fetchall())
    assert "PRIVATE" not in rows
    assert "unique-key" not in rows


def test_missing_post_commit_resource_id_leaves_pending(world):
    with pytest.raises(ValueError):
        run(world, operation=lambda: {"unexpected": "committed"})
    conflict(lambda: run(world, operation=must_not_run))


def test_real_service_response_shapes_and_space_creation_scope_fence(tmp_path):
    path = tmp_path / "actual.db"
    svc = build_research_service(path)
    actor = svc.bootstrap("owner@example.org", "a-unique-password-123", "Research")["user"]["id"]
    store = SQLiteResearchEngineering(path)
    space = svc.create_space(actor, "Collaboration")

    def once(route, payload, operation):
        req = request(store, path=route, key=route)
        first = execute_write(req, actor, svc, payload, operation)
        replay = execute_write(req, actor, svc, payload, must_not_run)
        assert first.body == replay.body
        assert replay.headers["Idempotency-Replayed"] == "true"
        return json.loads(first.body)

    document = once(f"/api/spaces/{space['id']}/documents", {"content": "evidence"},
                    lambda: svc.add_document(actor, space["id"], "Evidence", "evidence"))
    version = once(f"/api/documents/{document['id']}/versions", {"content": "new evidence"},
                   lambda: svc.add_version(actor, document["id"], "new evidence"))
    target = svc.create_space(actor, "Target")
    once(f"/api/versions/{version['id']}/share", {"target_space_id": target["id"]},
         lambda: svc.share_version(actor, version["id"], target["id"]))
    task = once("/api/tasks", {"title": "Task"}, lambda: svc.create_task(actor, "Task", assignee_id=actor))
    svc.update_task(actor, task["id"], status="in_progress")
    once(f"/api/tasks/{task['id']}/progress", {"content": "Working"},
         lambda: svc.add_progress(actor, task["id"], "Working"))
    once("/api/reviews", {"version_id": version["id"]}, lambda: svc.submit_review(actor, version["id"]))
    once("/api/conversations", {"space_id": space["id"]}, lambda: svc.create_conversation(actor, space["id"]))

    req = request(store, path="/api/spaces", key="create-space")
    execute_write(req, actor, svc, {"name": "New"}, lambda: svc.create_space(actor, "New"))
    conflict(lambda: execute_write(req, actor, svc, {"name": "New"}, must_not_run))


def test_real_review_decision_rebuilds_published_receipt(tmp_path):
    path = tmp_path / "review.db"
    svc = build_research_service(path)
    password = "a-unique-password-123"
    owner = svc.bootstrap("owner@example.org", password, "Research")["user"]["id"]
    invitation = svc.create_invite(owner, "member@example.org")
    member = svc.register("member@example.org", password, invitation["invite_token"])["user"]["id"]
    space = svc.create_space(member, "Work")
    svc.grant_space(member, space["id"], owner, "reader")
    document = svc.add_document(member, space["id"], "Evidence", "reviewed content")
    review = svc.submit_review(member, document["latest_version"]["id"])
    store = SQLiteResearchEngineering(path)
    req = request(store, path=f"/api/reviews/{review['id']}/decision")
    payload = {"decision": "approve", "comment": "Checked"}
    first = execute_write(req, owner, svc, payload,
                          lambda: svc.decide_review(owner, review["id"], "approve", "Checked"))
    replay = execute_write(req, owner, svc, payload, must_not_run)
    assert json.loads(first.body) == json.loads(replay.body)
    assert json.loads(replay.body)["published_version_id"]
    assert replay.headers["Idempotency-Replayed"] == "true"
