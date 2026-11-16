"""Research engineering contracts: real SQLite, concurrency and privacy fences."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
from threading import Event

import pytest

from app.application.usecases.request_idempotency import RequestIdempotency
from app.domain.engineering_ports import (
    AuthorizationScope, EngineeringConflict, LedgerClaim, OperationResult,
    RequestOperation, ResourceReference, ResponseMetadata,
)
from app.infrastructure.persistence.research_engineering import SQLiteResearchEngineering


class Clock:
    value = 1000.0

    def now(self):
        return self.value


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchEngineering(path)
    clock = Clock()
    response = {"id": "document-1", "content": "PRIVATE body never persisted"}
    authorized = []

    def authorize(actor, scope, metadata):
        authorized.append((actor, scope, metadata))
        return dict(response)

    usecase = RequestIdempotency(store, authorize=authorize, clock=clock, ttl_seconds=60, pending_ttl_seconds=10)
    return store, clock, response, authorized, usecase


OP = RequestOperation("POST", "/api/spaces/s1/documents", "document")
SCOPE = AuthorizationScope("space-s1-membership-v1", 1, 1)
META = ResponseMetadata(201, (ResourceReference("document", "document-1"),))


def execute(setup, *, actor="alice", key="key-1", payload=None, scope=SCOPE, operation=OP, fn=None):
    return setup[4].execute(actor, key, operation, payload or {"title": "hello"}, scope,
                            fn or (lambda: OperationResult(dict(setup[2]), META)))


def assert_conflict(code, fn):
    with pytest.raises(EngineeringConflict) as exc:
        fn()
    assert exc.value.status_code == 409
    assert exc.value.code == code


def test_completed_replay_same_status_response_and_live_authorization(setup):
    first = execute(setup)
    replay = execute(setup, fn=lambda: pytest.fail("must not rerun"))
    assert first.response == replay.response
    assert first.status_code == replay.status_code == 201
    assert not first.replayed and replay.replayed
    assert len(setup[3]) == 1


def test_changed_body_conflicts(setup):
    execute(setup)
    assert_conflict("body_conflict", lambda: execute(setup, payload={"title": "different"}))


def test_canonical_request_hash_handles_mapping_order(setup):
    execute(setup, payload={"a": 1, "b": 2})
    assert execute(setup, payload={"b": 2, "a": 1}).replayed


def test_users_are_isolated(setup):
    execute(setup, actor="alice")
    assert not execute(setup, actor="bob", payload={"title": "other"}).replayed
    assert execute(setup, actor="alice").replayed
    assert [item[0] for item in setup[3]] == ["alice"]


def test_method_and_path_are_isolated(setup):
    execute(setup)
    assert not execute(setup, operation=replace(OP, method="PATCH")).replayed
    assert not execute(setup, operation=replace(OP, path="/api/spaces/s2/documents")).replayed


@pytest.mark.parametrize("scope", [AuthorizationScope("space-other", 1, 1), AuthorizationScope(SCOPE.fingerprint, 2, 2)])
def test_scope_or_epoch_change_rejects_existing_key(setup, scope):
    execute(setup)
    assert_conflict("authorization_changed", lambda: execute(setup, scope=scope))


def test_stale_expected_epoch_rejected_before_operation(setup):
    assert_conflict("authorization_changed", lambda: execute(setup, scope=AuthorizationScope("s", 1, 2)))
    assert not execute(setup).replayed


def test_live_authorizer_denies_replay(setup):
    execute(setup)

    def deny(*_):
        raise PermissionError("resource access revoked")

    uc = RequestIdempotency(setup[0], authorize=deny, clock=setup[1])
    with pytest.raises(PermissionError):
        uc.execute("alice", "key-1", OP, {"title": "hello"}, SCOPE, lambda: pytest.fail("must not execute"))


def test_changed_resource_does_not_replay_different_response(setup):
    execute(setup)
    setup[2]["content"] = "new content"
    assert_conflict("response_changed", lambda: execute(setup))


def test_completed_ttl_allows_new_operation_and_body(setup):
    execute(setup)
    setup[1].value += 60
    assert not execute(setup, payload={"title": "new request"}).replayed


def test_concurrent_call_has_one_business_operation(setup):
    entered, finish = Event(), Event()
    calls = []

    def slow():
        calls.append(1)
        entered.set()
        assert finish.wait(5)
        return OperationResult(dict(setup[2]), META)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, setup, fn=slow)
        assert entered.wait(5)
        try:
            # Another adapter/connection models a second worker/process.
            other = SQLiteResearchEngineering(setup[0].path)
            uc = RequestIdempotency(other, authorize=lambda *_: setup[2], clock=setup[1])
            assert_conflict("in_progress", lambda: uc.execute("alice", "key-1", OP, {"title": "hello"}, SCOPE, slow))
        finally:
            finish.set()
        assert not first.result(timeout=5).replayed
    assert calls == [1]


def test_many_concurrent_claims_are_serialized(setup):
    claim = LedgerClaim("alice", "hashed-key", "POST", "/api/tasks", "scope", "1", "hash", 1000, 1010)

    def attempt(_):
        try:
            return setup[0].claim(claim).lease_token
        except EngineeringConflict as error:
            return error.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(16)))
    assert results.count("in_progress") == 15
    assert sum(value != "in_progress" for value in results) == 1


def test_expired_pending_is_unknown_never_automatically_retried(setup):
    entered, finish = Event(), Event()

    def slow():
        entered.set()
        assert finish.wait(5)
        return OperationResult(dict(setup[2]), META)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(execute, setup, fn=slow)
        assert entered.wait(5)
        try:
            setup[1].value += 10
            assert_conflict("unknown_outcome", lambda: execute(setup, fn=lambda: pytest.fail("duplicate effect")))
            setup[1].value += 100000
            assert_conflict("unknown_outcome", lambda: execute(setup))
        finally:
            finish.set()
        first.result(timeout=5)


def test_operation_exception_releases_claim_for_rolled_back_retry(setup):
    def fail():
        raise ValueError("business transaction rolled back")

    with pytest.raises(ValueError):
        execute(setup, fn=fail)
    assert not execute(setup).replayed


def test_invalid_result_after_effect_retains_unknown_pending(setup):
    with pytest.raises(TypeError):
        execute(setup, fn=lambda: {"committed": True})
    setup[1].value += 11
    assert_conflict("unknown_outcome", lambda: execute(setup))


def test_completion_failure_retains_pending(setup, monkeypatch):
    def fail(*_):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(setup[0], "complete", fail)
    with pytest.raises(sqlite3.OperationalError):
        execute(setup)
    setup[1].value += 11
    assert_conflict("unknown_outcome", lambda: execute(setup))


def test_no_plaintext_request_response_or_raw_key_in_database(setup):
    execute(setup, key="sensitive-raw-idempotency-key", payload={"content": "secret-request-text"})
    content = setup[0].path.read_bytes()
    assert b"PRIVATE body" not in content
    assert b"secret-request-text" not in content
    assert b"sensitive-raw-idempotency-key" not in content
    with sqlite3.connect(setup[0].path) as conn:
        row = conn.execute("SELECT response_metadata FROM research_request_ledger").fetchone()
    assert "document-1" in row[0]
    assert "content" not in row[0]


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/invites", "/api/chat", "/api/conversations/1/messages", "/api/%63hat"])
def test_sensitive_endpoints_cannot_opt_in_by_category(setup, path):
    with pytest.raises(ValueError):
        execute(setup, operation=replace(OP, path=path))


@pytest.mark.parametrize("operation", [RequestOperation("GET", "/api/tasks", "task"),
                                        RequestOperation("POST", "/api/chat", "chat"),
                                        RequestOperation("POST", "/api/tasks?secret=x", "task")])
def test_unsupported_operations_rejected(setup, operation):
    with pytest.raises(ValueError):
        execute(setup, operation=operation)


def test_owner_fencing_prevents_stale_completion_release(setup):
    claim = LedgerClaim("alice", "key", "POST", "/api/tasks", "scope", "1", "hash", 1000, 1010)
    reservation = setup[0].claim(claim)
    setup[0].release(claim, "wrong-lease")
    assert_conflict("in_progress", lambda: setup[0].claim(claim))
    assert_conflict("reservation_lost", lambda: setup[0].complete(claim, "wrong-lease", META, "digest", 2000))
    setup[0].complete(claim, reservation.lease_token, META, "digest", 2000)
    setup[0].release(claim, reservation.lease_token)
    assert setup[0].claim(claim).metadata == META


def test_authentication_required_by_usecases(setup):
    with pytest.raises(ValueError):
        execute(setup, actor="")


def test_additive_migration_repeatable_preserves_core_schema_and_version(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE core_marker (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO core_marker VALUES (1)")
        conn.execute("PRAGMA user_version=123")
    SQLiteResearchEngineering(path)
    SQLiteResearchEngineering(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 123
        assert conn.execute("SELECT id FROM core_marker").fetchone()[0] == 1


def test_completed_receipt_survives_adapter_restart(setup):
    execute(setup)
    restarted = SQLiteResearchEngineering(setup[0].path)
    uc = RequestIdempotency(restarted, authorize=lambda *_: dict(setup[2]), clock=setup[1])
    result = uc.execute("alice", "key-1", OP, {"title": "hello"}, SCOPE, lambda: pytest.fail("duplicate effect"))
    assert result.replayed and result.response == setup[2]


def test_unknown_pending_survives_adapter_restart(setup):
    with pytest.raises(TypeError):
        execute(setup, fn=lambda: None)
    setup[1].value += 10000
    restarted = SQLiteResearchEngineering(setup[0].path)
    uc = RequestIdempotency(restarted, authorize=lambda *_: setup[2], clock=setup[1])
    assert_conflict("unknown_outcome", lambda: uc.execute(
        "alice", "key-1", OP, {"title": "hello"}, SCOPE, lambda: pytest.fail("duplicate effect")))


def test_scope_change_stays_blocked_after_completed_ttl(setup):
    execute(setup)
    setup[1].value += 61
    assert_conflict("authorization_changed", lambda: execute(setup, scope=AuthorizationScope("other-scope", 1, 1)))


def test_non_json_and_nan_fail_before_claim(setup):
    with pytest.raises(ValueError):
        execute(setup, payload={"number": float("nan")})
    assert not execute(setup).replayed
