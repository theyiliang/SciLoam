"""Schema-five goal import preserves legacy records without inventing acceptance."""
from contextlib import closing
import sqlite3

import pytest

from app.infrastructure.persistence.database import Database, SCHEMA, SCHEMA_4, SCHEMA_VERSION
from app.infrastructure.persistence.repositories import SQLiteResearchRepository


STATUSES = ("not_started", "in_progress", "pending_acceptance", "completed", "paused")
GOAL_TABLES = {"goal_nodes", "goal_updates", "goal_knowledge", "goal_status_history"}


def _schema_four(path):
    """Construct genuine v4 without invoking the current-version initializer."""
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
        conn.execute("CREATE TABLE document_withdrawals (document_id TEXT PRIMARY KEY REFERENCES documents(id), withdrawn_by TEXT NOT NULL REFERENCES users(id), withdrawn_at REAL NOT NULL)")
        conn.execute("CREATE INDEX active_users ON users(disabled_at,role)")
        conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
        conn.executescript(SCHEMA_4)
        conn.execute("PRAGMA user_version=4")
        conn.executemany("INSERT INTO users VALUES (?,?,?,?,?,?)", [
            ("owner", "owner@example.test", "hash", "owner", 1, None),
            ("author", "author@example.test", "hash", "member", 2, 100),
            ("assignee", "assignee@example.test", "hash", "member", 3, None),
        ])
        conn.execute("INSERT INTO projects VALUES ('project','Research','owner')")
        conn.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?)", [
            (f"task-{status}", f"Title {status}", f"Description {status}", "assignee",
             None if i == 0 else "2026-11-30", status, "author", 10 + i, 20 + i)
            for i, status in enumerate(STATUSES)
        ])
        conn.executemany("INSERT INTO spaces VALUES (?,?,?,?,?,?,?)", [
            ("private", "Private evidence", "private", "author", 2, None, 4),
            ("collab", "Legacy task space", "collaboration", "assignee", 3, "task-completed", 5),
        ])
        conn.executemany("INSERT INTO documents VALUES (?,?,?,?,?)", [
            ("private-doc", "private", "Private source", "author", 6),
            ("other-doc", "collab", "Unlinked source", "assignee", 7),
        ])
        conn.executemany("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?)", [
            ("private-version", "private-doc", 1, "Private content", "private.md", "upload", "author", 8, None),
            ("unlinked-version", "other-doc", 1, "Other content", "other.md", "upload", "assignee", 9, None),
        ])
        conn.executemany("INSERT INTO version_indexes VALUES (?,'ready',10)", [("private-version",), ("unlinked-version",)])
        conn.executemany("INSERT INTO document_heads VALUES (?,?)", [("private-doc", "private-version"), ("other-doc", "unlinked-version")])
        conn.execute("INSERT INTO reviews VALUES ('review','private-version','task-completed','author','approved','owner','Legacy accepted',NULL,30,31)")
        conn.execute(
            "INSERT INTO review_snapshots VALUES ('review','private-version','private-doc','private',"
            "'Frozen private title',1,'Private content','private.md','upload','author',NULL,"
            "'[\"author\"]','private','snapshot',30)"
        )
        conn.executemany("INSERT INTO progress VALUES (?,?,?,?,?,?)", [
            ("private-progress", "task-in_progress", "author", "Private progress", "private-version", 32),
            ("text-progress", "task-completed", "assignee", "Plain progress", None, 33),
        ])
        conn.execute("INSERT INTO audit_events VALUES ('legacy-audit','owner','approved','review','review',31)")
        conn.execute("PRAGMA foreign_keys=ON")
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


@pytest.fixture
def old_path(tmp_path):
    path = tmp_path / "real-v4.sqlite3"
    _schema_four(path)
    return path


@pytest.fixture
def database(old_path):
    return Database(old_path)


def _snapshot(conn):
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {
        table: ([row[1] for row in conn.execute(f"PRAGMA table_info({table})")],
                [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")])
        for table in tables
    }


def _goal(goal_id="new-goal", **changes):
    return {"id": goal_id, "title": "Goal", "created_by": "owner", "created_at": 40, "updated_at": 40, **changes}


def test_real_v4_backfill_is_lossless_and_preserves_old_state_not_new_acceptance(old_path):
    with sqlite3.connect(old_path) as conn:
        before = _snapshot(conn)
    database = Database(old_path)
    with closing(database.connect()) as conn:
        repo = SQLiteResearchRepository(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 7
        for table, (columns, rows) in before.items():
            assert [tuple(row) for row in conn.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY 1")] == rows
        assert len(repo.list("goal_nodes")) == len(STATUSES)
        for task in repo.list("tasks"):
            goal = repo.get("goal_nodes", task["id"])
            assert goal == {
                "id": task["id"], "parent_id": None, "title": task["title"],
                "description": task["description"], "acceptance_criteria": "", "due_date": task["due_date"],
                "status": task["status"], "position": 0, "revision": 1, "created_by": task["created_by"],
                "created_at": task["created_at"], "updated_at": task["updated_at"], "archived_at": None,
                "archived_reason": "", "legacy_task_id": task["id"],
                'deliverables': '', 'constraints': '', 'time_requirement': '',
            }
            assert "assignee_id" not in goal
            history = repo.list("goal_status_history", goal_id=goal["id"])
            assert len(history) == 1
            assert history[0]["from_status"] == history[0]["to_status"] == task["status"]
            assert history[0]["actor_id"] == task["created_by"]
            assert history[0]["created_at"] == task["updated_at"]
            assert "legacy_import" in history[0]["reason"]
            assert "not a new acceptance" in history[0]["reason"]
        for progress in repo.list("progress"):
            assert repo.get("goal_updates", progress["id"]) == {
                "id": progress["id"], "goal_id": progress["task_id"], "author_id": progress["author_id"],
                "content": progress["content"], "version_id": progress["version_id"], "created_at": progress["created_at"],
            }
        assert repo.get("reviews", "review")["goal_id"] == "task-completed"
        assert repo.get("reviews", "review")["status"] == "approved"
        assert repo.get("spaces", "collab")["goal_id"] == "task-completed"
        assert repo.get("spaces", "private")["goal_id"] is None
        assert repo.list("goal_knowledge") == []
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_original_v1_baseline_has_not_been_rewritten(tmp_path):
    with sqlite3.connect(tmp_path / "baseline.sqlite3") as conn:
        conn.executescript(SCHEMA)
        assert not GOAL_TABLES & {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("reviews", "spaces"):
            assert "goal_id" not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_database_runs_all_append_migrations_without_fake_legacy_goals(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    database = Database(path)
    with closing(database.connect()) as conn:
        repo = SQLiteResearchRepository(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        for table in GOAL_TABLES:
            assert repo.list(table) == []
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    Database(path)


def test_reinitialization_does_not_reimport_or_reset_mutable_goal_state(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        repo.update("goal_nodes", "task-completed", {"title": "Edited goal", "status": "paused", "revision": 2, "archived_at": 45, "archived_reason": "Superseded research direction"})
        repo.add("goal_knowledge", {"id": "link", "goal_id": "task-completed", "version_id": "unlinked-version", "linked_by": "owner", "created_at": 46})
        before = _snapshot(conn)
    for _ in range(3):
        Database(database.path)
    with closing(database.connect()) as conn:
        assert _snapshot(conn) == before


@pytest.mark.parametrize("corruption", ["task_creator", "task_assignee", "progress_task", "progress_author", "progress_version", "review_task", "space_task", "task_status"])
def test_damaged_v4_aborts_entire_migration_without_partial_schema_or_lost_rows(old_path, corruption):
    with sqlite3.connect(old_path) as conn:
        statements = {
            "task_creator": "UPDATE tasks SET created_by='missing' WHERE id='task-completed'",
            "task_assignee": "UPDATE tasks SET assignee_id='missing' WHERE id='task-completed'",
            "progress_task": "UPDATE progress SET task_id='missing' WHERE id='private-progress'",
            "progress_author": "UPDATE progress SET author_id='missing' WHERE id='private-progress'",
            "progress_version": "UPDATE progress SET version_id='missing' WHERE id='private-progress'",
            "review_task": "UPDATE reviews SET task_id='missing' WHERE id='review'",
            "space_task": "UPDATE spaces SET task_id='missing' WHERE id='collab'",
            "task_status": "UPDATE tasks SET status='accepted-again' WHERE id='task-completed'",
        }
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(statements[corruption])
        before = _snapshot(conn)
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        Database(old_path)
    with sqlite3.connect(old_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _snapshot(conn) == before
        assert not GOAL_TABLES & {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("reviews", "spaces"):
            assert "goal_id" not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.mark.parametrize("entity,key,changes", [
    ("goal_updates", "private-progress", {"content": "Tampered"}),
    ("goal_status_history", "legacy-task-status:task-completed", {"reason": "Reaccepted"}),
])
def test_updates_and_history_are_append_only_even_under_replace(database, entity, key, changes):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        before = repo.get(entity, key)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.update(entity, key, changes)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            repo.delete_where(entity, id=key)
        replacement = {**before, **changes}
        columns, placeholders = ",".join(replacement), ",".join("?" for _ in replacement)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"INSERT OR REPLACE INTO {entity} ({columns}) VALUES ({placeholders})", tuple(replacement.values()))
        assert repo.get(entity, key) == before
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(f"UPDATE {entity} SET id='replacement-id' WHERE id=?", (key,))
        assert repo.get(entity, key) == before


def test_goal_defaults_nullable_unique_legacy_link_and_nonself_parent_triggers(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        repo.add("goal_nodes", _goal())
        repo.add("goal_nodes", _goal("child", parent_id="new-goal"))
        goal = repo.get("goal_nodes", "new-goal")
        assert {key: goal[key] for key in ("description", "acceptance_criteria", "due_date", "status", "position", "revision", "archived_at", "legacy_task_id")} == {
            "description": "", "acceptance_criteria": "", "due_date": None, "status": "not_started", "position": 0,
            "revision": 1, "archived_at": None, "legacy_task_id": None,
        }
        with pytest.raises(sqlite3.IntegrityError, match="itself"):
            repo.add("goal_nodes", _goal("self", parent_id="self"))
        with pytest.raises(sqlite3.IntegrityError, match="itself"):
            repo.update("goal_nodes", "child", {"parent_id": "child"})
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            repo.add("goal_nodes", _goal("duplicate", legacy_task_id="task-completed"))
        for changes in ({"status": "accepted"}, {"position": -1}, {"position": 10001}, {"revision": 0}):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                repo.update("goal_nodes", "new-goal", changes)


@pytest.mark.parametrize("entity,record", [
    ("goal_nodes", _goal(parent_id="missing")),
    ("goal_nodes", _goal(created_by="missing")),
    ("goal_nodes", _goal(legacy_task_id="missing")),
    ("goal_updates", {"id": "new-update", "goal_id": "missing", "author_id": "owner", "content": "Update", "created_at": 40}),
    ("goal_updates", {"id": "new-update", "goal_id": "task-completed", "author_id": "missing", "content": "Update", "created_at": 40}),
    ("goal_updates", {"id": "new-update", "goal_id": "task-completed", "author_id": "owner", "content": "Update", "version_id": "missing", "created_at": 40}),
    ("goal_knowledge", {"id": "link", "goal_id": "missing", "version_id": "private-version", "linked_by": "owner", "created_at": 40}),
    ("goal_knowledge", {"id": "link", "goal_id": "task-completed", "version_id": "missing", "linked_by": "owner", "created_at": 40}),
    ("goal_knowledge", {"id": "link", "goal_id": "task-completed", "version_id": "private-version", "linked_by": "missing", "created_at": 40}),
    ("goal_status_history", {"id": "history", "goal_id": "missing", "actor_id": "owner", "from_status": "not_started", "to_status": "in_progress", "reason": "Start", "created_at": 40}),
    ("goal_status_history", {"id": "history", "goal_id": "task-completed", "actor_id": "missing", "from_status": "not_started", "to_status": "in_progress", "reason": "Start", "created_at": 40}),
])
def test_all_goal_foreign_keys_are_enforced(database, entity, record):
    with database.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            SQLiteResearchRepository(conn).add(entity, record)


def test_review_and_space_goal_foreign_keys_are_enforced(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        for entity, key in (("reviews", "review"), ("spaces", "collab")):
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                repo.update(entity, key, {"goal_id": "missing"})


def test_goal_version_references_protect_private_legacy_evidence_and_reversible_knowledge_links(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        legacy_refs = {(r["entity"], r["id"], r["field"]) for r in repo.version_references("private-version")}
        assert {("progress", "private-progress", "version_id"), ("goal_updates", "private-progress", "version_id")} <= legacy_refs
        assert repo.version_details("private-version")["space_id"] == "private"
        assert repo.version_references("unlinked-version") == []
        link = {"id": "link", "goal_id": "task-completed", "version_id": "unlinked-version", "linked_by": "owner", "created_at": 40}
        repo.add("goal_knowledge", link)
        assert repo.get("goal_knowledge", "link") == link
        assert repo.version_references("unlinked-version") == [{"entity": "goal_knowledge", "id": "link", "field": "version_id"}]
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            repo.add("goal_knowledge", {**link, "id": "duplicate-link"})
        audit_before = repo.list("audit_events")
        assert repo.delete_where("goal_knowledge", id="link") == 1
        assert repo.version_references("unlinked-version") == []
        assert repo.list("audit_events") == audit_before
        repo.add("goal_updates", {"id": "update", "goal_id": "task-completed", "author_id": "owner", "content": "Evidence", "version_id": "unlinked-version", "created_at": 41})
        assert repo.version_references("unlinked-version") == [{"entity": "goal_updates", "id": "update", "field": "version_id"}]
        assert repo.version_references("unlinked-version' OR 1=1 --") == []


def test_generic_allowlist_accepts_plain_goal_strings_without_new_json_decoding(database):
    with database.transaction() as conn:
        repo = SQLiteResearchRepository(conn)
        text = '{"literal": ["not-json-metadata"]}'
        repo.add("goal_nodes", _goal(description=text, acceptance_criteria=text))
        repo.add("goal_updates", {"id": "plain-update", "goal_id": "new-goal", "author_id": "owner", "content": text, "created_at": 40})
        assert repo.get("goal_nodes", "new-goal")["description"] == text
        assert repo.find("goal_nodes", acceptance_criteria=text)["id"] == "new-goal"
        assert repo.get("goal_updates", "plain-update")["content"] == text
        for entity in GOAL_TABLES:
            with pytest.raises(ValueError, match="Unknown"):
                repo.add(entity, {"id": "invalid", "unexpected": "not allowed"})
            with pytest.raises(ValueError, match="Unknown"):
                repo.list(entity, **{"id OR 1=1": "invalid"})
        with pytest.raises(ValueError, match="Unknown"):
            repo.add("goal_nodes", _goal("dispatch", assignee_id="assignee"))
        with pytest.raises(ValueError, match="Unknown"):
            repo.list("goal_nodes; DROP TABLE users")


def test_goal_lookup_reference_and_history_indexes_exist(database):
    with closing(database.connect()) as conn:
        expected = {
            "goal_nodes": {"goal_nodes_parent", "goal_nodes_status"},
            "goal_updates": {"goal_updates_goal", "goal_updates_version"},
            "goal_knowledge": {"goal_knowledge_version"},
            "goal_status_history": {"goal_status_history_goal"},
            "reviews": {"reviews_goal"}, "spaces": {"spaces_goal"},
        }
        for table, indexes in expected.items():
            assert indexes <= {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
