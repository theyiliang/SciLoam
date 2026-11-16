"""Append-only schema-six drafts and immutable publication receipts."""
from contextlib import closing
import json
import sqlite3

import pytest

from app.infrastructure.persistence.database import Database, SCHEMA, SCHEMA_VERSION, _migrate_schema_5
from app.infrastructure.persistence.repositories import SQLiteResearchRepository
from tests.test_sciloam_schema5 import _schema_four, _snapshot


def _draft(draft_id="draft", **changes):
    return {"id": draft_id, "created_by": "owner", "status": "draft", "revision": 1,
            "sources": [{"version_id": "unlinked-version", "document_id": "other-doc", "space_id": "collab",
                         "number": 1, "content_hash": "sha256", "version_hash": "sha256"}],
            "sources_hash": "sha256", "base_tree_hash": "sha256", "nodes": [], "method": "extractive",
            "limitations": ["Local extraction only"], "created_at": 50, "updated_at": 50,
            "published_at": None, 'generation': {}, 'suggestions': [], 'regenerates_id': None, 'abandoned_at': None, **changes}


@pytest.fixture
def v5_path(tmp_path):
    path = tmp_path / "v5.sqlite3"
    _schema_four(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        _migrate_schema_5(connection)
        connection.execute("PRAGMA user_version=5")
    return path


@pytest.fixture
def db(v5_path):
    return Database(v5_path)


def test_v5_to_v6_only_appends_new_tables_and_preserves_all_old_records_and_schema(v5_path):
    with sqlite3.connect(v5_path) as connection:
        before = _snapshot(connection)
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    database = Database(v5_path)
    with closing(database.connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 7
        after = _snapshot(connection)
        assert set(after) - set(before) == {'goal_drafts', 'goal_draft_publications', 'knowledge_imports', 'goal_generations', 'goal_generation_sources', 'user_action_events', 'sqlite_sequence'}
        for table, (columns, rows) in before.items():
            assert [tuple(row) for row in connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1")] == rows
        all_schema = {tuple(row) for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master")}
        assert {row for row in schema if row[1] != 'goal_nodes'} <= all_schema
        assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        initial = _snapshot(connection)
    Database(v5_path)
    with closing(database.connect()) as connection:
        assert _snapshot(connection) == initial


def test_v1_baseline_still_does_not_contain_drafts_and_fresh_database_migrates(tmp_path):
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(SCHEMA)
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'goal_draft%'").fetchall()
    database = Database(tmp_path / "fresh.sqlite3")
    with closing(database.connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        repo = SQLiteResearchRepository(connection)
        assert repo.list("goal_drafts") == repo.list("goal_draft_publications") == []


def test_draft_json_roundtrip_mutable_then_published_immutable_even_replace(db):
    with db.transaction() as connection:
        repo = SQLiteResearchRepository(connection)
        draft = _draft()
        repo.add("goal_drafts", draft)
        assert repo.get("goal_drafts", "draft") == draft
        repo.update("goal_drafts", "draft", {"revision": 2, "nodes": [{"title": "Goal"}], "limitations": []})
        assert repo.find("goal_drafts", created_by="owner")["nodes"] == [{"title": "Goal"}]
        receipt = {"draft_id": "draft", "result": {"draft_id": "draft", "goal_ids": [], "node_id_mapping": {}, "published_at": 60}, "created_at": 60}
        repo.add("goal_draft_publications", receipt)
        repo.update("goal_drafts", "draft", {"status": "published", "published_at": 60})
        assert repo.get("goal_draft_publications", "draft") == receipt
        before = repo.get("goal_drafts", "draft")
        repo.add("goal_drafts", _draft("other"))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE OR REPLACE goal_drafts SET id='draft' WHERE id='other'")
        for changes in ({"nodes": []}, {"status": "draft"}, {"id": "other"}, {"published_at": 61}):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                repo.update("goal_drafts", "draft", changes)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.delete_where("goal_drafts", id="draft")
        raw = {**before, "status": "draft"}
        for field in ("sources", "nodes", "limitations", 'generation', 'suggestions'):
            raw[field] = json.dumps(raw[field])
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(f"INSERT OR REPLACE INTO goal_drafts ({','.join(raw)}) VALUES ({','.join('?' for _ in raw)})", tuple(raw.values()))
        for sql in ("UPDATE goal_draft_publications SET result='{}' WHERE draft_id='draft'",
                    "UPDATE goal_draft_publications SET draft_id='other' WHERE draft_id='draft'",
                    "DELETE FROM goal_draft_publications WHERE draft_id='draft'",
                    "INSERT OR REPLACE INTO goal_draft_publications VALUES ('draft','{}',61)"):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                connection.execute(sql)
        assert repo.get("goal_drafts", "draft") == before
        assert repo.get("goal_draft_publications", "draft") == receipt


def test_draft_delete_and_json_updates_before_publication(db):
    with db.transaction() as connection:
        repo = SQLiteResearchRepository(connection)
        repo.add("goal_drafts", _draft())
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            repo.update('goal_drafts', 'draft', {'sources': []})
        repo.update("goal_drafts", "draft", {"limitations": ["未确认"]})
        assert repo.get("goal_drafts", "draft")["limitations"] == ["未确认"]
        assert repo.delete_where("goal_drafts", id="draft") == 1


@pytest.mark.parametrize("changes", [{"status": "approved"}, {"revision": 0}, {"created_by": "missing"}])
def test_draft_checks_foreign_keys_and_allowlist(db, changes):
    with db.transaction() as connection:
        repo = SQLiteResearchRepository(connection)
        with pytest.raises(sqlite3.IntegrityError):
            repo.add("goal_drafts", _draft(**changes))
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            repo.add("goal_draft_publications", {"draft_id": "missing", "result": {}, "created_at": 60})
        with pytest.raises(ValueError, match="Unknown"):
            repo.add("goal_drafts", {**_draft(), "sql": "not a column"})


def test_sources_protect_versions_including_unpublished_drafts_and_malformed_json(db):
    with db.transaction() as connection:
        repo = SQLiteResearchRepository(connection)
        assert repo.version_references("unlinked-version") == []
        repo.add("goal_drafts", _draft())
        expected = {"entity": "goal_drafts", "id": "draft", "field": "sources"}
        assert repo.version_references("unlinked-version") == [expected]
        assert repo.version_references("unlinked-version' OR 1=1 --") == []
        repo.update("goal_drafts", "draft", {"status": "published", "published_at": 60})
        assert repo.version_references("unlinked-version") == [expected]
        # Simulate malformed pre-migration historical rows at insertion time;
        # schema seven prevents subsequent provenance mutation.
        for index, invalid in enumerate(('not JSON', '{}', '[{"version_id":null}]', '["unlinked-version"]')):
            raw = _draft('bad-' + str(index))
            for field in ('nodes', 'limitations', 'generation', 'suggestions'):
                raw[field] = json.dumps(raw[field])
            raw['sources'] = invalid
            connection.execute(f"INSERT INTO goal_drafts ({','.join(raw)}) VALUES ({','.join('?' for _ in raw)})", tuple(raw.values()))
            assert {'entity': 'goal_drafts', 'id': raw['id'], 'field': 'sources', 'invalid_json': True} in repo.version_references('anything')
