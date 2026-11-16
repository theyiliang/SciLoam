"""SSE wire formatting lives exclusively at the presentation boundary."""
from __future__ import annotations

import json
from typing import AsyncIterator, Any

from app.domain.run_events import RunEvent


def encode_sse(event: RunEvent) -> bytes:
    data = json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n".encode("utf-8")


async def stream_sse(manager: Any, actor: str, run_id: str,
                     after_seq: int = 0, *, authorize_actor=None) -> AsyncIterator[bytes]:
    # Actor must be resolved from server authentication/cookies, never event data.
    reader = manager.subscribe(actor, run_id, after_seq, authorize_actor=authorize_actor, include_heartbeats=True)
    try:
        async for event in reader:
            yield b": heartbeat\n\n" if event is None else encode_sse(event)
    finally:
        await reader.aclose()
