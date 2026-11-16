"""SQLite schema migration and per-operation transactions."""
from contextlib import closing, contextmanager
from pathlib import Path
import json
import sqlite3

SCHEMA_VERSION = 9
SCHEMA = """
CREATE TABLE users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('owner','member')),
 created_at REAL NOT NULL
);
CREATE UNIQUE INDEX single_owner ON users(role) WHERE role='owner';
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL REFERENCES users(id)
);
CREATE UNIQUE INDEX single_project ON projects((1));
CREATE TABLE sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 expires_at REAL NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE login_failures (
 email TEXT PRIMARY KEY, failures INTEGER NOT NULL, locked_until REAL NOT NULL,
 updated_at REAL NOT NULL
);
CREATE TABLE invites (
 id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE, role TEXT NOT NULL CHECK(role='member'),
 token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), accepted_by TEXT REFERENCES users(id),
 accepted_at REAL, created_at REAL NOT NULL
);
CREATE TABLE tasks (
 id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
 assignee_id TEXT REFERENCES users(id), due_date TEXT,
 status TEXT NOT NULL CHECK(status IN ('not_started','in_progress','pending_acceptance','completed','paused')),
 created_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX task_assignee ON tasks(assignee_id,status);
CREATE TABLE spaces (
 id TEXT PRIMARY KEY, name TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('basis','private','collaboration','results')),
 owner_id TEXT NOT NULL REFERENCES users(id), epoch INTEGER NOT NULL DEFAULT 1,
 task_id TEXT REFERENCES tasks(id), created_at REAL NOT NULL
);
CREATE UNIQUE INDEX single_system_space ON spaces(kind) WHERE kind IN ('basis','results');
CREATE UNIQUE INDEX single_private_space ON spaces(owner_id) WHERE kind='private';
CREATE TABLE space_members (
 space_id TEXT NOT NULL REFERENCES spaces(id), user_id TEXT NOT NULL REFERENCES users(id),
 role TEXT NOT NULL CHECK(role IN ('owner','editor','reader')),
 PRIMARY KEY(space_id,user_id)
);
CREATE TABLE documents (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id), title TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL
);
CREATE INDEX documents_space ON documents(space_id);
CREATE TABLE versions (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), number INTEGER NOT NULL,
 content TEXT NOT NULL, filename TEXT NOT NULL, source TEXT NOT NULL,
 created_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 copied_from TEXT REFERENCES versions(id), UNIQUE(document_id,number)
);
CREATE TRIGGER versions_immutable_update BEFORE UPDATE ON versions
 BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
CREATE TRIGGER versions_immutable_delete BEFORE DELETE ON versions
 BEGIN SELECT RAISE(ABORT,'document versions are immutable'); END;
CREATE TABLE version_indexes (
 version_id TEXT PRIMARY KEY REFERENCES versions(id),
 status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')), updated_at REAL NOT NULL
);
CREATE TABLE reviews (
 id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE REFERENCES versions(id),
 task_id TEXT REFERENCES tasks(id), submitted_by TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected')),
 reviewed_by TEXT REFERENCES users(id), comment TEXT NOT NULL DEFAULT '',
 published_version_id TEXT REFERENCES versions(id), created_at REAL NOT NULL, reviewed_at REAL
);
CREATE INDEX review_status ON reviews(status);
CREATE TABLE progress (
 id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 author_id TEXT NOT NULL REFERENCES users(id), content TEXT NOT NULL,
 version_id TEXT REFERENCES versions(id), created_at REAL NOT NULL
);
CREATE INDEX progress_task ON progress(task_id,created_at);
CREATE TABLE conversations (
 id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
 owner_id TEXT NOT NULL REFERENCES users(id), space_epoch INTEGER NOT NULL,
 created_at REAL NOT NULL
);
CREATE INDEX conversations_owner ON conversations(owner_id,space_id);
CREATE TABLE messages (
 id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 citations TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX messages_conversation ON messages(conversation_id,created_at);
CREATE TABLE audit_events (
 id TEXT PRIMARY KEY, actor_id TEXT REFERENCES users(id), action TEXT NOT NULL,
 resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX audit_time ON audit_events(created_at);
"""


# Append-only migrations: SCHEMA remains the original version-one baseline.
SCHEMA_4 = """
CREATE TABLE review_snapshots (
 review_id TEXT PRIMARY KEY REFERENCES reviews(id),
 version_id TEXT NOT NULL REFERENCES versions(id),
 document_id TEXT NOT NULL REFERENCES documents(id),
 source_space_id TEXT NOT NULL REFERENCES spaces(id),
 title TEXT NOT NULL, number INTEGER NOT NULL, content TEXT NOT NULL,
 filename TEXT NOT NULL, source TEXT NOT NULL,
 source_created_by TEXT NOT NULL REFERENCES users(id),
 copied_from TEXT REFERENCES versions(id), lineage_authors TEXT NOT NULL,
 source_space_kind TEXT NOT NULL, submission_mode TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE INDEX review_snapshots_version ON review_snapshots(version_id);
CREATE INDEX review_snapshots_document ON review_snapshots(document_id);
CREATE TABLE version_lifecycle (
 version_id TEXT PRIMARY KEY REFERENCES versions(id),
 status TEXT NOT NULL CHECK(status IN ('deleted','withdrawn')),
 reason TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL
);
CREATE INDEX version_lifecycle_status ON version_lifecycle(status);
CREATE TABLE document_heads (
 document_id TEXT PRIMARY KEY REFERENCES documents(id),
 version_id TEXT NOT NULL UNIQUE REFERENCES versions(id)
);
CREATE TRIGGER document_heads_same_document_insert BEFORE INSERT ON document_heads
 WHEN NOT EXISTS (SELECT 1 FROM versions WHERE id=NEW.version_id AND document_id=NEW.document_id)
 BEGIN SELECT RAISE(ABORT,'document head must belong to its document'); END;
CREATE TRIGGER document_heads_same_document_update BEFORE UPDATE ON document_heads
 WHEN NOT EXISTS (SELECT 1 FROM versions WHERE id=NEW.version_id AND document_id=NEW.document_id)
 BEGIN SELECT RAISE(ABORT,'document head must belong to its document'); END;
CREATE TABLE space_deletions (
 space_id TEXT PRIMARY KEY REFERENCES spaces(id),
 actor_id TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL
);
CREATE INDEX versions_copied_from ON versions(copied_from);
CREATE INDEX reviews_published_version ON reviews(published_version_id);
CREATE INDEX progress_version ON progress(version_id);
CREATE TRIGGER review_snapshots_immutable_replace BEFORE INSERT ON review_snapshots
 WHEN EXISTS (SELECT 1 FROM review_snapshots WHERE review_id=NEW.review_id)
 BEGIN SELECT RAISE(ABORT,'review snapshots are immutable'); END;
CREATE TRIGGER version_lifecycle_immutable_replace BEFORE INSERT ON version_lifecycle
 WHEN EXISTS (SELECT 1 FROM version_lifecycle WHERE version_id=NEW.version_id)
 BEGIN SELECT RAISE(ABORT,'version lifecycle receipts are immutable'); END;
CREATE TRIGGER space_deletions_immutable_replace BEFORE INSERT ON space_deletions
 WHEN EXISTS (SELECT 1 FROM space_deletions WHERE space_id=NEW.space_id)
 BEGIN SELECT RAISE(ABORT,'space deletion receipts are immutable'); END;
CREATE TRIGGER audit_events_immutable_replace BEFORE INSERT ON audit_events
 WHEN EXISTS (SELECT 1 FROM audit_events WHERE id=NEW.id)
 BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
CREATE TRIGGER review_snapshots_immutable_update BEFORE UPDATE ON review_snapshots
 BEGIN SELECT RAISE(ABORT,'review snapshots are immutable'); END;
CREATE TRIGGER review_snapshots_immutable_delete BEFORE DELETE ON review_snapshots
 BEGIN SELECT RAISE(ABORT,'review snapshots are immutable'); END;
CREATE TRIGGER version_lifecycle_immutable_update BEFORE UPDATE ON version_lifecycle
 BEGIN SELECT RAISE(ABORT,'version lifecycle receipts are immutable'); END;
CREATE TRIGGER version_lifecycle_immutable_delete BEFORE DELETE ON version_lifecycle
 BEGIN SELECT RAISE(ABORT,'version lifecycle receipts are immutable'); END;
CREATE TRIGGER space_deletions_immutable_update BEFORE UPDATE ON space_deletions
 BEGIN SELECT RAISE(ABORT,'space deletion receipts are immutable'); END;
CREATE TRIGGER space_deletions_immutable_delete BEFORE DELETE ON space_deletions
 BEGIN SELECT RAISE(ABORT,'space deletion receipts are immutable'); END;
CREATE TRIGGER audit_events_immutable_update BEFORE UPDATE ON audit_events
 BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
CREATE TRIGGER audit_events_immutable_delete BEFORE DELETE ON audit_events
 BEGIN SELECT RAISE(ABORT,'audit events are immutable'); END;
"""


SCHEMA_5 = """
CREATE TABLE goal_nodes (
 id TEXT PRIMARY KEY, parent_id TEXT REFERENCES goal_nodes(id),
 title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 acceptance_criteria TEXT NOT NULL DEFAULT '', due_date TEXT,
 status TEXT NOT NULL DEFAULT 'not_started'
 CHECK(status IN ('not_started','in_progress','pending_acceptance','completed','paused')),
 position INTEGER NOT NULL DEFAULT 0 CHECK(position BETWEEN 0 AND 10000),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 created_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 updated_at REAL NOT NULL, archived_at REAL, archived_reason TEXT DEFAULT '',
 legacy_task_id TEXT UNIQUE REFERENCES tasks(id)
);
CREATE INDEX goal_nodes_parent ON goal_nodes(parent_id,position,id);
CREATE INDEX goal_nodes_status ON goal_nodes(status,archived_at);
CREATE TRIGGER goal_nodes_parent_nonself_insert BEFORE INSERT ON goal_nodes
 WHEN NEW.parent_id=NEW.id
 BEGIN SELECT RAISE(ABORT,'goal parent cannot be itself'); END;
CREATE TRIGGER goal_nodes_parent_nonself_update BEFORE UPDATE ON goal_nodes
 WHEN NEW.parent_id=NEW.id
 BEGIN SELECT RAISE(ABORT,'goal parent cannot be itself'); END;
CREATE TABLE goal_updates (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_nodes(id),
 author_id TEXT NOT NULL REFERENCES users(id), content TEXT NOT NULL,
 version_id TEXT REFERENCES versions(id), created_at REAL NOT NULL
);
CREATE INDEX goal_updates_goal ON goal_updates(goal_id,created_at,id);
CREATE INDEX goal_updates_version ON goal_updates(version_id);
CREATE TABLE goal_knowledge (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_nodes(id),
 version_id TEXT NOT NULL REFERENCES versions(id),
 linked_by TEXT NOT NULL REFERENCES users(id), created_at REAL NOT NULL,
 UNIQUE(goal_id,version_id)
);
CREATE INDEX goal_knowledge_version ON goal_knowledge(version_id);
CREATE TABLE goal_status_history (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_nodes(id),
 actor_id TEXT NOT NULL REFERENCES users(id),
 from_status TEXT NOT NULL
 CHECK(from_status IN ('not_started','in_progress','pending_acceptance','completed','paused')),
 to_status TEXT NOT NULL
 CHECK(to_status IN ('not_started','in_progress','pending_acceptance','completed','paused')),
 reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX goal_status_history_goal ON goal_status_history(goal_id,created_at,id);
CREATE TRIGGER goal_updates_immutable_replace BEFORE INSERT ON goal_updates
 WHEN EXISTS (SELECT 1 FROM goal_updates WHERE id=NEW.id)
 BEGIN SELECT RAISE(ABORT,'goal updates are immutable'); END;
CREATE TRIGGER goal_updates_immutable_update BEFORE UPDATE ON goal_updates
 BEGIN SELECT RAISE(ABORT,'goal updates are immutable'); END;
CREATE TRIGGER goal_updates_immutable_delete BEFORE DELETE ON goal_updates
 BEGIN SELECT RAISE(ABORT,'goal updates are immutable'); END;
CREATE TRIGGER goal_status_history_immutable_replace BEFORE INSERT ON goal_status_history
 WHEN EXISTS (SELECT 1 FROM goal_status_history WHERE id=NEW.id)
 BEGIN SELECT RAISE(ABORT,'goal status history is immutable'); END;
CREATE TRIGGER goal_status_history_immutable_update BEFORE UPDATE ON goal_status_history
 BEGIN SELECT RAISE(ABORT,'goal status history is immutable'); END;
CREATE TRIGGER goal_status_history_immutable_delete BEFORE DELETE ON goal_status_history
 BEGIN SELECT RAISE(ABORT,'goal status history is immutable'); END;
ALTER TABLE reviews ADD COLUMN goal_id TEXT REFERENCES goal_nodes(id);
ALTER TABLE spaces ADD COLUMN goal_id TEXT REFERENCES goal_nodes(id);
CREATE INDEX reviews_goal ON reviews(goal_id);
CREATE INDEX spaces_goal ON spaces(goal_id);
"""


SCHEMA_6 = """
CREATE TABLE goal_drafts (
 id TEXT PRIMARY KEY, created_by TEXT NOT NULL REFERENCES users(id),
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published')),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
 sources TEXT NOT NULL, sources_hash TEXT NOT NULL, base_tree_hash TEXT NOT NULL,
 nodes TEXT NOT NULL, method TEXT NOT NULL, limitations TEXT NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, published_at REAL
);
CREATE INDEX goal_drafts_creator ON goal_drafts(created_by,status,created_at,id);
CREATE TABLE goal_draft_publications (
 draft_id TEXT PRIMARY KEY REFERENCES goal_drafts(id),
 result TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TRIGGER goal_drafts_published_update BEFORE UPDATE ON goal_drafts
 WHEN OLD.status='published' OR EXISTS (
  SELECT 1 FROM goal_drafts WHERE id=NEW.id AND status='published'
 )
 BEGIN SELECT RAISE(ABORT,'published goal drafts are immutable'); END;
CREATE TRIGGER goal_drafts_published_delete BEFORE DELETE ON goal_drafts
 WHEN OLD.status='published'
 BEGIN SELECT RAISE(ABORT,'published goal drafts are immutable'); END;
CREATE TRIGGER goal_drafts_published_replace BEFORE INSERT ON goal_drafts
 WHEN EXISTS (SELECT 1 FROM goal_drafts WHERE id=NEW.id AND status='published')
 BEGIN SELECT RAISE(ABORT,'published goal drafts are immutable'); END;
CREATE TRIGGER goal_draft_publications_immutable_update BEFORE UPDATE ON goal_draft_publications
 BEGIN SELECT RAISE(ABORT,'goal draft publications are immutable'); END;
CREATE TRIGGER goal_draft_publications_immutable_delete BEFORE DELETE ON goal_draft_publications
 BEGIN SELECT RAISE(ABORT,'goal draft publications are immutable'); END;
CREATE TRIGGER goal_draft_publications_immutable_replace BEFORE INSERT ON goal_draft_publications
 WHEN EXISTS (SELECT 1 FROM goal_draft_publications WHERE draft_id=NEW.draft_id)
 BEGIN SELECT RAISE(ABORT,'goal draft publications are immutable'); END;
"""


SCHEMA_7 = """
ALTER TABLE goal_drafts ADD COLUMN generation TEXT NOT NULL DEFAULT '{}';
ALTER TABLE goal_drafts ADD COLUMN suggestions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE goal_drafts ADD COLUMN regenerates_id TEXT REFERENCES goal_drafts(id);
ALTER TABLE goal_drafts ADD COLUMN abandoned_at REAL;
ALTER TABLE goal_nodes ADD COLUMN deliverables TEXT NOT NULL DEFAULT '';
ALTER TABLE goal_nodes ADD COLUMN constraints TEXT NOT NULL DEFAULT '';
ALTER TABLE goal_nodes ADD COLUMN time_requirement TEXT NOT NULL DEFAULT '';
CREATE TABLE knowledge_imports (
 version_id TEXT PRIMARY KEY REFERENCES versions(id), report TEXT NOT NULL,
 structure_map TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE goal_generations (
 draft_id TEXT PRIMARY KEY REFERENCES goal_drafts(id), snapshot TEXT NOT NULL,
 created_at REAL NOT NULL
);
CREATE TABLE goal_generation_sources (
 id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goal_nodes(id),
 draft_id TEXT NOT NULL REFERENCES goal_drafts(id), node_id TEXT NOT NULL,
 node TEXT NOT NULL, generation TEXT NOT NULL, confirmation TEXT NOT NULL,
 operation TEXT NOT NULL CHECK(operation IN ('add','modify','move','retire')),
 created_at REAL NOT NULL
);
CREATE INDEX goal_generation_sources_goal ON goal_generation_sources(goal_id,created_at,id);
"""
for _table, _key in (("knowledge_imports", "version_id"), ("goal_generations", "draft_id"),
                     ("goal_generation_sources", "id")):
    SCHEMA_7 += f"""
CREATE TRIGGER {_table}_immutable_update BEFORE UPDATE ON {_table}
 BEGIN SELECT RAISE(ABORT,'generation/import evidence is immutable'); END;
CREATE TRIGGER {_table}_immutable_delete BEFORE DELETE ON {_table}
 BEGIN SELECT RAISE(ABORT,'generation/import evidence is immutable'); END;
CREATE TRIGGER {_table}_immutable_replace BEFORE INSERT ON {_table}
 WHEN EXISTS (SELECT 1 FROM {_table} WHERE {_key}=NEW.{_key})
 BEGIN SELECT RAISE(ABORT,'generation/import evidence is immutable'); END;
"""
SCHEMA_7 += """
CREATE TRIGGER goal_drafts_provenance_update BEFORE UPDATE ON goal_drafts
 WHEN NEW.sources!=OLD.sources OR NEW.sources_hash!=OLD.sources_hash OR NEW.method!=OLD.method
 OR NEW.generation!=OLD.generation OR NEW.regenerates_id IS NOT OLD.regenerates_id
 BEGIN SELECT RAISE(ABORT,'goal draft generation provenance is immutable'); END;
"""


SCHEMA_8 = """
CREATE TABLE user_action_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 event_type TEXT NOT NULL CHECK(event_type IN ('user.answer_feedback','user.session_action')),
 action_type TEXT NOT NULL,
 actor_id TEXT NOT NULL REFERENCES users(id),
 space_id TEXT NOT NULL REFERENCES spaces(id),
 conversation_id TEXT REFERENCES conversations(id),
 run_id TEXT,
 document_version_id TEXT REFERENCES versions(id),
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL
);
CREATE INDEX user_action_events_actor ON user_action_events(actor_id, created_at);
CREATE INDEX user_action_events_space ON user_action_events(space_id, created_at);
CREATE INDEX user_action_events_type ON user_action_events(event_type, action_type, created_at);
CREATE TRIGGER user_action_events_immutable_update BEFORE UPDATE ON user_action_events
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
CREATE TRIGGER user_action_events_immutable_delete BEFORE DELETE ON user_action_events
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
CREATE TRIGGER user_action_events_immutable_replace BEFORE INSERT ON user_action_events
 WHEN EXISTS (SELECT 1 FROM user_action_events WHERE seq=NEW.seq)
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
"""

SCHEMA_9 = """
DROP TRIGGER user_action_events_immutable_update;
DROP TRIGGER user_action_events_immutable_delete;
DROP TRIGGER user_action_events_immutable_replace;
DROP INDEX user_action_events_actor;
DROP INDEX user_action_events_space;
DROP INDEX user_action_events_type;
ALTER TABLE user_action_events RENAME TO user_action_events_v8;
CREATE TABLE user_action_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 event_type TEXT NOT NULL CHECK(event_type LIKE 'user.%' OR event_type LIKE 'security.%'),
 action_type TEXT NOT NULL,
 actor_id TEXT NOT NULL REFERENCES users(id),
 space_id TEXT REFERENCES spaces(id),
 conversation_id TEXT REFERENCES conversations(id),
 run_id TEXT,
 document_version_id TEXT REFERENCES versions(id),
 payload_json TEXT NOT NULL DEFAULT '{}',
 created_at REAL NOT NULL
);
CREATE INDEX user_action_events_actor ON user_action_events(actor_id, created_at);
CREATE INDEX user_action_events_space ON user_action_events(space_id, created_at);
CREATE INDEX user_action_events_type ON user_action_events(event_type, action_type, created_at);
CREATE TRIGGER user_action_events_immutable_update BEFORE UPDATE ON user_action_events
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
CREATE TRIGGER user_action_events_immutable_delete BEFORE DELETE ON user_action_events
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
CREATE TRIGGER user_action_events_immutable_replace BEFORE INSERT ON user_action_events
 WHEN EXISTS (SELECT 1 FROM user_action_events WHERE seq=NEW.seq)
 BEGIN SELECT RAISE(ABORT,'user action events are immutable'); END;
INSERT INTO user_action_events
 (seq, event_type, action_type, actor_id, space_id, conversation_id, run_id, document_version_id, payload_json, created_at)
 SELECT seq, event_type, action_type, actor_id, space_id, conversation_id, run_id, document_version_id, payload_json, created_at
 FROM user_action_events_v8;
DROP TABLE user_action_events_v8;
"""


def _execute_statements(conn, script):
    """Unlike executescript, preserve the initializer's transaction and lock."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""


def _migrate_schema_4(conn):
    _execute_statements(conn, SCHEMA_4)
    conn.execute(
        "INSERT INTO document_heads(document_id,version_id) "
        "SELECT v.document_id,v.id FROM versions v "
        "WHERE v.number=(SELECT MAX(v2.number) FROM versions v2 WHERE v2.document_id=v.document_id)"
    )
    # Do not use an inner-join-only backfill: orphaned old reviews must abort the
    # entire migration rather than silently lose their immutable review evidence.
    for review in conn.execute("SELECT id,version_id,created_at FROM reviews ORDER BY id").fetchall():
        version = conn.execute(
            "SELECT v.*,d.title,d.space_id,s.kind AS source_space_kind "
            "FROM versions v JOIN documents d ON d.id=v.document_id "
            "JOIN spaces s ON s.id=d.space_id WHERE v.id=?", (review["version_id"],)
        ).fetchone()
        if version is None:
            raise RuntimeError(f"Cannot freeze review {review['id']}: missing source version, document or space")
        authors, seen = set(), set()
        ancestor_id = version["id"]
        while ancestor_id is not None:
            if ancestor_id in seen:
                raise RuntimeError(f"Cannot freeze review {review['id']}: cyclic copy lineage")
            seen.add(ancestor_id)
            ancestor = conn.execute("SELECT created_by,copied_from FROM versions WHERE id=?", (ancestor_id,)).fetchone()
            if ancestor is None:
                raise RuntimeError(f"Cannot freeze review {review['id']}: missing copy lineage version")
            if conn.execute("SELECT 1 FROM users WHERE id=?", (ancestor["created_by"],)).fetchone() is None:
                raise RuntimeError(f"Cannot freeze review {review['id']}: missing lineage author")
            authors.add(ancestor["created_by"])
            ancestor_id = ancestor["copied_from"]
        conn.execute(
            "INSERT INTO review_snapshots (review_id,version_id,document_id,source_space_id,title,number,"
            "content,filename,source,source_created_by,copied_from,lineage_authors,source_space_kind,submission_mode,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (review["id"], version["id"], version["document_id"], version["space_id"], version["title"],
             version["number"], version["content"], version["filename"], version["source"], version["created_by"],
             version["copied_from"], json.dumps(sorted(authors), ensure_ascii=False),
             version["source_space_kind"], "snapshot", review["created_at"]),
        )


def _migrate_schema_5(conn):
    # Old clients may have written with FK checks disabled. Never silently drop
    # orphaned task/progress/review/space links while importing their evidence.
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Cannot import legacy goals: database has broken foreign-key references")
    _execute_statements(conn, SCHEMA_5)
    conn.execute(
        "INSERT INTO goal_nodes (id,parent_id,title,description,acceptance_criteria,due_date,status,"
        "position,revision,created_by,created_at,updated_at,archived_at,legacy_task_id) "
        "SELECT id,NULL,title,description,'',due_date,status,0,1,created_by,created_at,updated_at,NULL,id "
        "FROM tasks"
    )
    conn.execute(
        "INSERT INTO goal_updates (id,goal_id,author_id,content,version_id,created_at) "
        "SELECT id,task_id,author_id,content,version_id,created_at FROM progress"
    )
    conn.execute(
        "INSERT INTO goal_status_history (id,goal_id,actor_id,from_status,to_status,reason,created_at) "
        "SELECT 'legacy-task-status:' || id,id,created_by,status,status,"
        "'legacy_import: preserved task state, not a new acceptance or status transition',updated_at "
        "FROM tasks"
    )
    conn.execute("UPDATE reviews SET goal_id=task_id WHERE task_id IS NOT NULL")
    conn.execute("UPDATE spaces SET goal_id=task_id WHERE task_id IS NOT NULL")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("Cannot import legacy goals: migrated references are inconsistent")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            # executescript commits implicitly: begin inside the script so concurrent
            # initializers cannot observe a partly-created migration.
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                conn.rollback()
                raise RuntimeError("Research database schema is newer than this application")
            if version == 0:
                _execute_statements(conn, SCHEMA)
                version = 1
            if version == 1:
                conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
                conn.execute(
                    "CREATE TABLE document_withdrawals (document_id TEXT PRIMARY KEY REFERENCES documents(id), "
                    "withdrawn_by TEXT NOT NULL REFERENCES users(id), withdrawn_at REAL NOT NULL)"
                )
                conn.execute("CREATE INDEX active_users ON users(disabled_at,role)")
                version = 2
            if version == 2:
                conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
                version = 3
            if version == 3:
                _migrate_schema_4(conn)
                version = 4
            if version == 4:
                _migrate_schema_5(conn)
                version = 5
            if version == 5:
                _execute_statements(conn, SCHEMA_6)
                version = 6
            if version == 6:
                _execute_statements(conn, SCHEMA_7)
                version = 7
            if version == 7:
                _execute_statements(conn, SCHEMA_8)
                version = 8
            if version == 8:
                _execute_statements(conn, SCHEMA_9)
                version = 9
            conn.execute(f"PRAGMA user_version={version}")
            conn.commit()

    def connect(self):
        conn = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            # Cancellation and answer commit share the application's narrow guard.
            # The context variable is unset for all other transactions.
            from app.domain.ai_ports import answer_commit_guard
            guard = answer_commit_guard.get()
            if guard is None:
                conn.commit()
            else:
                with guard.commit_scope():
                    conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
