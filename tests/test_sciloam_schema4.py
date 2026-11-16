"""Schema-four append migration, immutable receipts and explicit-head read models."""
import json
import sqlite3
from contextlib import closing

import pytest

from app.infrastructure.persistence.database import Database, SCHEMA, SCHEMA_VERSION
from app.infrastructure.persistence.repositories import SQLiteResearchRepository


def _old_database(path):
    """Build real v3 using only the unchanged v1 baseline and v2/v3 DDL."""
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
        conn.execute("CREATE TABLE document_withdrawals (document_id TEXT PRIMARY KEY REFERENCES documents(id), withdrawn_by TEXT NOT NULL REFERENCES users(id), withdrawn_at REAL NOT NULL)")
        conn.execute("CREATE INDEX active_users ON users(disabled_at,role)")
        conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        conn.execute("PRAGMA user_version=3")
        conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)", [
            ("owner", "owner@example.test", "hash", "owner", 1, None),
            ("alice", "alice@example.test", "hash", "member", 2, 40),
            ("bob", "bob@example.test", "hash", "member", 3, None),
        ])
        conn.executemany("INSERT INTO spaces VALUES (?,?,?,?,?,?,?)", [
            ("private", "Original", "private", "alice", 1, None, 1),
            ("collab", "Source", "collaboration", "bob", 1, None, 1),
        ])
        conn.executemany("INSERT INTO documents VALUES (?,?,?,?,?)", [
            ("origin", "private", "Origin", "alice", 1),
            ("middle", "collab", "Middle", "bob", 2),
            ("doc", "collab", "Frozen title", "bob", 3),
        ])
        conn.executemany("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)", [
            ("o1", "origin", 1, "Origin body", "a.md", "original", "alice", 1, None),
            ("m1", "middle", 1, "Middle body", "b.md", "copied", "bob", 2, "o1"),
            ("v1", "doc", 1, "Reviewed body 1", "v1.md", "source 1", "owner", 3, "m1"),
            ("v2", "doc", 2, "Reviewed body 2", "v2.md", "source 2", "bob", 4, "m1"),
            ("v3", "doc", 3, "Reviewed body 3", "v3.md", "source 3", "bob", 5, "m1"),
        ])
        conn.executemany("INSERT INTO version_indexes VALUES (?,'ready',1)", [(v,) for v in ("o1", "m1", "v1", "v2", "v3")])
        conn.executemany("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?)", [
            ("r1", "v1", None, "bob", "pending", None, "", None, 10, None),
            ("r2", "v2", None, "bob", "approved", "owner", "approved", "o1", 11, 12),
            ("r3", "v3", None, "bob", "rejected", "owner", "needs work", None, 13, 14),
        ])
        conn.execute("INSERT INTO tasks VALUES ('task','Task','Description','bob',NULL,'in_progress','owner',1,1)")
        conn.execute("INSERT INTO progress VALUES ('progress','task','bob','Report','v1',15)")
        conn.execute("INSERT INTO conversations VALUES ('conv','collab','bob',1,16)")
        conn.executemany("INSERT INTO messages VALUES (?,'conv','assistant','Answer',?,17,'{}')", [
            ("msg-string", json.dumps(["v1"])),
            ("msg-object", json.dumps([{"version_id": "v1", "title": "Title"}])),
            ("msg-other", json.dumps(["v2"])),
        ])
        conn.execute("INSERT INTO audit_events VALUES ('event','owner','submitted','review','r1',18)")


@pytest.fixture
def old_path(tmp_path):
    path = tmp_path / "v3.sqlite3"
    _old_database(path)
    return path


@pytest.fixture
def database(old_path):
    return Database(old_path)


def test_migrate_true_v3_preserves_reviews_users_and_freezes_full_lineage(old_path):
    with sqlite3.connect(old_path) as conn:
        before_reviews = conn.execute("SELECT * FROM reviews ORDER BY id").fetchall()
        before_users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    database = Database(old_path)
    with closing(database.connect()) as conn:
        repo = SQLiteResearchRepository(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = "id,version_id,task_id,submitted_by,status,reviewed_by,comment,published_version_id,created_at,reviewed_at"
        assert [tuple(row) for row in conn.execute(f"SELECT {columns} FROM reviews ORDER BY id")] == before_reviews
        assert [tuple(row) for row in conn.execute("SELECT * FROM users ORDER BY id")] == before_users
        snapshots = repo.list("review_snapshots")
        assert len(snapshots) == 3
        assert repo.get("review_snapshots", "r1") == {
            "review_id": "r1", "version_id": "v1", "document_id": "doc", "source_space_id": "collab",
            "title": "Frozen title", "number": 1, "content": "Reviewed body 1", "filename": "v1.md",
            "source": "source 1", "source_created_by": "owner", "copied_from": "m1",
            "lineage_authors": ["alice", "bob", "owner"], "source_space_kind": "collaboration",
            "submission_mode": "snapshot", "created_at": 10,
        }
        repo.update("documents", "doc", {"title": "Renamed source"})
        repo.update("spaces", "collab", {"name": "Renamed space"})
        assert repo.get("review_snapshots", "r1")["title"] == "Frozen title"
        assert repo.get("review_snapshots", "r1")["content"] == "Reviewed body 1"
        assert repo.get("review_snapshots", "r2")["created_at"] == 11
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_original_schema_is_still_version_one_baseline(tmp_path):
    with sqlite3.connect(tmp_path / "baseline.sqlite3") as conn:
        conn.executescript(SCHEMA)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {"review_snapshots", "version_lifecycle", "document_heads", "space_deletions", "document_withdrawals"} & tables
        assert "disabled_at" not in {row[1] for row in conn.execute("PRAGMA table_info(users)")}
        assert "metadata" not in {row[1] for row in conn.execute("PRAGMA table_info(messages)")}


def test_heads_backfilled_once_and_explicit_head_beats_max_number(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        assert repo.document_active_head("doc")["id"] == "v3"
        assert repo.active_version_details("v1") is None
        repo.set_document_head("doc", "v1")
        assert repo.document_active_head("doc")["id"] == "v1"
        assert [row["version_id"] for row in repo.searchable_versions("collab") if row["document_id"] == "doc"] == ["v1"]
        assert repo.version_details("v3")["is_head"] == 0
    Database(database.path)
    with closing(database.connect()) as conn:
        repo = SQLiteResearchRepository(conn)
        assert repo.document_active_head("doc")["id"] == "v1"
        conn.execute("DELETE FROM document_heads WHERE document_id='doc'")
        assert repo.document_active_head("doc") is None
        assert repo.active_version_details("v1") is None
        assert not [row for row in repo.searchable_versions("collab") if row["document_id"] == "doc"]


@pytest.mark.parametrize("blocked", ["index_pending", "index_failed", "index_missing", "deleted", "withdrawn", "document", "space"])
def test_search_never_falls_back_when_explicit_head_is_unavailable(database, blocked):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        if blocked.startswith("index_"):
            if blocked == "index_missing":
                repo.delete_where("version_indexes", version_id="v3")
            else:
                repo.update("version_indexes", "v3", {"status": blocked.removeprefix("index_")})
        elif blocked in {"deleted", "withdrawn"}:
            repo.add("version_lifecycle", {"version_id": "v3", "status": blocked, "reason": "Receipt", "actor_id": "bob", "created_at": 20})
        elif blocked == "document":
            repo.add("document_withdrawals", {"document_id": "doc", "withdrawn_by": "bob", "withdrawn_at": 20})
        else:
            repo.add("space_deletions", {"space_id": "collab", "actor_id": "bob", "created_at": 20})
        assert not [row for row in repo.searchable_versions("collab") if row["document_id"] == "doc"]
        # Head metadata can remain available while indexing, but never after a tombstone.
        head = repo.document_active_head("doc")
        assert (head is not None) == blocked.startswith("index_")
        metadata = repo.version_metadata("doc")
        assert [row["id"] for row in metadata] == ["v1", "v2", "v3"]
        assert all("content" not in row for row in metadata)
        assert metadata[-1]["lifecycle"] == (blocked if blocked in {"deleted", "withdrawn"} else "active")


def test_document_head_must_reference_version_in_same_document(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        with pytest.raises(sqlite3.IntegrityError, match="belong"):
            repo.set_document_head("doc", "o1")
        repo.delete_where("document_heads", document_id="doc")
        with pytest.raises(sqlite3.IntegrityError, match="belong"):
            repo.add("document_heads", {"document_id": "doc", "version_id": "m1"})


@pytest.mark.parametrize("entity,key,changes", [
    ("review_snapshots", "r1", {"content": "Tamper"}),
    ("version_lifecycle", "v3", {"reason": "Tamper"}),
    ("space_deletions", "collab", {"actor_id": "owner"}),
    ("audit_events", "event", {"action": "Tamper"}),
])
def test_immutable_audit_triggers_reject_update_and_delete(database, entity, key, changes):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        repo.add("version_lifecycle", {"version_id": "v3", "status": "withdrawn", "reason": "Receipt", "actor_id": "bob", "created_at": 20})
        repo.add("space_deletions", {"space_id": "collab", "actor_id": "bob", "created_at": 20})
        before = repo.get(entity, key)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.update(entity, key, changes)
        primary_key = {"review_snapshots": "review_id", "version_lifecycle": "version_id", "space_deletions": "space_id", "audit_events": "id"}[entity]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.delete_where(entity, **{primary_key: key})
        assert repo.get(entity, key) == before
        # SQLite REPLACE implicitly deletes rows without firing DELETE triggers
        # unless recursive_triggers is on; duplicate INSERT must be guarded too.
        columns = ",".join(before)
        placeholders = ",".join("?" for _ in before)
        replacement = {**before, **changes}
        if entity == "review_snapshots":
            replacement["lineage_authors"] = json.dumps(replacement["lineage_authors"])
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"INSERT OR REPLACE INTO {entity} ({columns}) VALUES ({placeholders})", tuple(replacement.values()))
        assert repo.get(entity, key) == before


def test_new_snapshot_crud_json_round_trip_and_column_allowlist(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        repo.add("versions", {"id": "v4", "document_id": "doc", "number": 4, "content": "New body", "filename": "", "source": "", "created_by": "bob", "created_at": 21, "copied_from": None})
        repo.add("reviews", {"id": "r4", "version_id": "v4", "task_id": None, "submitted_by": "bob", "status": "pending", "created_at": 22})
        snapshot = {**repo.get("review_snapshots", "r3"), "review_id": "r4", "version_id": "v4", "lineage_authors": ["bob", "作者"]}
        repo.add("review_snapshots", snapshot)
        assert repo.get("review_snapshots", "r4") == snapshot
        assert repo.find("review_snapshots", review_id="r4")["lineage_authors"] == ["bob", "作者"]
        with pytest.raises(ValueError, match="Unknown"):
            repo.add("review_snapshots", {**snapshot, "unexpected": "invalid"})
        with pytest.raises(ValueError, match="Unknown"):
            repo.list("document_heads; DROP TABLE users")


def test_version_references_cover_copies_reviews_progress_and_json_citations(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        references = {(row["entity"], row["id"], row["field"]) for row in repo.version_references("v1")}
        assert {("reviews", "r1", "version_id"), ("review_snapshots", "r1", "version_id"),
                ("progress", "progress", "version_id"), ("messages", "msg-string", "citations"),
                ("messages", "msg-object", "citations")} <= references
        assert ("messages", "msg-other", "citations") not in references
        origin = {(row["entity"], row["id"], row["field"]) for row in repo.version_references("o1")}
        assert ("versions", "m1", "copied_from") in origin
        assert ("reviews", "r2", "published_version_id") in origin
        assert repo.version_references("not-a-version") == []
        assert repo.version_references("v1' OR 1=1 --") == []
        conn.execute("UPDATE messages SET citations='not-json' WHERE id='msg-other'")
        assert any(row.get("invalid_json") for row in repo.version_references("not-a-version"))
        conn.execute("UPDATE messages SET citations='{}' WHERE id='msg-other'")
        assert any(row.get("invalid_json") for row in repo.version_references("not-a-version"))


def test_migration_adds_reference_indexes(database):
    with closing(database.connect()) as conn:
        expected = {
            "review_snapshots": {"review_snapshots_version", "review_snapshots_document"},
            "version_lifecycle": {"version_lifecycle_status"},
            "versions": {"versions_copied_from"},
            "reviews": {"reviews_published_version"},
            "progress": {"progress_version"},
        }
        for table, names in expected.items():
            assert names <= {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
        assert any(row[2] for row in conn.execute("PRAGMA index_list(document_heads)"))


def test_repeated_migration_preserves_frozen_data_and_receipts(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        before = repo.list("review_snapshots")
        repo.update("documents", "doc", {"title": "Changed after review"})
        repo.set_document_head("doc", "v1")
        repo.add("version_lifecycle", {"version_id": "v3", "status": "withdrawn", "reason": "Receipt", "actor_id": "bob", "created_at": 20})
    for _ in range(3):
        Database(database.path)
    with closing(database.connect()) as conn:
        repo = SQLiteResearchRepository(conn)
        assert repo.list("review_snapshots") == before
        assert repo.get("document_heads", "doc")["version_id"] == "v1"
        assert repo.get("version_lifecycle", "v3")["reason"] == "Receipt"
        assert len(repo.list("users")) == 3


@pytest.mark.parametrize("corruption", ["review_version", "source_document", "source_space", "copy_ancestor", "lineage_author", "cycle"])
def test_missing_old_review_source_fails_closed_and_rolls_back(old_path, corruption):
    # Historical files can have been written with foreign-key checks disabled.
    with sqlite3.connect(old_path) as conn:
        conn.execute("DROP TRIGGER versions_immutable_update")
        if corruption == "review_version":
            conn.execute("UPDATE reviews SET version_id='missing' WHERE id='r1'")
        elif corruption == "source_document":
            conn.execute("UPDATE versions SET document_id='missing' WHERE id='v1'")
        elif corruption == "source_space":
            conn.execute("UPDATE documents SET space_id='missing' WHERE id='doc'")
        elif corruption == "copy_ancestor":
            conn.execute("UPDATE versions SET copied_from='missing' WHERE id='m1'")
        elif corruption == "lineage_author":
            conn.execute("UPDATE versions SET created_by='missing' WHERE id='o1'")
        else:
            conn.execute("UPDATE versions SET copied_from='v1' WHERE id='o1'")
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        Database(old_path)
    with sqlite3.connect(old_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='review_snapshots'").fetchall()
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
