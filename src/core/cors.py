"""Cross-origin policy — two of them, chosen by path (spec §5.6, §5.7).

The console and the public chat API face opposite audiences, and one policy cannot serve both:

* **The console** is a known set of the platform's own frontends, configured by whoever runs it
  (``CORS_ALLOW_ORIGINS``), and it is allowed credentials.
* **The public chat API** is embedded in tenants' own sites, which the platform cannot list in
  advance — a new tenant site must not wait for an operator to edit an environment variable and
  redeploy. It carries an API key in a header, never a cookie, so any origin may call it without
  credentials. *Which* origins a tenant accepts is the tenant's call, made per agent through the web
  channel's allowlist and enforced after the key is known.

Both expose the response headers a browser client actually needs. Without ``expose_headers`` a
cross-origin script sees none of them: not the conversation id on a stream, not the remaining rate
limit, not ``Retry-After`` on a 429, and not the request id to quote when something breaks.
"""

from __future__ import annotations

from collections.abc import Sequence

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

PUBLIC_CHAT_PREFIX = "/v1/chat"

EXPOSED_HEADERS = [
    "X-Request-ID",
    "X-Conversation-Id",
    "X-RateLimit-Limit",
    "X-RateLimit-Remaining",
    "X-RateLimit-Reset",
    "Retry-After",
]

PUBLIC_CHAT_REQUEST_HEADERS = [
    "Authorization",
    "X-API-Key",
    "Content-Type",
    "Accept",
    "X-Request-ID",
]


def is_public_chat(path: str) -> bool:
    return path == PUBLIC_CHAT_PREFIX or path.startswith(f"{PUBLIC_CHAT_PREFIX}/")


class PathScopedCORSMiddleware:
    """The console's policy everywhere, except under ``/v1/chat``."""

    def __init__(self, app: ASGIApp, *, console_origins: Sequence[str]) -> None:
        self._console = CORSMiddleware(
            app,
            allow_origins=list(console_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=EXPOSED_HEADERS,
        )
        self._public_chat = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=PUBLIC_CHAT_REQUEST_HEADERS,
            expose_headers=EXPOSED_HEADERS,
            max_age=600,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_public_chat(str(scope.get("path", ""))):
            await self._public_chat(scope, receive, send)
            return
        await self._console(scope, receive, send)
