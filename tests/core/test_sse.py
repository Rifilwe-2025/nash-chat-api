"""SSE framing — the transport Phase 8's streaming chat endpoint will use."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from src.core.sse import format_event, format_json_event, sse_response, text_event_stream


def test_a_frame_ends_with_a_blank_line() -> None:
    assert format_event("hello") == "data: hello\n\n"


def test_multiline_payloads_prefix_every_line() -> None:
    """A raw newline inside `data:` would silently truncate the event for the client."""
    assert format_event("one\ntwo") == "data: one\ndata: two\n\n"


def test_named_events_carry_their_type() -> None:
    assert format_event("hi", event="delta").startswith("event: delta\n")


def test_json_frames_are_compact() -> None:
    frame = format_json_event({"delta": "hi"}, event="delta")

    assert frame == 'event: delta\ndata: {"delta":"hi"}\n\n'


async def test_the_stream_ends_with_a_done_event() -> None:
    async def chunks() -> AsyncIterator[str]:
        yield "Hel"
        yield "lo"

    frames = [frame async for frame in text_event_stream(chunks())]

    assert len(frames) == 3
    assert json.loads(frames[0].split("data: ")[1]) == {"delta": "Hel"}
    assert json.loads(frames[2].split("data: ")[1]) == {"done": True}


def test_the_response_disables_proxy_buffering() -> None:
    async def chunks() -> AsyncIterator[str]:
        yield "hi"

    response = sse_response(chunks())

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
