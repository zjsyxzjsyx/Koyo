"""`GET /api/chat/stream`: the workbench's events as Server-Sent Events.

Each event is sent with its seq as the SSE id, so a page that reconnects with Last-Event-ID
resumes right after the last event it saw. The worker thread emits; this generator waits on
WebIO's condition in a worker thread of its own, a few seconds at a time, so it notices both new
events and a closing server, and leaves when the client disconnects.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import anyio

from ..redact import redact_obj

# Seconds between keep-alive comments while nothing happens.
PING_S = 15.0
# The longest one wait for new events lasts before the generator looks around again.
WAIT_S = 5.0


def frame(kind: str, data: dict[str, Any], seq: int | None = None) -> bytes:
    body = json.dumps(redact_obj(data), ensure_ascii=False, default=str)
    head = (f"id: {seq}\n" if seq is not None else "") + f"event: {kind}\n"
    # json.dumps escapes newlines, so the data is one line.
    return (head + f"data: {body}\n\n").encode("utf-8")


def start_after(last_event_id: str | None, after: str | None) -> int:
    """Where a stream starts: the Last-Event-ID a reconnecting page sends, else ?after=, else 0."""
    for raw in (last_event_id, after):
        if raw:
            try:
                return max(int(raw), 0)
            except ValueError:
                continue
    return 0


class _Open:
    """How many streams are being served now; a disconnected client must bring it back down."""
    count = 0


def open_streams() -> int:
    return _Open.count


async def events(server: Any, after: int) -> AsyncIterator[bytes]:
    _Open.count += 1
    try:
        async for chunk in _events(server, after):
            yield chunk
    finally:
        _Open.count -= 1


async def _events(server: Any, after: int) -> AsyncIterator[bytes]:
    bench = server.workbench
    io = bench.io
    yield frame("hello", {"busy": bench.busy, "pending": io.pending(), "next": io.last_seq,
                          "conversation_id": bench.conversation_id})
    if io.lost(after):
        # Older events were dropped from memory: the page should reload history from the database.
        yield frame("gap", {"after": after})
    idle = 0.0
    while not server.closing and not io.closed:
        batch = io.since_switch(after)
        if batch:
            for event in batch:
                yield frame(event["kind"], event, event["seq"])
                after = max(after, event["seq"])
            # What the polling endpoint answered alongside its events: whether a turn runs, what waits.
            yield frame("state", {"busy": bench.busy, "pending": io.pending()})
            idle = 0.0
            continue
        if await anyio.to_thread.run_sync(io.wait_after, after, WAIT_S, abandon_on_cancel=True):
            continue
        idle += WAIT_S
        if idle >= PING_S:
            yield b": ping\n\n"
            idle = 0.0
