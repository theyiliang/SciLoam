"""Provider/transport-neutral contracts for short-lived research runs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol


EVENT_TYPES = frozenset({
    "run.started", "retrieval.started", "retrieval.completed", "agent.started",
    "agent.completed", "answer.delta", "answer.final", "run.failed", "run.cancelled",
    "retrieval.skipped", "retrieval.degraded", "rerank.started", "rerank.completed",
    "rerank.skipped", "rerank.degraded", "run.completed", "document.read",
})
TERMINAL_EVENTS = frozenset({"answer.final", "run.failed", "run.cancelled", "run.completed"})
EventSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class RunEvent:
    run_id: str
    seq: int
    timestamp: str
    type: str
    actor_id: str
    space_id: str
    conversation_id: str
    request_id: str | None
    data: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        # Actor is internal routing metadata, never a client-provided identity.
        return {"run_id": self.run_id, "seq": self.seq, "timestamp": self.timestamp,
                "type": self.type, "space_id": self.space_id,
                "conversation_id": self.conversation_id, "request_id": self.request_id,
                "data": dict(self.data)}


class RunBackpressure(RuntimeError):
    """An active consumer cannot keep up; do not drop answer fragments silently."""


class RunReplayExpired(RuntimeError):
    """Requested cursor is no longer in the bounded replay window."""


class RunBus(Protocol):
    def create(self, run_id: str, *, actor: str, space: str, conversation: str,
               request_id: str | None) -> None: ...
    async def publish(self, run_id: str, event_type: str, data: Mapping[str, Any]) -> RunEvent: ...
    def listen(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunEvent | None]: ...
    def discard(self, run_id: str) -> None: ...
    def purge_content(self, run_id: str) -> None: ...
