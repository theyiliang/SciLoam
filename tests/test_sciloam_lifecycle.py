"""Phase-five soft lifecycle boundaries; immutable audit survives every deletion."""
import sqlite3

import pytest

from app.infrastructure.persistence.factory import build_research_service
from tests.test_research_service import denied, shared, space_of, world


def revisions(world):
    service, owner, alice, bob = world
    space = shared(world)
    document = service.add_document(alice, space["id"], "Evidence", "first evidence")
    first = document["latest_version"]["id"]
    second = service.add_version(alice, document["id"], "second evidence")["id"]
    return space, document, first, second


def test_active_version_requires_explicit_replacement_and_never_rolls_back(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    denied(409, service.delete_version, alice, second)
    denied(409, service.withdraw_version, alice, second, "withdraw current")
    assert service.list_documents(alice, space["id"])[0]["latest_version"]["id"] == second
    receipt = service.delete_version(alice, second, first, "duplicate")
    assert receipt["version_id"] == second and receipt["status"] == "deleted"
    assert receipt["actor_id"] == alice and receipt["reason"] == "duplicate"
    denied(410, service.get_version, alice, second)
    current = service.list_documents(alice, space["id"])[0]
    assert current["latest_version"]["id"] == first
    history = {v["id"]: v for v in current["versions"]}
    assert history[second]["lifecycle_status"] == "deleted"
    assert history[first]["is_current"] is True and history[second]["is_current"] is False
    assert all("content" not in row for row in history.values())
    assert service.search(alice, space["id"], "first")[0]["version_id"] == first
    assert service.search(alice, space["id"], "second") == []
    third = service.add_version(alice, document["id"], "third evidence")
    assert third["number"] == 3
    restarted = build_research_service(service.db_path)
    assert restarted.list_documents(alice, space["id"])[0]["latest_version"]["id"] == third["id"]


def test_replacement_must_be_readable_ready_different_and_same_document(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    foreign = service.add_document(alice, space["id"], "Other", "other evidence")["latest_version"]["id"]
    denied(409, service.delete_version, alice, second, second)
    denied(409, service.delete_version, alice, second, foreign)
    denied(404, service.delete_version, alice, second, "missing")
    with service.db.transaction() as connection:
        connection.execute("UPDATE version_indexes SET status='pending' WHERE version_id=?", (first,))
    denied(409, service.delete_version, alice, second, first)
    with service.db.transaction() as connection:
        connection.execute("UPDATE version_indexes SET status='ready' WHERE version_id=?", (first,))
    service.delete_version(alice, first)
    denied(410, service.delete_version, alice, second, first)
    assert service.get_version(alice, second)["content"] == "second evidence"


@pytest.mark.parametrize("protection", ["citation", "review", "copy", "progress"])
def test_protected_historical_versions_only_withdraw_and_keep_original_audit(world, protection):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    if protection == "citation":
        conversation = service.create_conversation(bob, space["id"])
        service.append_message(bob, conversation["id"], "assistant", "cited", [first])
    elif protection == "review":
        service.submit_review(alice, first)
    elif protection == "copy":
        target = service.create_space(alice, "Copy destination")
        copied = service.share_version(alice, first, target["id"])["latest_version"]["id"]
    else:
        task = service.create_task(owner, "Research task", assignee_id=alice)
        service.update_task(alice, task["id"], status="in_progress")
        service.add_progress(alice, task["id"], "Progress", first)
    metadata = service.list_documents(alice, space["id"])[0]["versions"][0]
    assert metadata["protected"] is True
    denied(409, service.delete_version, alice, first)
    receipt = service.withdraw_version(alice, first, "unreliable")
    assert receipt["status"] == "withdrawn"
    denied(410, service.get_version, bob, first)
    denied(409, service.delete_version, alice, first)
    assert service.get_version(alice, second)["content"] == "second evidence"
    with service.db.transaction() as connection:
        original = connection.execute("SELECT content FROM versions WHERE id=?", (first,)).fetchone()
        assert original[0] == "first evidence"
        assert connection.execute("SELECT COUNT(*) FROM version_indexes WHERE version_id=?", (first,)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM versions WHERE id=?", (first,))
    if protection == "citation":
        denied(409, service.get_conversation, bob, conversation["id"])
        fresh = service.create_conversation(bob, space["id"])
        denied(410, service.append_message, bob, fresh["id"], "assistant", "stale citation", [first])
    elif protection == "copy":
        assert service.get_version(alice, copied)["content"] == "first evidence"


def test_version_author_not_document_author_or_editor_controls_deletion(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    service.grant_space(owner, space["id"], bob, "editor")
    bobs_version = service.add_version(bob, document["id"], "bob's evidence")["id"]
    service.add_version(alice, document["id"], "new current evidence")
    for action, extra in ((service.delete_version, ()), (service.withdraw_version, ("reason",))):
        denied(403, action, bob, first, *extra)
        denied(403, action, alice, bobs_version, *extra)
    assert service.delete_version(bob, bobs_version)["actor_id"] == bob
    assert service.withdraw_version(owner, first, "space owner")["actor_id"] == owner
    service.grant_space(owner, space["id"], bob, "reader")
    # Authorship retains the old withdrawal rule even after write access is lowered.
    assert service.delete_version(alice, second)["status"] == "deleted"


def test_project_owner_has_no_implicit_version_or_space_deletion_authority(world):
    service, owner, alice, bob = world
    space = service.create_space(alice, "Alice owned")
    document = service.add_document(alice, space["id"], "Evidence", "old evidence")
    first = document["latest_version"]["id"]
    service.add_version(alice, document["id"], "new evidence")
    denied(403, service.delete_version, owner, first)
    denied(403, service.withdraw_version, owner, first, "owner override")
    denied(403, service.delete_space, owner, space["id"])
    service.grant_space(alice, space["id"], owner, "editor")
    denied(403, service.delete_version, owner, first)
    denied(403, service.withdraw_version, owner, first, "owner override")
    denied(403, service.delete_space, owner, space["id"])


@pytest.mark.parametrize("kind", ["basis", "results", "private"])
def test_system_and_default_personal_roots_cannot_be_deleted(world, kind):
    service, owner, alice, bob = world
    actor = alice if kind == "private" else owner
    root = space_of(service, actor, kind)
    denied(403, service.delete_space, actor, root["id"])
    assert root["id"] in {s["id"] for s in service.list_spaces(actor)}


def test_space_soft_delete_owner_only_gates_all_reads_preserves_grants_and_copies(world):
    service, owner, alice, bob = world
    source = service.create_space(alice, "Source collaboration")
    service.grant_space(alice, source["id"], bob, "editor")
    document = service.add_document(alice, source["id"], "Evidence", "independent evidence")
    version = document["latest_version"]["id"]
    conversation = service.create_conversation(bob, source["id"])
    service.append_message(bob, conversation["id"], "assistant", "source citation", [version])
    target = service.create_space(alice, "Destination collaboration")
    copied = service.share_version(alice, version, target["id"])["latest_version"]["id"]
    denied(403, service.delete_space, bob, source["id"])
    with service.db.transaction() as connection:
        epoch = connection.execute("SELECT epoch FROM spaces WHERE id=?", (source["id"],)).fetchone()[0]
        grants = [tuple(row) for row in connection.execute("SELECT * FROM space_members WHERE space_id=? ORDER BY user_id", (source["id"],))]
    receipt = service.delete_space(alice, source["id"])
    assert receipt["space_id"] == source["id"] and receipt["actor_id"] == alice
    assert service.delete_space(alice, source["id"]) == receipt
    for actor in (owner, alice, bob):
        assert source["id"] not in {s["id"] for s in service.list_spaces(actor)}
        for action, args in (
            (service.list_documents, (source["id"],)),
            (service.search, (source["id"], "evidence")),
            (service.get_version, (version,)),
            (service.list_space_members, (source["id"],)),
            (service.list_conversations, (source["id"],)),
            (service.add_document, (source["id"], "New", "body")),
        ):
            denied(410, action, actor, *args)
    denied(410, service.get_conversation, bob, conversation["id"])
    denied(410, service.grant_space, alice, source["id"], bob, "reader")
    denied(410, service.share_version, alice, version, target["id"])
    assert service.get_version(alice, copied)["content"] == "independent evidence"
    assert service.search(alice, target["id"], "independent")
    restarted = build_research_service(service.db_path)
    denied(410, restarted.get_version, alice, version)
    with service.db.transaction() as connection:
        assert connection.execute("SELECT epoch FROM spaces WHERE id=?", (source["id"],)).fetchone()[0] == epoch + 1
        assert [tuple(row) for row in connection.execute("SELECT * FROM space_members WHERE space_id=? ORDER BY user_id", (source["id"],))] == grants
        assert connection.execute("SELECT content FROM versions WHERE id=?", (version,)).fetchone()[0] == "independent evidence"


@pytest.mark.parametrize("mutation", ["delete_version", "replace_head", "withdraw_version", "withdraw_document"])
def test_every_knowledge_lifecycle_mutation_invalidates_old_conversation_epoch(world, mutation):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    conversation = service.create_conversation(bob, space["id"])
    service.append_message(bob, conversation["id"], "assistant", "metadata-only answer", metadata={"answer_status": "insufficient"})
    if mutation == "delete_version":
        service.delete_version(alice, first)
    elif mutation == "replace_head":
        service.delete_version(alice, second, first)
    elif mutation == "withdraw_version":
        service.withdraw_version(alice, first, "reason")
    else:
        service.withdraw_document(alice, document["id"])
    denied(409, service.get_conversation, bob, conversation["id"])
    denied(409, service.append_message, bob, conversation["id"], "user", "stale")
    assert service.list_conversations(bob, space["id"]) == []


def test_ordinary_additions_preserve_historical_citations_and_permission_epoch(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    conversation = service.create_conversation(bob, space["id"])
    service.append_message(bob, conversation["id"], "assistant", "historical evidence", [first])
    with service.db.transaction() as connection:
        epoch = connection.execute("SELECT epoch FROM spaces WHERE id=?", (space["id"],)).fetchone()[0]
    service.add_version(alice, document["id"], "third evidence")
    service.add_document(alice, space["id"], "New knowledge", "new evidence")
    history = service.get_conversation(bob, conversation["id"])
    assert history["messages"][0]["citations"][0]["version_id"] == first
    assert service.get_version(bob, first)["content"] == "first evidence"
    with service.db.transaction() as connection:
        assert connection.execute("SELECT epoch FROM spaces WHERE id=?", (space["id"],)).fetchone()[0] == epoch


def test_lifecycle_receipts_are_idempotent_audit_is_immutable_and_requires_auth(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    for actor in (None, {"id": alice, "role": "owner"}, "missing"):
        denied(401, service.delete_version, actor, first)
        denied(401, service.withdraw_version, actor, first, "reason")
        denied(401, service.delete_space, actor, space["id"])
    receipt = service.delete_version(alice, first, reason="original reason")
    assert service.delete_version(owner, first, reason="cannot rewrite") == receipt
    with service.db.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM version_lifecycle WHERE version_id=?", (first,)).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audit_events WHERE action='version_deleted' AND resource_id=?", (first,)).fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE version_lifecycle SET reason='tampered' WHERE version_id=?", (first,))
    denied(410, service.get_version, alice, first)
    denied(409, service.withdraw_version, alice, first, "convert")


def test_results_only_project_owner_can_withdraw_and_published_copy_is_independent(world):
    service, owner, alice, bob = world
    space, document, first, second = revisions(world)
    review = service.submit_review(alice, first)
    published = service.decide_review(owner, review["id"], "approve")["published_version_id"]
    service.withdraw_version(alice, first, "source withdrawn")
    service.delete_space(owner, space["id"])
    assert service.get_version(bob, published)["content"] == "first evidence"
    for actor in (alice, bob):
        denied(403, service.withdraw_version, actor, published, "not owner")
        denied(403, service.delete_version, actor, published)
    denied(409, service.delete_version, owner, published)
    denied(409, service.withdraw_version, owner, published, "active published version")
    published_document = service.get_version(owner, published)["document_id"]
    denied(403, service.withdraw_document, alice, published_document)
    service.withdraw_document(owner, published_document)
    denied(410, service.get_version, bob, published)
