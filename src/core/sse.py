"""Server-sent events.

The transport for streamed model output. The web chat endpoint decides which frames to send — a
``delta`` for each piece of text, then ``done`` or ``error`` — and this module only knows how to
encode a frame and how to send a stream of them without anything in between buffering it.

The request-logging middleware is raw ASGI precisely so it does not buffer these responses (see
:mod:`src.core.middleware`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect
from starlette.types import Receive, Scope, Send

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Nginx buffers proxied responses by default, which would defeat streaming entirely.
    "X-Accel-Buffering": "no",
}


def format_event(data: str, event: str | None = None) -> str:
    """Encode one SSE frame. Every line of the payload needs its own `data:` prefix."""
    lines = [f"event: {event}"] if event else []
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"


def format_json_event(payload: Any, event: str | None = None) -> str:
    return format_event(json.dumps(payload, separators=(",", ":")), event=event)


class EventStreamResponse(StreamingResponse):
    """A stream whose reader may leave at any moment — which, for a chat widget, is not an error.

    Servers on ASGI spec 2.4 report a vanished client by raising ``ClientDisconnect`` out of the
    response; older ones cancel the stream quietly. For an event stream the first is how an ordinary
    visit ends, somebody closing a tab, and letting it propagate would roll back the request's
    transaction and the message that visitor sent with it. Both now end the same quiet way.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except ClientDisconnect:
            # Nothing will read another frame, so the frame source is closed now rather than left
            # suspended for the garbage collector.
            close = getattr(self.body_iterator, "aclose", None)
            if close is not None:
                await close()


def sse_response(
    frames: AsyncIterator[str], headers: dict[str, str] | None = None
) -> StreamingResponse:
    """Send already-encoded frames (see :func:`format_event`) as ``text/event-stream``.

    ``headers`` carries anything the caller needs before the first frame — the conversation id, so
    a client can attach the stream to the right thread without parsing the body."""
    return EventStreamResponse(
        frames,
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **(headers or {})},
    )
