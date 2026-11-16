"""Phase-three space/account boundaries; local temporary data and pure ports only."""
import json

import pytest

from app.application.usecases.facade import ResearchService as PureResearchService
from app.domain import policies
from app.infrastructure.persistence.factory import build_research_service
from tests.test_research_domain import MemoryRepository, MemoryUow, TestSecurity
from tests.test_research_service import PASSWORD, denied, space_of, world


NEW_PASSWORD = "a-new-personal-password-456"


def test_project_owner_has_no_implicit_collaboration_access_or_management(world):
    service, owner, alice, bob = world
    space = service.create_space(alice, "成员协作")
    doc = service.add_document(alice, space["id"], "证据", "collaboration evidence")
    version = doc["latest_version"]["id"]
    review = service.submit_review(alice, version)
    assert space["id"] not in {s["id"] for s in service.list_spaces(owner)}
    assert [item["id"] for item in service.list_reviews(owner)] == [review["id"]]
    assert service.read_review_snapshot(owner, review["id"])["content"] == "collaboration evidence"
    for action, args in (
        (service.list_documents, (space["id"],)),
        (service.list_space_members, (space["id"],)),
        (service.search, (space["id"], "evidence")),
        (service.get_version, (version,)),
        (service.grant_space, (space["id"], owner, "reader")),
        (service.grant_space, (space["id"], bob, "editor")),
        (service.revoke_space, (space["id"], alice)),
    ):
        denied(403, action, owner, *args)
    service.grant_space(alice, space["id"], owner, "reader")
    assert service.get_version(owner, version)["content"] == "collaboration evidence"
    assert len(service.list_space_members(owner, space["id"])) == 2
    denied(403, service.grant_space, owner, space["id"], owner, "editor")
    denied(403, service.grant_space, owner, space["id"], bob, "reader")
    denied(403, service.revoke_space, owner, space["id"], alice)
    denied(403, service.add_version, owner, doc["id"], "bypass")
    assert service.decide_review(owner, review["id"], "approve")["status"] == "approved"
    service.revoke_space(alice, space["id"], owner)
    denied(403, service.get_version, owner, version)
    assert service.list_reviews(owner)[0]["id"] == review["id"]
    assert service.read_review_snapshot(owner, review["id"])["content"] == "collaboration evidence"


def test_personal_reader_editor_revocation_and_historical_access(world):
    service, owner, alice, bob = world
    private = space_of(service, alice, "private")
    doc = service.add_document(alice, private["id"], "个人笔记", "personal evidence")
    first_version = doc["latest_version"]["id"]
    for actor in (owner, bob):
        denied(403, service.get_version, actor, first_version)
        denied(403, service.grant_space, actor, private["id"], actor, "editor")
    service.grant_space(alice, private["id"], bob, "reader")
    assert service.get_version(bob, first_version)["content"] == "personal evidence"
    assert service.search(bob, private["id"], "personal")
    conversation = service.create_conversation(bob, private["id"])
    service.append_message(bob, conversation["id"], "assistant", "历史引用", [first_version])
    denied(403, service.add_document, bob, private["id"], "越权", "body")
    denied(403, service.add_version, bob, doc["id"], "越权")
    denied(403, service.grant_space, bob, private["id"], bob, "editor")
    denied(403, service.revoke_space, bob, private["id"], alice)
    denied(409, service.grant_space, alice, private["id"], alice, "reader")
    denied(403, service.revoke_space, alice, private["id"], alice)
    service.grant_space(alice, private["id"], bob, "editor")
    denied(409, service.get_conversation, bob, conversation["id"])
    second = service.add_version(bob, doc["id"], "edited personal evidence")
    service.add_document(bob, private["id"], "可写", "new personal evidence")
    submitted = service.submit_review(bob, second["id"])
    assert service.read_review_snapshot(owner, submitted["id"])["content"] == "edited personal evidence"
    denied(403, service.grant_space, bob, private["id"], owner, "reader")
    fresh = service.create_conversation(bob, private["id"])
    service.append_message(bob, fresh["id"], "assistant", "引用", [second["id"]])
    service.revoke_space(alice, private["id"], bob)
    for version_id in (first_version, second["id"]):
        denied(403, service.get_version, bob, version_id)
    denied(403, service.get_conversation, bob, fresh["id"])
    denied(403, service.list_conversations, bob, private["id"])
    denied(403, service.search, bob, private["id"], "personal")
    assert private["id"] not in {s["id"] for s in service.list_spaces(bob)}
    service.grant_space(alice, private["id"], bob, "reader")
    denied(409, service.get_conversation, bob, fresh["id"])
    assert service.list_conversations(bob, private["id"]) == []
    assert service.get_version(alice, first_version)["content"] == "personal evidence"


def test_system_space_members_read_only_and_system_owner_protected(world):
    service, owner, alice, bob = world
    for kind in ("basis", "results"):
        space = space_of(service, alice, kind)
        assert space["role"] == "reader"
        denied(403, service.add_document, alice, space["id"], "越权", "body")
        denied(403, service.grant_space, owner, space["id"], alice, "editor")
        denied(403, service.grant_space, alice, space["id"], bob, "reader")
        denied(409, service.grant_space, owner, space["id"], owner, "reader")
        denied(403, service.revoke_space, owner, space["id"], owner)
        service.revoke_space(owner, space["id"], alice)
        denied(403, service.list_documents, alice, space["id"])
        service.grant_space(owner, space["id"], alice, "reader")
        assert service.list_space_members(alice, space["id"])
    basis = space_of(service, owner, "basis")
    assert service.add_document(owner, basis["id"], "基础", "basis evidence")


def test_default_space_names_are_chinese_without_renaming_existing_data(world):
    service, owner, alice, bob = world
    assert {s["name"] for s in service.list_spaces(owner)} == {"课题知识库", "成果知识库", "个人知识库"}
    assert space_of(service, alice, "private")["name"] == "个人知识库"
    private = space_of(service, alice, "private")
    with service.db.transaction() as connection:
        connection.execute("UPDATE spaces SET name='Legacy private' WHERE id=?", (private["id"],))
    restarted = build_research_service(service.db_path)
    assert space_of(restarted, alice, "private")["name"] == "Legacy private"


def test_change_password_wrong_current_preserves_sessions_and_persists_failure(world):
    service, owner, alice, bob = world
    session = service.login("alice@example.org", PASSWORD)
    denied(401, service.change_password, alice, "wrong-current", NEW_PASSWORD)
    assert service.authenticate(session["token"])["id"] == alice
    restarted = build_research_service(service.db_path)
    with restarted.db.transaction() as connection:
        failure = connection.execute("SELECT failures FROM login_failures WHERE email='alice@example.org'").fetchone()
        assert failure[0] == 1
    assert restarted.login("alice@example.org", PASSWORD)["user"]["id"] == alice
    assert not any(event["action"] == "password_changed" for event in service.audit(owner))


def test_change_password_success_revokes_all_sessions_clears_failures_and_audits(world):
    service, owner, alice, bob = world
    sessions = [service.login("alice@example.org", PASSWORD)["token"] for _ in range(3)]
    other_session = service.login("bob@example.org", PASSWORD)["token"]
    denied(401, service.change_password, alice, "wrong-current", NEW_PASSWORD)
    assert service.change_password(alice, PASSWORD, NEW_PASSWORD) == {"password_changed": True, "reauthenticate": True}
    restarted = build_research_service(service.db_path)
    for token in sessions:
        denied(401, restarted.authenticate, token)
    assert restarted.authenticate(other_session)["id"] == bob
    with restarted.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM login_failures WHERE email='alice@example.org'").fetchone()[0] == 0
        encoded = connection.execute("SELECT password_hash FROM users WHERE id=?", (alice,)).fetchone()[0]
        assert encoded.startswith("pbkdf2_sha256$") and NEW_PASSWORD not in encoded
    assert restarted.login("alice@example.org", NEW_PASSWORD)["user"]["id"] == alice
    denied(401, restarted.login, "alice@example.org", PASSWORD)
    events = [event for event in service.audit(owner) if event["action"] == "password_changed"]
    assert len(events) == 1
    assert events[0]["actor_id"] == events[0]["resource_id"] == alice
    audit = json.dumps(service.audit(owner))
    for secret in (PASSWORD, NEW_PASSWORD, "wrong-current", "password_hash", *sessions):
        assert secret not in audit


def test_change_password_rejects_weak_reused_and_unauthenticated_requests(world):
    service, owner, alice, bob = world
    session = service.login("alice@example.org", PASSWORD)["token"]
    for password in ("short", "", "x" * 1025, PASSWORD):
        denied(422, service.change_password, alice, PASSWORD, password)
        assert service.authenticate(session)["id"] == alice
    denied(401, service.change_password, "not-a-user", PASSWORD, NEW_PASSWORD)
    denied(401, service.change_password, {"id": owner, "role": "owner"}, PASSWORD, NEW_PASSWORD)
    assert service.login("alice@example.org", PASSWORD)["user"]["id"] == alice
    service.remove_member(owner, bob)
    denied(401, service.change_password, bob, PASSWORD, NEW_PASSWORD)


def test_change_password_and_login_share_persistent_email_throttle(world):
    service, owner, alice, bob = world
    session = service.login("alice@example.org", PASSWORD)["token"]
    for _ in range(2):
        denied(401, service.login, "ALICE@EXAMPLE.ORG", "wrong")
    for _ in range(policies.LOGIN_FAILURE_LIMIT - 2):
        denied(401, service.change_password, alice, "wrong", NEW_PASSWORD)
    restarted = build_research_service(service.db_path)
    denied(429, restarted.change_password, alice, PASSWORD, NEW_PASSWORD)
    denied(429, restarted.change_password, alice, "wrong", NEW_PASSWORD)
    denied(429, restarted.login, "Alice@example.org", PASSWORD)
    assert restarted.authenticate(session)["id"] == alice
    with restarted.db.transaction() as connection:
        assert connection.execute("SELECT failures FROM login_failures WHERE email='alice@example.org'").fetchone()[0] == 5
        connection.execute("UPDATE login_failures SET locked_until=0,updated_at=0 WHERE email='alice@example.org'")
    assert restarted.change_password(alice, PASSWORD, NEW_PASSWORD)["reauthenticate"] is True
    denied(401, restarted.authenticate, session)
    assert restarted.login("alice@example.org", NEW_PASSWORD)["user"]["id"] == alice


class TrackingUow(MemoryUow):
    def __init__(self, repository):
        super().__init__(repository)
        self.active = False

    def __enter__(self):
        result = super().__enter__()
        self.active = True
        return result

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.active = False


@pytest.mark.parametrize("race", ["none", "credentials", "disabled", "throttle"])
def test_change_password_slow_work_outside_transaction_and_race_recheck(race):
    repository = MemoryRepository()
    uow = TrackingUow(repository)
    security = TestSecurity()
    service = PureResearchService(lambda: uow, security, lambda: 100.0)
    actor = service.bootstrap("owner@example.org", PASSWORD, "并发测试")["user"]["id"]
    token = service.login("owner@example.org", PASSWORD)["token"]
    original_hash = security.hash_password
    original_verify = security.verify_password

    def hash_outside_lock(password):
        assert not uow.active, "Password hashing must not hold the transaction"
        if password == NEW_PASSWORD:
            if race == "credentials":
                repository.update("users", actor, {"password_hash": original_hash("concurrent-reset-password")})
            elif race == "disabled":
                repository.update("users", actor, {"disabled_at": 100.0})
            elif race == "throttle":
                repository.save_login_failure("owner@example.org", {"failures": 5, "locked_until": 160.0, "updated_at": 100.0})
        return original_hash(password)

    def verify_outside_lock(password, encoded):
        assert not uow.active, "Password verification must not hold the transaction"
        return original_verify(password, encoded)

    security.hash_password = hash_outside_lock
    security.verify_password = verify_outside_lock
    if race == "none":
        assert service.change_password(actor, PASSWORD, NEW_PASSWORD)["password_changed"] is True
        denied(401, service.authenticate, token)
    else:
        denied({"credentials": 409, "disabled": 401, "throttle": 429}[race], service.change_password, actor, PASSWORD, NEW_PASSWORD)
        assert repository.get("sessions", security.hash_token(token)) is not None
        assert not repository.find("audit_events", action="password_changed")
        expected = original_hash("concurrent-reset-password" if race == "credentials" else PASSWORD)
        assert repository.get("users", actor)["password_hash"] == expected
