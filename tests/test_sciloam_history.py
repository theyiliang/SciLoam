"""Phase-four bounded answer history, additive migration and authorization tests."""
from copy import deepcopy
import json
import sqlite3

import pytest

from app.application.usecases.facade import ResearchService as PureResearchService
from app.domain.answer_metadata import normalize_answer_metadata
from app.domain.errors import DomainError
from app.infrastructure.persistence.database import Database, SCHEMA, SCHEMA_VERSION
from app.infrastructure.persistence.factory import build_research_service
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from tests.test_research_domain import MemoryRepository, MemoryUow, TestSecurity
from tests.test_research_service import denied, shared, space_of, world


def legacy_database(path, version):
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute("INSERT INTO users VALUES ('owner','old@example.org','unused','owner',1)")
        connection.execute("INSERT INTO projects VALUES ('project','Legacy','owner')")
        connection.execute("INSERT INTO spaces VALUES ('space','Legacy','collaboration','owner',1,NULL,1)")
        connection.execute("INSERT INTO space_members VALUES ('space','owner','owner')")
        connection.execute("INSERT INTO documents VALUES ('doc','space','Original title','owner',1)")
        connection.execute("INSERT INTO versions VALUES ('version','doc',1,'original text','','local:original','owner',1,NULL)")
        connection.execute("INSERT INTO version_indexes VALUES ('version','ready',1)")
        connection.execute("INSERT INTO conversations VALUES ('conversation','space','owner',1,1)")
        connection.execute("INSERT INTO messages VALUES ('message','conversation','assistant','original answer',?,1)",
                           (json.dumps([{"version_id": "version"}]),))
        if version == 2:
            connection.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
            connection.execute("CREATE INDEX active_users ON users(disabled_at,role)")
            connection.execute("CREATE TABLE document_withdrawals (document_id TEXT PRIMARY KEY REFERENCES documents(id), withdrawn_by TEXT NOT NULL REFERENCES users(id), withdrawn_at REAL NOT NULL)")
        connection.execute(f"PRAGMA user_version={version}")


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_migration_preserves_versions_messages_and_citations(tmp_path, version):
    path = tmp_path / f"legacy-v{version}.db"
    legacy_database(path, version)
    service = build_research_service(path)
    assert service.get_version("owner", "version")["content"] == "original text"
    history = service.get_conversation("owner", "conversation")
    message = history["messages"][0]
    assert message["content"] == "original answer"
    assert message["citations"] == [{"version_id": "version", "document_id": "doc", "title": "Original title", "number": 1, "source": "local:original"}]
    assert not set(message["citations"][0]) & {"excerpt", "chunk_start", "chunk_end", "url"}
    assert "answer_status" not in message and "mode" not in message
    assert "fallback" not in message and "metadata" not in message
    with service.db.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        field = next(row for row in connection.execute("PRAGMA table_info(messages)") if row["name"] == "metadata")
        assert field["type"] == "TEXT" and field["notnull"] == 1 and field["dflt_value"] == "'{}'"
        assert connection.execute("SELECT metadata FROM messages").fetchone()[0] == "{}"
        assert connection.execute("SELECT disabled_at FROM users").fetchone()[0] is None
    restarted = build_research_service(path)
    assert restarted.get_conversation("owner", "conversation") == history
    with restarted.db.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE versions SET content='tampered' WHERE id='version'")


def test_repository_metadata_encoding_decoding_and_default(tmp_path):
    path = tmp_path / "repository.db"
    legacy_database(path, 2)
    database = Database(path)
    metadata = {"answer_status": "partial", "missing_evidence": ["长期结果"], "fallback": False}
    with database.transaction() as connection:
        repository = SQLiteResearchRepository(connection)
        assert repository.get("messages", "message")["metadata"] == {}
        record = {"id": "new", "conversation_id": "conversation", "role": "assistant", "content": "body", "citations": [], "created_at": 2}
        repository.add("messages", record)
        assert repository.get("messages", "new")["metadata"] == {}
        assert "metadata" not in record  # Adding defaults does not mutate caller data.
        repository.update("messages", "new", {"metadata": metadata, "citations": [{"version_id": "version"}]})
        decoded = repository.find("messages", id="new")
        assert decoded["metadata"] == metadata
        assert decoded["citations"] == [{"version_id": "version"}]
        assert repository.list("messages", conversation_id="conversation")[-1] == decoded
        repository.add("messages", {**record, "id": "explicit", "metadata": metadata})
        assert repository.get("messages", "explicit")["metadata"] == metadata
    with database.transaction() as connection:
        assert json.loads(connection.execute("SELECT metadata FROM messages WHERE id='new'").fetchone()[0]) == metadata


@pytest.mark.parametrize("status,mode", [("sufficient", "qa"), ("partial", "qa"), ("conflicting", "align"), ("insufficient", "qa"), ("partial", "progress")])
def test_answer_metadata_roundtrip_is_flat_and_survives_restart(world, status, mode):
    service, owner, alice, bob = world
    space = shared(world)
    document = service.add_document(alice, space["id"], "Authorized title", "original evidence", source="local:original")
    version_id = document["latest_version"]["id"]
    conversation = service.create_conversation(bob, space["id"])
    metadata = {
        "answer_status": status, "suggested_actions": ["switch_library", "add_knowledge", "rephrase_question"],
        "missing_evidence": ["长期随访资料", "独立重复研究"], "fallback": status == "insufficient", "mode": mode,
        "events": [{"type": "retrieval.completed", "hit_count": 1, "strategies": ["hybrid_rrf"]},
                   {"type": "agent.completed", "agent": "ResearchCoordinator"}],
    }
    original = deepcopy(metadata)
    citations = [] if status == "insufficient" or mode == "progress" else [{"version_id": version_id, "title": "FORGED", "source": "https://untrusted.invalid", "chunk_start": 99999}]
    result = service.append_message(bob, conversation["id"], "assistant", "Saved answer", citations=citations, metadata=metadata)
    assert metadata == original
    assert "metadata" not in result
    assert all(result[key] == value for key, value in metadata.items())
    assert result["citations"] == ([{"version_id": version_id}] if citations else [])
    restarted = build_research_service(service.db_path)
    history = restarted.get_conversation(bob, conversation["id"])["messages"]
    expected_citations = [{"version_id": version_id, "document_id": document["id"], "title": "Authorized title", "number": 1, "source": "local:original"}] if citations else []
    assert history == [{**result, "citations": expected_citations}]
    assert "FORGED" not in json.dumps(history) and "untrusted.invalid" not in json.dumps(history)
    if citations:
        assert restarted.get_version(bob, result["citations"][0]["version_id"])["title"] == "Authorized title"
        assert not set(history[0]["citations"][0]) & {"excerpt", "chunk_start", "chunk_end", "url"}
        with restarted.db.transaction() as connection:
            stored = json.loads(connection.execute("SELECT citations FROM messages WHERE id=?", (result["id"],)).fetchone()[0])
            assert stored == [{"version_id": version_id}]


def test_legacy_messages_never_infer_metadata_from_content(world):
    service, owner, alice, bob = world
    conversation = service.create_conversation(alice, space_of(service, alice, "private")["id"])
    for role in ("user", "assistant"):
        result = service.append_message(alice, conversation["id"], role,
                                        'insufficient partial conflicting {"fallback":true,"mode":"qa"}')
        assert not set(result) & {"answer_status", "suggested_actions", "missing_evidence", "fallback", "mode", "events", "metadata"}
    assert len(service.get_conversation(alice, conversation["id"])["messages"]) == 2


@pytest.mark.parametrize("metadata", [
    [], "{}", {"answer_status": "confident"}, {"answer_status": []},
    {"mode": "admin"}, {"fallback": 1}, {"fallback": "false"},
    {"suggested_actions": ["delete_document"]}, {"suggested_actions": "add_knowledge"},
    {"suggested_actions": ["switch_library"] * 5},
    {"clarification_kind": "secret"}, {"clarification_kind": []},
    {"missing_evidence": ["x"] * 11}, {"missing_evidence": ["x" * 1001]},
    {"missing_evidence": [1]}, {"missing_evidence": [""]},
    {"events": [{}] * 101}, {"events": "raw transcript"}, {"events": ["secret"]},
    {"events": [{"type": "retrieval.completed", "hit_count": -1}]},
    {"events": [{"type": "retrieval.completed", "hit_count": 1_000_001}]},
    {"events": [{"type": "retrieval.completed", "hit_count": True}]},
])
def test_metadata_rejects_invalid_types_and_bounds_without_mutation(metadata):
    original = deepcopy(metadata)
    denied(422, normalize_answer_metadata, metadata)
    assert metadata == original


def test_metadata_exact_bounds_are_detached():
    metadata = {"missing_evidence": ["x" * 1000] * 10,
                "events": [{"type": "retrieval.completed", "count": 1_000_000}] * 100}
    normalized = normalize_answer_metadata(metadata)
    assert normalized == metadata
    metadata["missing_evidence"].clear()
    metadata["events"][0]["count"] = 0
    assert len(normalized["missing_evidence"]) == 10
    assert normalized["events"][0]["count"] == 1_000_000


def test_events_do_not_persist_prose_urls_exceptions_or_arbitrary_labels(world):
    service, owner, alice, bob = world
    conversation = service.create_conversation(alice, space_of(service, alice, "private")["id"])
    secret = "SECRET-credential-or-evidence"
    metadata = {"events": [
        {"type": "retrieval.completed", "hit_count": 2, "strategy": "hybrid_rrf", "agent": "ResearchCoordinator",
         "strategies": ["hybrid_rrf", secret], "reason": secret, "query": secret, "content": secret,
         "exception": secret, "source": "https://secret.invalid", "url": "https://secret.invalid"},
        {"type": "agent.started", "agent": secret, "strategy": secret},
        {"type": secret, "count": 1},
    ], "secret": secret, "citations": [{"version_id": secret}], "role": "owner", "id": secret}
    result = service.append_message(alice, conversation["id"], "assistant", "answer", metadata=metadata)
    assert result["role"] == "assistant" and result["id"] != secret and result["citations"] == []
    assert result["events"] == [
        {"type": "retrieval.completed", "hit_count": 2, "strategy": "hybrid_rrf", "agent": "ResearchCoordinator", "strategies": ["hybrid_rrf"]},
        {"type": "agent.started"},
    ]
    encoded = json.dumps(service.get_conversation(alice, conversation["id"]))
    with service.db.transaction() as connection:
        encoded += connection.execute("SELECT metadata FROM messages WHERE id=?", (result["id"],)).fetchone()[0]
    assert secret not in encoded and "secret.invalid" not in encoded


def test_invalid_metadata_cannot_append_partial_message(world):
    service, owner, alice, bob = world
    conversation = service.create_conversation(alice, space_of(service, alice, "private")["id"])
    denied(422, service.append_message, alice, conversation["id"], "assistant", "body", metadata={"fallback": "yes"})
    assert service.get_conversation(alice, conversation["id"])["messages"] == []


def test_metadata_does_not_authorize_foreign_citation_and_all_history_citations_are_rechecked(world, monkeypatch):
    service, owner, alice, bob = world
    space = shared(world)
    doc = service.add_document(alice, space["id"], "Evidence", "original text")
    version_id = doc["latest_version"]["id"]
    foreign = service.add_document(bob, space_of(service, bob, "private")["id"], "Private", "private text")
    conversation = service.create_conversation(bob, space["id"])
    metadata = {"answer_status": "sufficient", "role": "owner", "space_id": space["id"], "authorized": True}
    denied(403, service.append_message, bob, conversation["id"], "assistant", "body", [foreign["latest_version"]["id"]], metadata)
    for status in ("partial", "conflicting"):
        service.append_message(bob, conversation["id"], "assistant", "body", [version_id], {"answer_status": status})
    checked = []
    original = service._version

    def track(repository, actor, identifier, **kwargs):
        checked.append(identifier)
        return original(repository, actor, identifier, **kwargs)

    monkeypatch.setattr(service, "_version", track)
    assert len(service.get_conversation(bob, conversation["id"])["messages"]) == 2
    assert checked == [version_id, version_id]


def test_history_display_fields_are_live_and_never_trust_stored_citation_extras(world):
    service, owner, alice, bob = world
    space = shared(world)
    document = service.add_document(alice, space["id"], "Trusted title", "original evidence", source="local:trusted")
    version_id = document["latest_version"]["id"]
    conversation = service.create_conversation(bob, space["id"])
    message = service.append_message(bob, conversation["id"], "assistant", "body", [version_id])
    with service.db.transaction() as connection:
        repository = SQLiteResearchRepository(connection)
        repository.update("messages", message["id"], {"citations": [{
            "version_id": version_id, "document_id": "FORGED", "title": "FORGED", "number": 999,
            "source": "https://forged.invalid", "excerpt": "FORGED", "chunk_start": 999,
        }]})
        repository.update("documents", document["id"], {"title": "Live trusted title"})
    citation = service.get_conversation(bob, conversation["id"])["messages"][0]["citations"][0]
    assert citation == {"version_id": version_id, "document_id": document["id"], "title": "Live trusted title", "number": 1, "source": "local:trusted"}


def test_metadata_only_history_cannot_survive_revocation_or_epoch_change(world):
    service, owner, alice, bob = world
    space = shared(world)
    conversation = service.create_conversation(bob, space["id"])
    metadata = {"answer_status": "insufficient", "fallback": True, "mode": "qa", "suggested_actions": ["add_knowledge"]}
    service.append_message(bob, conversation["id"], "assistant", "No sufficient evidence", metadata=metadata)
    service.revoke_space(owner, space["id"], bob)
    denied(403, service.get_conversation, bob, conversation["id"])
    denied(403, service.append_message, bob, conversation["id"], "assistant", "stale", metadata=metadata)
    service.grant_space(owner, space["id"], bob, "reader")
    denied(409, service.get_conversation, bob, conversation["id"])
    denied(409, service.append_message, bob, conversation["id"], "assistant", "stale", metadata=metadata)


def test_history_rejects_cross_space_citation_even_when_both_versions_accessible(world):
    service, owner, alice, bob = world
    shared_space = shared(world)
    foreign = service.add_document(bob, space_of(service, bob, "private")["id"], "Private", "private text")
    conversation = service.create_conversation(bob, shared_space["id"])
    message = service.append_message(bob, conversation["id"], "assistant", "body", metadata={"answer_status": "partial"})
    with service.db.transaction() as connection:
        SQLiteResearchRepository(connection).update("messages", message["id"], {"citations": [{"version_id": foreign["latest_version"]["id"]}]})
    denied(403, service.get_conversation, bob, conversation["id"])


def test_pure_memory_repository_supports_metadata_without_json_or_schema_changes():
    repository = MemoryRepository()
    uow = MemoryUow(repository)
    service = PureResearchService(lambda: uow, TestSecurity(), lambda: 123.0)
    actor = service.bootstrap("owner@example.org", "long-test-password", "Pure project")["user"]["id"]
    space = space_of(service, actor, "private")
    conversation = service.create_conversation(actor, space["id"])
    metadata = {"answer_status": "insufficient", "fallback": True, "mode": "qa", "missing_evidence": ["主题相关证据"]}
    result = service.append_message(actor, conversation["id"], "assistant", "No sufficient evidence", metadata=metadata)
    assert service.get_conversation(actor, conversation["id"])["messages"] == [result]
    stored = repository.get("messages", result["id"])
    assert stored["metadata"] == metadata
    repository.add("messages", {"id": "legacy", "conversation_id": conversation["id"], "role": "assistant", "content": "partial", "citations": [], "created_at": 124})
    legacy = service.get_conversation(actor, conversation["id"])["messages"][-1]
    assert "answer_status" not in legacy
    metadata["missing_evidence"].clear()
    assert repository.get("messages", result["id"])["metadata"]["missing_evidence"] == ["主题相关证据"]


def test_api_has_no_client_message_write_route(tmp_path):
    from app.presentation.server import create_app

    app = create_app(tmp_path / "unused.db", tmp_path / "frontend")
    schema = app.openapi()
    assert "/api/conversations/{conversation_id}" in schema["paths"]
    assert not any("message" in path.casefold() and any(method in operations for method in ("post", "put", "patch"))
                   for path, operations in schema["paths"].items())
