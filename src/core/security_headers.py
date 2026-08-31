"""Response headers that harden every browser-facing surface (spec §5.7, Phase 13).

Raw ASGI, like the request-context middleware beside it, and for the same reason:
``BaseHTTPMiddleware`` buffers the response and would break the chat endpoint's server-sent events.

The set is small on purpose — each header here answers a threat this API actually has.

* ``X-Content-Type-Options: nosniff`` — the chat API returns tenant-authored text, and a browser
  that sniffs a JSON response as HTML is one reflected-XSS away from a problem.
* ``X-Frame-Options`` and ``frame-ancestors`` — nothing here should be framed. The web widget is a
  tenant's own page calling the API, not our page embedded in theirs.
* ``Referrer-Policy: strict-origin-when-cross-origin`` — API paths carry agent and conversation ids;
  those should not leak to third parties through the ``Referer`` of an outbound click.
* ``Cross-Origin-Opener-Policy`` and ``Cross-Origin-Resource-Policy`` — a cross-origin page must not
  be able to hold a handle to a window of ours, or read a response by embedding it.
* ``Strict-Transport-Security`` — sent only over HTTPS. Sent on a plain-HTTP response it is ignored
  by browsers and merely misleading to anyone reading the traffic.
* A **content security policy**, sent only on the documentation pages. `/docs` and `/redoc` are the
  only HTML this service serves, and they load Swagger UI and ReDoc from a CDN, so the policy names
  exactly those origins. Applying an API-shaped CSP to a JSON response would be noise; applying a
  document-shaped one everywhere would eventually break the docs.

Permissions-Policy is deliberately absent: it governs browser features on *our* pages, and the only
pages we serve are the docs, which use none of them.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src import configs

# Sent on every response.
BASE_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-site",
}

# Swagger UI and ReDoc are served from jsDelivr by FastAPI's defaults, and both need their own
# inline styles. Swagger UI additionally *bootstraps itself from an inline script*: FastAPI's
# template calls `SwaggerUIBundle({...})` in a `<script>` block with no src, so `script-src` has to
# allow inline here or `/docs` renders blank — the bundle loads from the CDN, nothing ever calls
# it, and the mount point stays empty. ReDoc is unaffected; it boots from a `<redoc>` element.
#
# 'unsafe-inline' is confined to this policy, and this policy is only ever sent on the
# documentation paths. Those pages are static FastAPI-generated HTML carrying no tenant content,
# so there is no injection sink on them; an API response still gets no CSP at all.
#
# Otherwise narrow to the origins actually used rather than reaching for 'unsafe-inline'
# everywhere — a policy that allows everything is a policy that documents nothing.
DOCS_CSP = (
    "default-src 'self'; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


class SecurityHeadersMiddleware:
    """Adds the headers above to every response, without touching the body."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.hsts = f"max-age={configs.SECURITY_HSTS_MAX_AGE_SECONDS}; includeSubDomains"
        self.docs_paths = {
            configs.DOCS_SWAGGER_PATH,
            configs.DOCS_REDOC_PATH,
            f"{configs.DOCS_SWAGGER_PATH}/oauth2-redirect",
        }

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # `scope["scheme"]` is what the ASGI server resolved, including from a trusted proxy's
        # X-Forwarded-Proto when uvicorn is run with --proxy-headers. Behind a terminating load
        # balancer without that flag it reads "http", and HSTS is then correctly not sent rather
        # than sent and ignored.
        secure = scope.get("scheme") == "https"

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {name.lower() for name, _ in headers}

                for name, value in BASE_HEADERS.items():
                    if name.encode() not in existing:
                        headers.append((name.encode(), value.encode()))

                if secure:
                    headers.append((b"strict-transport-security", self.hsts.encode()))

                if path in self.docs_paths:
                    headers.append((b"content-security-policy", DOCS_CSP.encode()))

            await send(message)

        await self.app(scope, receive, send_wrapper)
