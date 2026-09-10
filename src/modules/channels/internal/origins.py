"""Browser origins, as the web channel's allowlist stores and compares them (spec §5.7).

An origin is a scheme, a host and a port — nothing else. Browsers send it in exactly that shape in
the ``Origin`` header, so an allowlist entry is normalised into the same shape when it is saved:
lower-cased, no trailing slash, no default port. Matching is then an exact comparison, and an entry
cannot admit more than it names — ``https://example.com`` does not admit
``https://example.com.attacker.test``, and there are no wildcards to get wrong.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

#: Where the web channel keeps its allowlist in ``channel_config.settings_json``.
SETTING = "allowedOrigins"

DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidOriginError(ValueError):
    """A value that is not a bare http(s) origin."""


def normalise(value: str) -> str:
    """``HTTPS://Shop.Example.com:443/`` becomes ``https://shop.example.com``."""
    parts = urlsplit(value.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS or not parts.hostname:
        raise InvalidOriginError(value)

    # Anything past the host is not part of an origin. Refused rather than dropped, so nobody comes
    # away believing ``https://example.com/shop`` restricts the agent to a path.
    beyond_host = parts.query or parts.fragment or parts.username or parts.password
    if parts.path not in ("", "/") or beyond_host:
        raise InvalidOriginError(value)

    try:
        port = parts.port
    except ValueError as exc:
        raise InvalidOriginError(value) from exc

    host = parts.hostname.lower()
    if ":" in host:
        # An IPv6 literal, which urlsplit hands back without the brackets the header carries.
        host = f"[{host}]"
    suffix = f":{port}" if port is not None and port != DEFAULT_PORTS[scheme] else ""
    return f"{scheme}://{host}{suffix}"


def configured(settings: dict[str, Any] | None) -> list[str]:
    """The allowlist stored on a web channel's settings — empty when there is none."""
    raw = (settings or {}).get(SETTING)
    return [str(entry) for entry in raw] if isinstance(raw, list) else []


def is_allowed(origin: str, allowed: Iterable[str]) -> bool:
    """Whether a request's ``Origin`` is listed. One that is not an origin at all — the literal
    ``null`` a sandboxed frame sends — never is."""
    try:
        candidate = normalise(origin)
    except InvalidOriginError:
        return False

    for entry in allowed:
        try:
            if normalise(entry) == candidate:
                return True
        except InvalidOriginError:
            continue
    return False
