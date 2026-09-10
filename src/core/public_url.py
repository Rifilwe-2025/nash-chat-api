"""The origin this API is reachable at from outside.

Used wherever a URL has to be written down for somebody else to use later: the generated
integration guide, and the WhatsApp callback a tenant pastes into Meta. Behind a proxy the request's
own origin is the internal one — a guide that tells a developer to POST to ``http://api:8000`` is
worse than no guide, and a PDF carrying that URL is worse still, because it outlives the request
that produced it.
"""

from __future__ import annotations

from fastapi import Request

from src import configs


def public_base_url(request: Request) -> str:
    """``PUBLIC_BASE_URL`` when it is configured, falling back to the request's own origin."""
    configured = str(configs.APP_PUBLIC_BASE_URL or "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")
