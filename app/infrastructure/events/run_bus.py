"""Bounded in-process replay and subscriber queues; never logs event bodies."""
from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping

from app.domain.run_events import (EVENT_TYPES, TERMINAL_EVENTS, RunBackpressure,
                                   RunEvent, RunReplayExpired)


@dataclass
class _Channel:
    actor: str
    space: str
    conversation: str
    request_id: str | None
    events: deque = field(default_factory=deque)
    subscribers: set = field(default_factory=set)
    seq: int = 0
    chars: int = 0
    closed: bool = False
    generation: int = 0


class InMemoryRunBus:
    def __init__(self, *, ring_size: int = 128, queue_size: int = 32,
                 max_content_chars: int = 100000, heartbeat_seconds: float = .25,
                 max_subscribers: int = 4):
        if min(ring_size, queue_size, max_content_chars, max_subscribers) < 1 or heartbeat_seconds <= 0:
            raise ValueError("Bus bounds must be positive")
        self.ring_size, self.queue_size = ring_size, queue_size
        self.max_content_chars, self.heartbeat_seconds = max_content_chars, heartbeat_seconds
        self.max_subscribers = max_subscribers
        self._channels: dict[str, _Channel] = {}

    def create(self, run_id: str, *, actor: str, space: str, conversation: str,
               request_id: str | None) -> None:
        if run_id in self._channels:
            raise ValueError("Run already exists")
        self._channels[run_id] = _Channel(actor, space, conversation, request_id)

    async def publish(self, run_id: str, event_type: str, data: Mapping[str, Any]) -> RunEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError("Unsupported run event")
        channel = self._channels[run_id]
        if channel.closed:
            raise RuntimeError("Run already completed")
        # Snapshot caller-owned containers; later mutation must not alter replay.
        body = json.loads(json.dumps(dict(data), ensure_ascii=False))
        size = len(json.dumps(body, ensure_ascii=False))
        if size > self.max_content_chars:
            raise RunBackpressure("Event exceeds retained content limit")
        terminal = event_type in TERMINAL_EVENTS
        if any(queue.full() for queue in channel.subscribers):
            if not terminal:
                raise RunBackpressure("Subscriber queue is full")
            # Terminal replacement wins over queued tentative content.
            for queue in channel.subscribers:
                while not queue.empty():
                    queue.get_nowait()
        channel.seq += 1
        event = RunEvent(run_id, channel.seq, datetime.now(timezone.utc).isoformat(),
                         event_type, channel.actor, channel.space, channel.conversation,
                         channel.request_id, body)
        channel.events.append((event, size))
        channel.chars += size
        while len(channel.events) > self.ring_size or channel.chars > self.max_content_chars:
            _, removed = channel.events.popleft()
            channel.chars -= removed
        for queue in channel.subscribers:
            queue.put_nowait(event)
        channel.closed = terminal
        return event

    async def listen(self, run_id: str, after_seq: int = 0) -> AsyncIterator[RunEvent | None]:
        channel = self._channels[run_id]
        if after_seq < 0 or after_seq > channel.seq:
            raise RunReplayExpired("Invalid event cursor")
        oldest = channel.events[0][0].seq if channel.events else channel.seq + 1
        if after_seq < oldest - 1:
            # A canonical final is sufficient to recover from an expired delta.
            if not channel.closed or not channel.events or channel.events[-1][0].type != "answer.final":
                raise RunReplayExpired("Event cursor expired")
            yield channel.events[-1][0]
            return
        if len(channel.subscribers) >= self.max_subscribers:
            raise RunBackpressure("Subscriber limit reached")
        queue: asyncio.Queue = asyncio.Queue(self.queue_size)
        replay = [event for event, _ in channel.events if event.seq > after_seq]
        generation = channel.generation
        already_closed = channel.closed
        if not already_closed:
            channel.subscribers.add(queue)
        try:
            for event in replay:
                if channel.generation != generation:
                    break
                yield event
            if already_closed and channel.generation != generation:
                for event, _ in channel.events:
                    if event.type in TERMINAL_EVENTS:
                        yield event
                return
            if already_closed:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), self.heartbeat_seconds)
                except TimeoutError:
                    yield None  # Allows application-layer current ACL rechecks.
                    continue
                yield event
                if event.type in TERMINAL_EVENTS:
                    return
        finally:
            channel.subscribers.discard(queue)

    def purge_content(self, run_id: str) -> None:
        channel = self._channels.get(run_id)
        if channel is None:
            return
        channel.generation += 1
        channel.events.clear()
        channel.chars = 0
        for queue in channel.subscribers:
            while not queue.empty():
                queue.get_nowait()

    def discard(self, run_id: str) -> None:
        self.purge_content(run_id)
        self._channels.pop(run_id, None)
