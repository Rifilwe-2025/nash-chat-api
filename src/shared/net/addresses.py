"""Deciding whether an outbound URL is safe to request (spec §5.2.1, §5.7).

Two features now make requests to addresses a tenant supplied: knowledge-base URL ingestion
(Phase 5) and agent tools (Phase 11). Both are the same SSRF surface — a URL we fetch on someone
else's behalf can name ``http://169.254.169.254/`` for cloud metadata or ``http://localhost:6379/``
for Redis, and reach things from inside the network that the caller could never reach from outside.

The rule those two share lives here rather than in either module: ``internal/`` is module-private,
so the alternative was a second copy of the private-range list, and a security check with two copies
is a security check that will eventually disagree with itself. What is *not* here is anything either
feature knows — size caps, allowlists, redirect policy, and the exception type each raises stay with
the feature, because those genuinely differ.

**Every resolved address must be public, not just the first.** A hostname can answer with several
records, and which one a connection uses is not ours to choose — one private answer is enough to
reach an internal service, so one private answer refuses the URL.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeUrlError(Exception):
    """A URL that must not be requested. Callers translate it into their own error type."""


def is_public_address(address: str) -> bool:
    """Whether one resolved IP is routable on the public internet.

    Deliberately a denylist of the special ranges rather than an allowlist of public ones: the
    special ranges are enumerable and stable, whereas "everything else" is the thing that keeps
    working as the internet changes.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def resolve_addresses(hostname: str, port: int) -> list[str]:
    """Every address this hostname answers with.

    Raises :class:`UnsafeUrlError` when it answers with none — a host that cannot be resolved is
    not a host we are going to reach, and saying so plainly is better than letting the request fail
    later with a connection error nobody can act on.
    """
    try:
        resolved = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"The host {hostname!r} could not be resolved.") from exc

    return [str(info[4][0]) for info in resolved]


def assert_public_url(url: str, allow_private: bool = False) -> str:
    """Check a URL is a public ``http(s)`` address, and return its hostname.

    Raises :class:`UnsafeUrlError` with a message safe to show a tenant — it names what is wrong
    with *their* URL without reporting what happens to be listening on the address it resolved to.

    ``allow_private`` is passed in rather than read from configuration here. Each feature owns its
    own switch — ingestion and agent tools are used differently in local development and there is
    no reason relaxing one should relax the other — and a shared security primitive that read one
    caller's config key would be quietly deciding policy for the other.
    """
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrlError("Only http and https URLs can be requested.")

    hostname = parts.hostname
    if not hostname:
        raise UnsafeUrlError("The URL has no host.")

    if allow_private:
        return hostname

    port = parts.port or (443 if parts.scheme == "https" else 80)
    for address in resolve_addresses(hostname, port):
        if not is_public_address(address):
            raise UnsafeUrlError(
                "That URL resolves to a private or internal address and cannot be requested."
            )
    return hostname
