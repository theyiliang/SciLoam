"""Opt-in idempotency for short, authenticated research writes.

Only metadata and a response digest are persisted. The API's replay authorizer
must check live ACLs/epochs and reconstruct the original response from resources.
If the resource changed, replay fails closed rather than returning new data.

This is NOT crash exactly-once: the business write and ledger completion have no
shared UoW. A crash (including after a business commit) leaves an unknown pending
outcome that requires manual reconciliation, never automatic execution. A raised
operation exception releases its claim; callers MUST roll back business effects
before raising. Failures AFTER operation returns do NOT release the claim.
Auth/invites/login and chat/long LLM calls are intentionally excluded in v1.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from app.domain.engineering_ports import (
    AuthorizationScope, EngineeringClock, EngineeringConflict, IdempotencyResult,
    LedgerClaim, OperationResult, ReplayAuthorizer, RequestLedger, RequestOperation,
)

_ALLOWED_CATEGORIES = frozenset({"document", "share", "review", "task", "progress", "conversation_create"})
_SENSITIVE_SEGMENTS = frozenset({
    "auth", "invite", "invites", "login", "logout", "register", "chat", "messages",
    "token", "tokens", "password", "sessions", "me",
})


def canonical_hash(value: Any) -> str:
    """Strict JSON: callers include every effect-affecting body/query parameter."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SystemEngineeringClock:
    def now(self) -> float:
        return time.time()


class RequestIdempotency:
    def __init__(
        self, ledger: RequestLedger, *, authorize: ReplayAuthorizer,
        clock: EngineeringClock | None = None, ttl_seconds: float = 86400,
        pending_ttl_seconds: float = 300,
    ) -> None:
        if not callable(authorize):
            raise ValueError("A live replay authorization callback is required")
        if not all(math.isfinite(v) and v > 0 for v in (ttl_seconds, pending_ttl_seconds)):
            raise ValueError("TTLs must be finite and positive")
        self._ledger = ledger
        self._authorize = authorize
        self._clock = clock or SystemEngineeringClock()
        self._ttl = ttl_seconds
        self._pending_ttl = pending_ttl_seconds

    def execute(
        self, actor: str, key: str, operation: RequestOperation,
        request_payload: Any, scope: AuthorizationScope,
        operation_fn: Callable[[], OperationResult],
    ) -> IdempotencyResult:
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("An authenticated actor is required")
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            raise ValueError("Idempotency-Key must contain 1..256 characters")
        epochs = (scope.expected_auth_epoch, scope.current_auth_epoch)
        if (
            not isinstance(scope.fingerprint, str) or not scope.fingerprint.strip()
            or any(type(epoch) not in (str, int) or not str(epoch).strip() for epoch in epochs)
        ):
            raise ValueError("Scope fingerprint and expected/current authorization epochs are required")
        if str(scope.expected_auth_epoch) != str(scope.current_auth_epoch):
            raise EngineeringConflict("authorization_changed", "Authorization epoch changed; refresh authorization")
        method = operation.method.upper()
        parsed = urlsplit(operation.path)
        segments = frozenset(unquote(parsed.path).lower().split("/"))
        if (
            operation.category not in _ALLOWED_CATEGORIES
            or method not in {"POST", "PUT", "PATCH", "DELETE"}
            or not operation.path.startswith("/") or parsed.netloc or parsed.query or parsed.fragment
            or segments & _SENSITIVE_SEGMENTS
        ):
            raise ValueError("Operation is excluded from response idempotency caching")
        now = self._clock.now()
        claim = LedgerClaim(
            actor=actor, key=hashlib.sha256(key.encode("utf-8")).hexdigest(),
            method=method, path=operation.path,
            scope_fingerprint=scope.fingerprint, auth_epoch=str(scope.current_auth_epoch),
            request_hash=canonical_hash({"category": operation.category, "payload": request_payload}),
            now=now, pending_expires_at=now + self._pending_ttl,
        )
        reservation = self._ledger.claim(claim)
        if reservation.metadata is not None:
            # Authorizer must re-fetch the current epoch, not trust the caller's
            # earlier snapshot. This is also required for deletion-style receipts.
            response = self._authorize(actor, scope, reservation.metadata)
            if canonical_hash(response) != reservation.response_hash:
                raise EngineeringConflict("response_changed", "Original response cannot be safely reconstructed")
            return IdempotencyResult(response, reservation.metadata.status_code, True)
        if reservation.lease_token is None:
            raise RuntimeError("Ledger returned neither replay nor reservation")
        try:
            result = operation_fn()
        except BaseException:
            # The operation contract requires rollback before an exception escapes.
            self._ledger.release(claim, reservation.lease_token)
            raise
        # Leave pending on invalid output, serialization or completion failures:
        # operation may already have committed its business side effects.
        if not isinstance(result, OperationResult):
            raise TypeError("Operation must return OperationResult")
        if not 200 <= result.metadata.status_code < 300 or not result.metadata.resources:
            raise ValueError("Successful response metadata must reference authorized resources")
        self._ledger.complete(
            claim, reservation.lease_token, result.metadata,
            canonical_hash(result.response), self._clock.now() + self._ttl,
        )
        return IdempotencyResult(result.response, result.metadata.status_code, False)
