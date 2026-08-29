"""Server-sent events.

The transport for streamed model output. Consumed by the web chat endpoint in Phase 8; the
provider adapters produce the text deltas it frames.

The request-logging middleware is raw ASGI precisely so it does not buffer these responses (see
:mod:`src.core.middleware`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

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


async def text_event_stream(chunks: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap text deltas as SSE frames and close with a `done` event."""
    async for chunk in chunks:
        yield format_json_event({"delta": chunk}, event="delta")
    yield format_json_event({"done": True}, event="done")


def sse_response(
    chunks: AsyncIterator[str], headers: dict[str, str] | None = None
) -> StreamingResponse:
    """``headers`` carries anything the caller needs before the first frame — the conversation id,
    so a client can attach the stream to the right thread without parsing the body."""
    return StreamingResponse(
        text_event_stream(chunks),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **(headers or {})},
    )
