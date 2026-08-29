"""Raw ASGI middleware.

Deliberately not ``BaseHTTPMiddleware``: that buffers responses and breaks the SSE streaming the
chat endpoints need. Every request is tagged with an ``X-Request-ID`` and logged once on completion,
and any rate limit verdict reached during the request is copied onto the response.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("api.request")

REQUEST_ID_HEADER = b"x-request-id"


class RequestContextMiddleware:
    """Assigns a request id, echoes it back, and logs method/path/status/duration."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
        incoming = next((v for k, v in headers if k == REQUEST_ID_HEADER), None)
        request_id = incoming.decode() if incoming else str(uuid.uuid4())

        state: MutableMapping[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"].append((REQUEST_ID_HEADER, request_id.encode()))
                # Rate limit headers are attached here rather than per route: the verdict is
                # reached in a dependency, and every response under a limit should carry it —
                # a client should learn its remaining allowance from a success, not only a 429.
                for name, value in state.get("rate_limit_headers", {}).items():
                    message["headers"].append((name.lower().encode(), str(value).encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "%s %s %s %.1fms",
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_code,
                duration_ms,
                extra={"request_id": request_id},
            )
