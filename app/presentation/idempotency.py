"""Explicit, opt-in HTTP adapter for short research writes.

Routes pass their validated payload and a zero-argument business operation. No
request/response body is stored: replay rebuilds a response through live service
reads and the application ledger verifies its original digest. This is not crash
exactly-once; ambiguous pending outcomes require manual reconciliation.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from app.application.usecases.request_idempotency import RequestIdempotency, canonical_hash
from app.domain.engineering_ports import (
    AuthorizationScope,
    EngineeringConflict,
    OperationResult,
    RequestOperation,
    ResourceReference,
    ResponseMetadata,
)
from app.domain.errors import DomainError


# Exact POST-only allowlist. In particular: no auth/invites/chat, member grants,
# DELETE or PATCH. Space creation uses the ledger's existing document category.
_ROUTES = (
    (r"/api/spaces", "document", "space"),
    (r"/api/spaces/[^/]+/documents", "document", "document"),
    (r"/api/documents/[^/]+/versions", "document", "version"),
    (r"/api/versions/[^/]+/share", "share", "document"),
    (r"/api/reviews", "review", "review"),
    (r"/api/reviews/[^/]+/decision", "review", "review"),
    (r"/api/tasks", "task", "task"),
    (r"/api/tasks/[^/]+/progress", "progress", "progress"),
    (r"/api/conversations", "conversation_create", "conversation"),
    (r"/api/goals", "task", "goal"),
    (r"/api/goals/[^/]+/updates", "progress", "goal_update"),
    (r"/api/goals/[^/]+/knowledge", "document", "goal_knowledge"),
)


def _scope(svc: Any, actor: str) -> tuple[AuthorizationScope, list[dict]]:
    # Authenticate BEFORE inspecting scope or touching the ledger, also on replay.
    user = svc.get_me(actor)["user"]
    spaces = svc.list_spaces(actor)
    fingerprint = canonical_hash({
        "user_role": user["role"],
        "spaces": sorted(
            ((space["id"], space["epoch"], space["role"]) for space in spaces),
            key=lambda entry: entry[0],
        ),
    })
    # The aggregate fingerprint is also the aggregate authorization epoch.
    return AuthorizationScope(fingerprint, fingerprint, fingerprint), spaces


def _metadata(kind: str, response: Any) -> ResponseMetadata:
    # Invalid post-commit output must leave the ledger pending, not raise inside
    # operation_fn (which would release the reservation and allow a duplicate).
    if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not response["id"]:
        return ResponseMetadata(200, ())
    references = [ResourceReference(kind, response["id"])]
    parent = {"document": "space_id", "progress": "task_id", "goal_update": "goal_id", "goal_knowledge": "goal_id"}.get(kind)
    if parent:
        if not isinstance(response.get(parent), str) or not response[parent]:
            return ResponseMetadata(200, ())
        references.append(ResourceReference("space" if kind == "document" else "goal" if kind.startswith("goal_") else "task", response[parent]))
    return ResponseMetadata(200, tuple(references))


def _find(rows: list[dict], resource_id: str) -> dict:
    for row in rows:
        if row.get("id") == resource_id:
            return row
    raise EngineeringConflict("resource_unavailable", "Original response resource is no longer available")


def _rebuild(svc: Any, actor: str, metadata: ResponseMetadata, spaces: list[dict]) -> dict:
    references = metadata.resources
    if not references:
        raise EngineeringConflict("resource_unavailable", "Original response resource is unavailable")
    resource = references[0]
    kind, resource_id = resource.resource_type, resource.resource_id
    if kind == "space":
        return _find(spaces, resource_id)
    if kind == "document":
        if len(references) != 2 or references[1].resource_type != "space":
            raise EngineeringConflict("resource_unavailable", "Document scope reference is unavailable")
        space_id = _find(spaces, references[1].resource_id)["id"]
        return _find(svc.list_documents(actor, space_id), resource_id)
    if kind == "version":
        return svc.get_version(actor, resource_id)
    if kind == "review":
        review = _find(svc.list_reviews(actor), resource_id)
        # list_reviews authorizes the submitted version; authorize the published
        # output as well when replaying a decision receipt.
        if review.get("published_version_id"):
            svc.get_version(actor, review["published_version_id"])
        return review
    if kind == "task":
        task = dict(_find(svc.list_tasks(actor), resource_id))
        # The list API enriches a task, whereas create_task returns the task row.
        task.pop("progress", None)
        return task
    if kind == "progress":
        if len(references) != 2 or references[1].resource_type != "task":
            raise EngineeringConflict("resource_unavailable", "Progress task reference is unavailable")
        task = _find(svc.list_tasks(actor), references[1].resource_id)
        # list_tasks filters entries whose attached versions are not authorized.
        return _find(task.get("progress", []), resource_id)
    if kind == "goal":
        return svc.get_goal(actor, resource_id)
    if kind in ("goal_update", "goal_knowledge"):
        if len(references) != 2 or references[1].resource_type != "goal":
            raise EngineeringConflict("resource_unavailable", "Goal parent reference is unavailable")
        goal = svc.get_goal(actor, references[1].resource_id)
        return _find(goal["updates" if kind == "goal_update" else "knowledge"], resource_id)
    if kind == "conversation":
        return svc.get_conversation(actor, resource_id)
    raise EngineeringConflict("resource_unavailable", "Unsupported original response resource")


def execute_write(
    request: Request,
    actor: str | Mapping[str, Any],
    svc: Any,
    payload: dict,
    operation_fn: Callable[[], dict],
) -> dict | JSONResponse:
    """Run a route write; optionally claim/replay its Idempotency-Key.

    Use ``payload=body.model_dump()`` (or the exact effective business arguments),
    never ``request.body()`` in a synchronous route. Query parameters are included
    automatically. Unkeyed/excluded requests never access the engineering store.
    Engineering conflicts become DomainError(409) for the server's usual handler.
    """
    actor_id = actor.get("id") if isinstance(actor, Mapping) else actor
    # Authenticate even for unkeyed and explicitly excluded operations.
    svc.get_me(actor_id)
    key = request.headers.get("Idempotency-Key")
    route = next((entry for entry in _ROUTES if re.fullmatch(entry[0], request.url.path)), None)
    if key is None or request.method.upper() != "POST" or route is None:
        return operation_fn()
    if not key.strip() or len(key) > 256:
        raise DomainError(422, "请求幂等键长度需在 1 到 256 字符之间")

    def authorize(replay_actor: str, expected: AuthorizationScope, metadata: ResponseMetadata) -> dict:
        try:
            current, spaces = _scope(svc, replay_actor)
            if current != expected:
                raise EngineeringConflict("authorization_changed", "Request authorization scope changed")
            response = _rebuild(svc, replay_actor, metadata, spaces)
            # Close the scope-read/resource-read window conservatively.
            if _scope(svc, replay_actor)[0] != expected:
                raise EngineeringConflict("authorization_changed", "Request authorization scope changed")
            return response
        except DomainError as exc:
            if exc.status_code in (401, 403, 404, 409, 410):
                raise EngineeringConflict("resource_unavailable", "Original response cannot be safely reconstructed") from exc
            raise

    try:
        scope, _ = _scope(svc, actor_id)
        ledger = RequestIdempotency(request.app.state.container.engineering, authorize=authorize)

        def operation() -> OperationResult:
            response = operation_fn()
            return OperationResult(response, _metadata(route[2], response))

        result = ledger.execute(
            actor_id, key, RequestOperation("POST", request.url.path, route[1]),
            {"body": payload, "query": list(request.query_params.multi_items())},
            scope, operation,
        )
    except EngineeringConflict as exc:
        raise DomainError(409, exc.message) from exc
    return JSONResponse(
        json.loads(json.dumps(result.response, ensure_ascii=False, sort_keys=True, allow_nan=False)), status_code=result.status_code,
        headers={"Idempotency-Replayed": "true" if result.replayed else "false"},
    )
