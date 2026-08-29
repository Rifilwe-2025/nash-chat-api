"""Signing and delivering outbound webhooks (spec §5.6).

Deliveries are **best effort and out of band**. A customer's message must not fail, or even wait,
because a tenant's webhook receiver is slow — so a delivery is fired without blocking the turn and
its failures are recorded rather than raised. Durable retry belongs on the queue, which is Phase 9;
this phase does configuration, signing, and one attempt.

Every delivery is **signed** so the receiver can tell a real event from anyone who guessed the URL.
The signature covers a timestamp as well as the body, and the timestamp is compared by the receiver,
because a signature over the body alone is replayable forever. The scheme is deliberately the
familiar one (``t=<unix>,v1=<hex hmac>``) — a tenant integrating this should recognise it.

Comparisons use :func:`hmac.compare_digest`; a plain ``==`` on a signature leaks its prefix through
timing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any

import httpx

from src import configs

logger = logging.getLogger("api.webhooks")

SECRET_BYTES = 32


def generate_secret() -> str:
    """A signing secret, shown to the tenant so they can verify deliveries."""
    return f"whsec_{secrets.token_urlsafe(SECRET_BYTES)}"


def signature_for(secret: str, payload: str, timestamp: int) -> str:
    """``t=<unix>,v1=<hex>`` over ``<timestamp>.<body>``."""
    signed = f"{timestamp}.{payload}".encode()
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(secret: str, payload: str, header: str, tolerance_seconds: int = 300) -> bool:
    """The check a receiver performs. Implemented here so the docs can show working code.

    Rejects a signature older than ``tolerance_seconds`` even when it verifies: that is what stops
    a captured delivery being replayed indefinitely.
    """
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    raw_timestamp, provided = parts.get("t"), parts.get("v1")
    if not raw_timestamp or not provided:
        return False

    try:
        timestamp = int(raw_timestamp)
    except ValueError:
        return False

    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        return False

    expected = signature_for(secret, payload, timestamp).split("v1=", 1)[1]
    return hmac.compare_digest(expected, provided)


def build_payload(event: str, data: dict[str, Any]) -> str:
    """Serialised once, so the bytes that are signed are exactly the bytes that are sent."""
    return json.dumps(
        {"event": event, "sentAt": int(time.time()), "data": data},
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


async def deliver(
    url: str,
    secret: str,
    payload: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[bool, str | None]:
    """One attempt. Returns ``(delivered, error)`` and never raises.

    Failures are values rather than exceptions because the caller is a fire-and-forget task on the
    conversation path: there is nobody to catch an exception, and a tenant's broken endpoint is a
    thing to record, not an incident in our own request.
    """
    timestamp = int(time.time())
    headers = {
        "Content-Type": "application/json",
        configs.WEBHOOKS_SIGNATURE_HEADER: signature_for(secret, payload, timestamp),
    }

    owned = client is None
    http = client or httpx.AsyncClient(timeout=configs.WEBHOOKS_TIMEOUT_SECONDS)
    try:
        response = await http.post(url, content=payload, headers=headers)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, None
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    finally:
        if owned:
            await http.aclose()
