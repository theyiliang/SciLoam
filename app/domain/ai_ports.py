"""Provider-neutral ports and immutable descriptions for read-only research AI."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import threading
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Protocol

from app.domain.errors import DomainError


class AnswerCommitGuard:
    """Cancellation and the durable COMMIT have one linearization point.

    The validation callback uses the already-open repository, never another DB
    connection or an event-loop callback while the write transaction is held.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self.cancelled = threading.Event()
        self.committed = False
        self.result: dict | None = None
        self.validate: Callable[[], None] | None = None

    def check(self) -> None:
        if self.cancelled.is_set():
            raise DomainError(409, "研究运行已取消")

    def cancel(self) -> bool:
        with self._lock:
            if self.committed:
                return False
            self.cancelled.set()
            return True

    @contextmanager
    def commit_scope(self):
        with self._lock:
            self.check()
            if self.validate is not None:
                self.validate()
            yield
            self.committed = True


@dataclass(frozen=True)
class SynchronousRunContext:
    """Thread-propagated checks/events at the actual synchronous adapter stages."""
    check: Callable[[], None]
    emit: Callable[[dict], None]


synchronous_run_context: ContextVar[SynchronousRunContext | None] = ContextVar("research_run_context", default=None)
answer_commit_guard: ContextVar[AnswerCommitGuard | None] = ContextVar("answer_commit_guard", default=None)


class ResearchService(Protocol):
    """Authorization remains owned by the service; adapters may be sync or async."""
    def get_conversation(self, actor: str, conversation: str) -> Any: ...
    def get_version(self, actor: str, version: str) -> Any: ...
    def search(self, actor: str, space: str, query: str, *, limit: int) -> Any: ...
    def list_goals(self, actor: str) -> Any: ...
    def list_tasks(self, actor: str) -> Any: ...  # Legacy adapters only.
    def append_message(self, actor: str, conversation: str, role: str,
                       content: str, citations: list[dict]) -> Any: ...


@dataclass(frozen=True)
class ToolDescription:
    name: str
    description: str
    invoke: Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class AgentDescription:
    name: str
    instructions: str
    tools: tuple[ToolDescription, ...]


class ResearchRuntime(Protocol):
    """Request-local agents must reauthorize before every upstream attempt."""
    async def run_specialist(self, agent: AgentDescription, payload: Mapping[str, Any],
                             *, check: Callable[[], Awaitable[None]], state: Any) -> str: ...
    async def run_coordinator(self, agent: AgentDescription, payload: Mapping[str, Any],
                              *, check: Callable[[], Awaitable[None]], state: Any) -> str: ...
    def new_request_state(self) -> Any: ...


class StreamingResearchRuntime(Protocol):
    """Optional capability: deltas are actual provider output, never sliced finals.

    Raw JSON is untrusted and may only be shown after application claim validation.
    Implementations reauthorize each upstream chunk and preserve request budgets.
    """
    def stream_coordinator(self, agent: AgentDescription, payload: Mapping[str, Any],
                           *, check: Callable[[], Awaitable[None]], state: Any) -> AsyncIterator[str]: ...
