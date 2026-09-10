"""SSE framing — the transport the streaming chat endpoint uses."""

from __future__ import annotations

from collections.abc import AsyncIterator

from starlette.requests import ClientDisconnect
from starlette.types import Message

from src.core.sse import format_event, format_json_event, sse_response


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


async def test_the_response_sends_the_frames_it_is_given_unchanged() -> None:
    async def frames() -> AsyncIterator[str]:
        yield format_json_event({"delta": "Hel"}, event="delta")
        yield format_json_event({"done": True}, event="done")

    response = sse_response(frames())

    sent = [chunk async for chunk in response.body_iterator]
    assert sent == [
        'event: delta\ndata: {"delta":"Hel"}\n\n',
        'event: done\ndata: {"done":true}\n\n',
    ]


def test_the_response_disables_proxy_buffering() -> None:
    async def frames() -> AsyncIterator[str]:
        yield format_event("hi")

    response = sse_response(frames())

    assert response.media_type == "text/event-stream"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"


async def test_a_reader_leaving_mid_stream_is_not_an_error() -> None:
    """On an ASGI 2.4 server a closed tab arrives as ``ClientDisconnect`` out of the response.

    Propagating it would fail the request and roll back its transaction, so the response ends
    quietly instead — the same way it already ends on older servers, which cancel the stream.
    """

    closed = False

    async def frames() -> AsyncIterator[str]:
        nonlocal closed
        try:
            yield format_event("one")
            yield format_event("two")
        finally:
            closed = True

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("the client went away")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}}

    # Starlette turns the OSError into ClientDisconnect. Were it not swallowed, this would raise.
    try:
        await sse_response(frames())(scope, receive, send)
    except ClientDisconnect:  # pragma: no cover - the failure this test exists to catch
        raise AssertionError("a reader leaving propagated out of the response") from None

    assert closed, "the frame source is closed rather than left suspended"
