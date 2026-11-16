"""Independent SQLite connections to the research database; no core DB imports.

Only additive CREATE IF NOT EXISTS migrations, without touching user_version or
core tables. BEGIN IMMEDIATE serializes ledger claims across processes. Business
operations run OUTSIDE these transactions: there is deliberately no claim of
crash exactly-once until a shared business/ledger UoW exists.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import secrets
import sqlite3

from app.domain.engineering_ports import (
    EngineeringConflict, LedgerClaim, LedgerReservation, ResourceReference,
    ResponseMetadata,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_request_ledger (
 actor TEXT NOT NULL, key_hash TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL,
 scope_fingerprint TEXT NOT NULL, auth_epoch TEXT NOT NULL, request_hash TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('in_progress','completed')),
 lease_token TEXT NOT NULL, pending_expires_at REAL NOT NULL,
 expires_at REAL, response_metadata TEXT, response_hash TEXT,
 PRIMARY KEY(actor,key_hash,method,path)
);
CREATE INDEX IF NOT EXISTS research_request_ledger_expiry
 ON research_request_ledger(state,expires_at);
CREATE TABLE IF NOT EXISTS research_user_preferences (
 actor TEXT PRIMARY KEY,
 language TEXT CHECK(language IN ('zh-CN','en-US')),
 answer_style TEXT CHECK(answer_style IN ('concise','detailed')),
 citation_style TEXT CHECK(citation_style IN ('inline','list'))
);
"""


class SQLiteResearchEngineering:
    """Implements RequestLedger using one fresh connection/call.

    Use the same on-disk db_path as core research persistence, never ':memory:'.
    Scope is stored alongside the request identity, then compared on claim: this
    stronger uniqueness fence prevents a changed ACL from silently re-running a
    previously used key (a composite PK including scope alone would allow that).
    No request bodies, response bodies, raw keys or authentication tokens persist.
    """

    def __init__(self, db_path: str | Path, *, timeout_seconds: float = 10):
        if str(db_path) == ":memory:":
            raise ValueError("Independent connections require an on-disk database")
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._timeout = timeout_seconds
        with closing(self._connect()) as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self._timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self):
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def _identity(claim: LedgerClaim) -> tuple[str, str, str, str]:
        return claim.actor, claim.key, claim.method, claim.path

    def claim(self, claim: LedgerClaim) -> LedgerReservation:
        identity = self._identity(claim)
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM research_request_ledger WHERE actor=? AND key_hash=? AND method=? AND path=?",
                identity,
            ).fetchone()
            if row is not None:
                # Epoch/scope changes refuse even when the completed receipt TTL
                # elapsed. Callers must use a fresh key after reauthorization.
                if row["scope_fingerprint"] != claim.scope_fingerprint or row["auth_epoch"] != claim.auth_epoch:
                    raise EngineeringConflict("authorization_changed", "Request authorization scope changed")
                completed_expired = row["state"] == "completed" and row["expires_at"] <= claim.now
                if not completed_expired and row["request_hash"] != claim.request_hash:
                    raise EngineeringConflict("body_conflict", "Idempotency-Key was used with a different request")
                if row["state"] == "in_progress":
                    if row["pending_expires_at"] <= claim.now:
                        raise EngineeringConflict("unknown_outcome", "Expired pending request requires manual reconciliation")
                    raise EngineeringConflict("in_progress", "Request is already in progress")
                if row["expires_at"] > claim.now:
                    data = json.loads(row["response_metadata"])
                    metadata = ResponseMetadata(
                        status_code=data["status_code"],
                        resources=tuple(ResourceReference(**item) for item in data["resources"]),
                    )
                    return LedgerReservation(metadata=metadata, response_hash=row["response_hash"])
                conn.execute(
                    "DELETE FROM research_request_ledger WHERE actor=? AND key_hash=? AND method=? AND path=?",
                    identity,
                )
            lease = secrets.token_hex(24)
            conn.execute(
                "INSERT INTO research_request_ledger "
                "(actor,key_hash,method,path,scope_fingerprint,auth_epoch,request_hash,state,lease_token,pending_expires_at) "
                "VALUES (?,?,?,?,?,?,?,'in_progress',?,?)",
                (*identity, claim.scope_fingerprint, claim.auth_epoch, claim.request_hash, lease, claim.pending_expires_at),
            )
            return LedgerReservation(lease_token=lease)

    def complete(
        self, claim: LedgerClaim, lease_token: str, metadata: ResponseMetadata,
        response_hash: str, expires_at: float,
    ) -> None:
        # Explicit schema, not arbitrary dict serialization: only resource IDs and
        # status may be persisted, even when the business response is private.
        if not isinstance(metadata, ResponseMetadata) or not metadata.resources:
            raise ValueError("Resource response metadata is required")
        for resource in metadata.resources:
            if not isinstance(resource, ResourceReference) or not resource.resource_type or not resource.resource_id:
                raise ValueError("Each resource reference requires type and ID")
        encoded = json.dumps({
            "status_code": metadata.status_code,
            "resources": [{"resource_type": r.resource_type, "resource_id": r.resource_id} for r in metadata.resources],
        }, separators=(",", ":"))
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE research_request_ledger SET state='completed',response_metadata=?,response_hash=?,expires_at=? "
                "WHERE actor=? AND key_hash=? AND method=? AND path=? AND state='in_progress' AND lease_token=?",
                (encoded, response_hash, expires_at, *self._identity(claim), lease_token),
            ).rowcount
            if changed != 1:
                raise EngineeringConflict("reservation_lost", "Request reservation is no longer owned")

    def release(self, claim: LedgerClaim, lease_token: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM research_request_ledger WHERE actor=? AND key_hash=? AND method=? AND path=? "
                "AND state='in_progress' AND lease_token=?",
                (*self._identity(claim), lease_token),
            )
