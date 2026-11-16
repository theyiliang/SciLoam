"""Bounded goal-only replay, using the existing channel infrastructure."""
import asyncio
import json
from datetime import datetime, timezone

from app.domain.goal_generation_runs import EVENT_TYPES, TERMINAL_EVENTS, GoalGenerationEvent
from app.domain.run_events import RunBackpressure, RunReplayExpired
from .run_bus import InMemoryRunBus


class GoalGenerationBus(InMemoryRunBus):
    async def publish(self, run_id, event_type, data):
        if event_type not in EVENT_TYPES:
            raise ValueError('Unsupported goal event')
        channel = self._channels[run_id]
        if channel.closed:
            raise RuntimeError('Goal run already completed')
        body = json.loads(json.dumps(dict(data), ensure_ascii=False))
        size = len(json.dumps(body, ensure_ascii=False))
        if size > self.max_content_chars:
            raise RunBackpressure('Goal event exceeds content limit')
        terminal = event_type in TERMINAL_EVENTS
        if any(queue.full() for queue in channel.subscribers):
            if not terminal:
                raise RunBackpressure('Goal subscriber queue full')
            for queue in channel.subscribers:
                while not queue.empty():
                    queue.get_nowait()
        channel.seq += 1
        event = GoalGenerationEvent(run_id, channel.seq, datetime.now(timezone.utc).isoformat(),
                                    event_type, channel.actor, '', '', channel.request_id, body)
        channel.events.append((event, size))
        channel.chars += size
        while len(channel.events) > self.ring_size or channel.chars > self.max_content_chars:
            _, removed = channel.events.popleft()
            channel.chars -= removed
        for queue in channel.subscribers:
            queue.put_nowait(event)
        channel.closed = terminal
        return event

    async def listen(self, run_id, after_seq=0):
        channel = self._channels[run_id]
        if after_seq < 0 or after_seq > channel.seq:
            raise RunReplayExpired('Invalid goal cursor')
        oldest = channel.events[0][0].seq if channel.events else channel.seq + 1
        if after_seq < oldest - 1:
            if channel.closed and channel.events and channel.events[-1][0].type in TERMINAL_EVENTS:
                yield channel.events[-1][0]
                return
            raise RunReplayExpired('Goal cursor expired; retrieve run instead')
        if len(channel.subscribers) >= self.max_subscribers:
            raise RunBackpressure('Goal subscriber limit reached')
        queue = asyncio.Queue(self.queue_size)
        replay = [event for event, _ in channel.events if event.seq > after_seq]
        generation, closed = channel.generation, channel.closed
        if not closed:
            channel.subscribers.add(queue)
        try:
            for event in replay:
                if channel.generation != generation:
                    break
                yield event
            if closed:
                if channel.generation != generation:
                    for event, _ in channel.events:
                        if event.type in TERMINAL_EVENTS:
                            yield event
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), self.heartbeat_seconds)
                except TimeoutError:
                    yield None
                    continue
                yield event
                if event.type in TERMINAL_EVENTS:
                    return
        finally:
            channel.subscribers.discard(queue)
