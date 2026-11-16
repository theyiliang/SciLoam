"""SQLite research records and explicit read models, without business decisions."""
from __future__ import annotations
import json
import sqlite3

from app.domain.models import Record

# Fixed identifiers are the only identifiers interpolated into SQL. Callers can
# supply values and equality filters but cannot supply SQL or an executable query.
TABLES = {
    "users": ("id", {"id", "email", "password_hash", "role", "created_at", "disabled_at"}),
    "projects": ("id", {"id", "name", "owner_id"}),
    "sessions": ("token_hash", {"token_hash", "user_id", "expires_at", "created_at"}),
    "login_failures": ("email", {"email", "failures", "locked_until", "updated_at"}),
    "invites": ("id", {"id", "email", "role", "token_hash", "expires_at", "created_by", "accepted_by", "accepted_at", "created_at"}),
    "tasks": ("id", {"id", "title", "description", "assignee_id", "due_date", "status", "created_by", "created_at", "updated_at"}),
    "goal_drafts": ("id", {"id", "created_by", "status", "revision", "sources", "sources_hash", "base_tree_hash", "nodes", "method", "limitations", "created_at", "updated_at", "published_at", "generation", "suggestions", "regenerates_id", "abandoned_at"}),
    "goal_draft_publications": ("draft_id", {"draft_id", "result", "created_at"}),
    "goal_nodes": ("id", {"id", "parent_id", "title", "description", "acceptance_criteria", "due_date", "status", "position", "revision", "created_by", "created_at", "updated_at", "archived_at", "archived_reason", "legacy_task_id", "deliverables", "constraints", "time_requirement"}),
    "knowledge_imports": ("version_id", {"version_id", "report", "structure_map", "created_at"}),
    "goal_generations": ("draft_id", {"draft_id", "snapshot", "created_at"}),
    "goal_generation_sources": ("id", {"id", "goal_id", "draft_id", "node_id", "node", "generation", "confirmation", "operation", "created_at"}),
    "goal_updates": ("id", {"id", "goal_id", "author_id", "content", "version_id", "created_at"}),
    "goal_knowledge": ("id", {"id", "goal_id", "version_id", "linked_by", "created_at"}),
    "goal_status_history": ("id", {"id", "goal_id", "actor_id", "from_status", "to_status", "reason", "created_at"}),
    "spaces": ("id", {"id", "name", "kind", "owner_id", "epoch", "task_id", "goal_id", "created_at"}),
    "space_members": (None, {"space_id", "user_id", "role"}),
    "documents": ("id", {"id", "space_id", "title", "created_by", "created_at"}),
    "versions": ("id", {"id", "document_id", "number", "content", "filename", "source", "created_by", "created_at", "copied_from"}),
    "version_indexes": ("version_id", {"version_id", "status", "updated_at"}),
    "document_withdrawals": ("document_id", {"document_id", "withdrawn_by", "withdrawn_at"}),
    "document_heads": ("document_id", {"document_id", "version_id"}),
    "version_lifecycle": ("version_id", {"version_id", "status", "reason", "actor_id", "created_at"}),
    "space_deletions": ("space_id", {"space_id", "actor_id", "created_at"}),
    "review_snapshots": ("review_id", {"review_id", "version_id", "document_id", "source_space_id", "title", "number", "content", "filename", "source", "source_created_by", "copied_from", "lineage_authors", "source_space_kind", "submission_mode", "created_at"}),
    "reviews": ("id", {"id", "version_id", "task_id", "goal_id", "submitted_by", "status", "reviewed_by", "comment", "published_version_id", "created_at", "reviewed_at"}),
    "progress": ("id", {"id", "task_id", "author_id", "content", "version_id", "created_at"}),
    "conversations": ("id", {"id", "space_id", "owner_id", "space_epoch", "created_at"}),
    "messages": ("id", {"id", "conversation_id", "role", "content", "citations", "metadata", "created_at"}),
    "audit_events": ("id", {"id", "actor_id", "action", "resource_type", "resource_id", "created_at"}),
    "user_action_events": ("seq", {"seq", "event_type", "action_type", "actor_id", "space_id", "conversation_id", "run_id", "document_version_id", "payload_json", "created_at"}),
}


JSON_FIELDS = {
    "messages": ("citations", "metadata"),
    "review_snapshots": ("lineage_authors",),
    "goal_drafts": ("sources", "nodes", "limitations", "generation", "suggestions"),
    "knowledge_imports": ("report", "structure_map"),
    "goal_generations": ("snapshot",),
    "goal_generation_sources": ("node", "generation", "confirmation"),
    "goal_draft_publications": ("result",),
}


class SQLiteResearchRepository:
    def __init__(self, connection: sqlite3.Connection):
        self._connection = connection

    @staticmethod
    def _validate(entity, columns=()):
        if entity not in TABLES or not set(columns) <= TABLES[entity][1]:
            raise ValueError("Unknown research entity or field")
        return TABLES[entity][0]

    @staticmethod
    def _decode(entity, row):
        if row is None:
            return None
        record = dict(row)
        for name in JSON_FIELDS.get(entity, ()):
            record[name] = json.loads(record[name])
        return record

    def _where(self, entity, filters):
        self._validate(entity, filters)
        return (" WHERE " + " AND ".join(f"{key} IS ?" for key in filters)) if filters else ""

    def get(self, entity: str, key: str) -> Record | None:
        primary_key = self._validate(entity)
        if primary_key is None:
            raise ValueError("Entity has a compound key")
        return self.find(entity, **{primary_key: key})

    def find(self, entity: str, **filters) -> Record | None:
        where = self._where(entity, filters)
        row = self._connection.execute(f"SELECT * FROM {entity}{where} LIMIT 1", tuple(filters.values())).fetchone()
        return self._decode(entity, row)

    def list(self, entity: str, **filters) -> list[Record]:
        where = self._where(entity, filters)
        fields = TABLES[entity][1]
        order = "created_at,id" if {"created_at", "id"} <= fields else TABLES[entity][0]
        query = f"SELECT * FROM {entity}{where}" + (f" ORDER BY {order}" if order else "")
        return [self._decode(entity, row) for row in self._connection.execute(query, tuple(filters.values())).fetchall()]

    def add(self, entity: str, record: Record) -> None:
        self._validate(entity, record)
        values = dict(record)
        if entity == "messages":
            values.setdefault("metadata", {})
        for name in JSON_FIELDS.get(entity, ()):
            if name in values:
                values[name] = json.dumps(values[name], ensure_ascii=False)
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self._connection.execute(f"INSERT INTO {entity} ({columns}) VALUES ({placeholders})", tuple(values.values()))

    def update(self, entity: str, key: str, changes: Record) -> None:
        primary_key = self._validate(entity, changes)
        if not primary_key:
            raise ValueError("Entity has a compound key")
        if not changes:
            return
        values = dict(changes)
        for name in JSON_FIELDS.get(entity, ()):
            if name in values:
                values[name] = json.dumps(values[name], ensure_ascii=False)
        assignments = ",".join(f"{name}=?" for name in values)
        self._connection.execute(f"UPDATE {entity} SET {assignments} WHERE {primary_key}=?", (*values.values(), key))

    def delete_where(self, entity: str, **filters) -> int:
        if not filters:
            raise ValueError("Deletion requires a filter")
        where = self._where(entity, filters)
        return self._connection.execute(f"DELETE FROM {entity}{where}", tuple(filters.values())).rowcount

    def set_membership(self, space_id, user_id, role):
        self._connection.execute("INSERT INTO space_members VALUES (?,?,?) ON CONFLICT(space_id,user_id) DO UPDATE SET role=excluded.role", (space_id, user_id, role))

    def membership(self, space_id, user_id):
        row = self.find("space_members", space_id=space_id, user_id=user_id)
        return row["role"] if row else None

    def space_members(self, space_id):
        return [dict(row) for row in self._connection.execute("SELECT u.id,u.email,m.role FROM space_members m JOIN users u ON u.id=m.user_id WHERE m.space_id=? AND u.disabled_at IS NULL ORDER BY u.id", (space_id,))]

    def member_space_ids(self, user_id):
        return [row[0] for row in self._connection.execute("SELECT id FROM spaces WHERE owner_id=? UNION SELECT space_id FROM space_members WHERE user_id=?", (user_id, user_id))]

    def version_details(self, version_id):
        row = self._connection.execute(
            "SELECT v.*,d.space_id,d.title,i.status AS index_status,"
            "COALESCE(l.status,'active') AS lifecycle,l.reason AS lifecycle_reason,"
            "l.actor_id AS lifecycle_actor_id,l.created_at AS lifecycle_at,"
            "CASE WHEN h.version_id=v.id THEN 1 ELSE 0 END AS is_head "
            "FROM versions v JOIN documents d ON d.id=v.document_id "
            "LEFT JOIN version_indexes i ON i.version_id=v.id "
            "LEFT JOIN version_lifecycle l ON l.version_id=v.id "
            "LEFT JOIN document_heads h ON h.document_id=d.id WHERE v.id=?", (version_id,)
        ).fetchone()
        return dict(row) if row else None

    def version_metadata(self, document_id):
        # All immutable historical versions remain discoverable as metadata only.
        return [dict(row) for row in self._connection.execute(
            "SELECT v.id,v.document_id,v.number,v.filename,v.source,v.created_by,v.created_at,v.copied_from,"
            "i.status AS index_status,COALESCE(l.status,'active') AS lifecycle,"
            "l.reason AS lifecycle_reason,l.actor_id AS lifecycle_actor_id,l.created_at AS lifecycle_at,"
            "CASE WHEN h.version_id=v.id THEN 1 ELSE 0 END AS is_head "
            "FROM versions v LEFT JOIN version_indexes i ON i.version_id=v.id "
            "LEFT JOIN version_lifecycle l ON l.version_id=v.id "
            "LEFT JOIN document_heads h ON h.document_id=v.document_id "
            "WHERE v.document_id=? ORDER BY v.number", (document_id,)
        )]

    def set_document_head(self, document_id, version_id):
        self._connection.execute(
            "INSERT INTO document_heads(document_id,version_id) VALUES (?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET version_id=excluded.version_id", (document_id, version_id)
        )

    def active_version_details(self, version_id):
        row = self._connection.execute(
            "SELECT v.id FROM versions v JOIN documents d ON d.id=v.document_id "
            "JOIN document_heads h ON h.document_id=d.id AND h.version_id=v.id "
            "WHERE v.id=? AND NOT EXISTS (SELECT 1 FROM version_lifecycle l WHERE l.version_id=v.id) "
            "AND NOT EXISTS (SELECT 1 FROM document_withdrawals w WHERE w.document_id=d.id) "
            "AND NOT EXISTS (SELECT 1 FROM space_deletions s WHERE s.space_id=d.space_id)", (version_id,)
        ).fetchone()
        return self.version_details(row["id"]) if row else None

    def document_active_head(self, document_id):
        head = self.get("document_heads", document_id)
        return self.active_version_details(head["version_id"]) if head else None

    def next_version_number(self, document_id):
        return self._connection.execute("SELECT COALESCE(MAX(number),0)+1 FROM versions WHERE document_id=?", (document_id,)).fetchone()[0]

    def lineage_authors(self, version_id):
        rows = self._connection.execute("WITH RECURSIVE lineage(id,created_by,copied_from) AS (SELECT id,created_by,copied_from FROM versions WHERE id=? UNION SELECT v.id,v.created_by,v.copied_from FROM versions v JOIN lineage l ON v.id=l.copied_from) SELECT DISTINCT created_by FROM lineage", (version_id,))
        return {row[0] for row in rows}

    def searchable_versions(self, space_id):
        rows = self._connection.execute(
            "SELECT v.id AS version_id,v.document_id,v.number,d.title,v.content,v.source,i.status AS index_status "
            "FROM document_heads h JOIN versions v ON v.id=h.version_id AND v.document_id=h.document_id "
            "JOIN documents d ON d.id=h.document_id JOIN version_indexes i ON i.version_id=v.id "
            "WHERE d.space_id=? AND i.status='ready' "
            "AND NOT EXISTS (SELECT 1 FROM version_lifecycle l WHERE l.version_id=v.id) "
            "AND NOT EXISTS (SELECT 1 FROM document_withdrawals w WHERE w.document_id=d.id) "
            "AND NOT EXISTS (SELECT 1 FROM space_deletions s WHERE s.space_id=d.space_id) "
            "ORDER BY v.created_at DESC,v.id", (space_id,)
        )
        return [dict(row) for row in rows]

    def version_references(self, version_id):
        """Return durable downstream references, never interpreting JSON as SQL."""
        references = []
        for entity, field in (("reviews", "version_id"), ("reviews", "published_version_id"),
                              ("versions", "copied_from"), ("progress", "version_id"),
                              ("goal_updates", "version_id"), ("goal_knowledge", "version_id"),
                              ("review_snapshots", "version_id"), ("review_snapshots", "copied_from")):
            primary_key = TABLES[entity][0]
            rows = self._connection.execute(f"SELECT {primary_key} FROM {entity} WHERE {field}=?", (version_id,))
            references.extend({"entity": entity, "id": row[0], "field": field} for row in rows)
        for row in self._connection.execute("SELECT id,citations FROM messages ORDER BY id"):
            try:
                citations = json.loads(row["citations"])
            except (ValueError, TypeError, RecursionError):
                # Malformed historical data cannot establish that deletion is safe.
                references.append({"entity": "messages", "id": row["id"], "field": "citations", "invalid_json": True})
                continue
            if not isinstance(citations, list):
                references.append({"entity": "messages", "id": row["id"], "field": "citations", "invalid_json": True})
            elif any(citation == version_id if isinstance(citation, str)
                     else isinstance(citation, dict) and citation.get("version_id") == version_id
                     for citation in citations):
                references.append({"entity": "messages", "id": row["id"], "field": "citations"})
        for row in self._connection.execute("SELECT id,sources FROM goal_drafts ORDER BY id"):
            try:
                sources = json.loads(row["sources"])
            except (ValueError, TypeError, RecursionError):
                sources = None
            # Fail closed on malformed historical evidence rather than allow a
            # deletion merely because its JSON could not be understood.
            if not isinstance(sources, list) or any(
                not isinstance(source, dict) or not isinstance(source.get("version_id"), str)
                for source in sources
            ):
                references.append({"entity": "goal_drafts", "id": row["id"], "field": "sources", "invalid_json": True})
            elif any(source["version_id"] == version_id for source in sources):
                references.append({"entity": "goal_drafts", "id": row["id"], "field": "sources"})
        return references

    def save_login_failure(self, email, record):
        self._connection.execute("INSERT INTO login_failures VALUES (?,?,?,?) ON CONFLICT(email) DO UPDATE SET failures=excluded.failures,locked_until=excluded.locked_until,updated_at=excluded.updated_at", (email, record["failures"], record["locked_until"], record["updated_at"]))

    def track_user_action(self, event_type, action_type, actor_id, space_id, conversation_id, run_id, document_version_id, payload_json, created_at):
        self._connection.execute(
            "INSERT INTO user_action_events (event_type, action_type, actor_id, space_id, conversation_id, run_id, document_version_id, payload_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (event_type, action_type, actor_id, space_id, conversation_id, run_id, document_version_id, payload_json, created_at),
        )

    def resolve_space_id(self, conversation_id):
        row = self._connection.execute("SELECT space_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return row["space_id"] if row else None
