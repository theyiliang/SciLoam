"""Transport-neutral contracts for research request safety.

The API authenticates actors and computes authorization scope/epochs. These ports
never authenticate a user and must not be exposed directly to untrusted callers.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


class EngineeringConflict(Exception):
    status_code = 409

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class AuthorizationScope:
    fingerprint: str
    expected_auth_epoch: str | int
    current_auth_epoch: str | int


@dataclass(frozen=True)
class RequestOperation:
    method: str
    path: str
    category: Literal["document", "share", "review", "task", "progress", "conversation_create"]


@dataclass(frozen=True)
class ResourceReference:
    resource_type: str
    resource_id: str


@dataclass(frozen=True)
class ResponseMetadata:
    status_code: int
    resources: tuple[ResourceReference, ...]


@dataclass(frozen=True)
class OperationResult:
    response: Any
    metadata: ResponseMetadata


@dataclass(frozen=True)
class IdempotencyResult:
    response: Any
    status_code: int
    replayed: bool


@dataclass(frozen=True)
class LedgerClaim:
    actor: str
    key: str
    method: str
    path: str
    scope_fingerprint: str
    auth_epoch: str
    request_hash: str
    now: float
    pending_expires_at: float


@dataclass(frozen=True)
class LedgerReservation:
    """Exactly one of lease_token (new) and metadata (replay) is populated."""
    lease_token: str | None = None
    metadata: ResponseMetadata | None = None
    response_hash: str | None = None


class EngineeringClock(Protocol):
    def now(self) -> float: ...


class RequestLedger(Protocol):
    """claim/complete/release each use an atomic database transaction.

    Expired pending reservations MUST remain blocked as unknown outcomes. Only
    completed rows may expire automatically. complete/release are owner-fenced.
    """

    def claim(self, claim: LedgerClaim) -> LedgerReservation: ...

    def complete(
        self, claim: LedgerClaim, lease_token: str, metadata: ResponseMetadata,
        response_hash: str, expires_at: float,
    ) -> None: ...

    def release(self, claim: LedgerClaim, lease_token: str) -> None: ...


class ReplayAuthorizer(Protocol):
    def __call__(
        self, actor: str, scope: AuthorizationScope, metadata: ResponseMetadata,
    ) -> Any:
        """Recheck CURRENT scope/epoch and each resource ACL; reconstruct response.

        Raise on denied/deleted resources or changed scope. Return only after all
        resources are authorized. Do not return persisted/private cache bodies.
        """
        ...

