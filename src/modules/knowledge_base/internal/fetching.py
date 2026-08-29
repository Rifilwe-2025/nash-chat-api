"""Fetching a URL source, safely.

A tenant-supplied URL is an outbound request this server makes on their behalf, which makes it an
SSRF surface: `http://169.254.169.254/` or `http://localhost:6379/` would otherwise reach cloud
metadata and Redis from inside the network. The spec calls this out for agent tools (§5.2.1), and
the same reasoning applies to ingestion — so every host is resolved and checked against the private
ranges *before* the request, and again on each redirect hop, since a public host can redirect to a
private one.

The size cap is enforced while streaming rather than after: a caller must not be able to make the
server buffer an unbounded response by pointing it at a large file.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from src import configs
from src.modules.knowledge_base.internal.extractors.base import ExtractionError

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_REDIRECTS = 5
USER_AGENT = "NashChatBot/1.0 (+knowledge-base ingestion)"


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    body: bytes
    media_type: str


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_fetchable(url: str) -> None:
    """Reject anything that is not a public http(s) URL, before a request is made."""
    parts = urlsplit(url)
    if parts.scheme not in ALLOWED_SCHEMES:
        raise ExtractionError("Only http and https URLs can be used as a source.")
    if not parts.hostname:
        raise ExtractionError("The URL has no host.")

    if configs.KNOWLEDGE_BASE_ALLOW_PRIVATE_URLS:
        return

    try:
        resolved = socket.getaddrinfo(parts.hostname, parts.port or 80, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ExtractionError(f"The host {parts.hostname!r} could not be resolved.") from exc

    # Every address the host resolves to must be public: one private answer is enough to reach an
    # internal service, and which answer is used is not ours to decide.
    for info in resolved:
        address = str(info[4][0])
        if not _is_public(address):
            raise ExtractionError(
                "That URL resolves to a private or internal address and cannot be fetched."
            )


async def fetch(url: str, max_bytes: int, client: httpx.AsyncClient | None = None) -> FetchedPage:
    """Fetch a page, following redirects manually so each hop is re-checked."""
    owned = client is None
    http = client or httpx.AsyncClient(
        timeout=configs.KNOWLEDGE_BASE_URL_FETCH_TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )

    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            assert_fetchable(current)
            response = await _get(http, current)

            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ExtractionError("The URL redirected without saying where.")
                current = str(response.url.join(location))
                continue

            if response.status_code >= 400:
                raise ExtractionError(f"The URL responded with HTTP {response.status_code}.")

            body = await _read_capped(response, max_bytes)
            media_type = (
                response.headers.get("content-type", "text/html").split(";")[0].strip().lower()
            )
            return FetchedPage(url=current, body=body, media_type=media_type or "text/html")

        raise ExtractionError("The URL redirected too many times.")
    finally:
        if owned:
            await http.aclose()


async def _get(http: httpx.AsyncClient, url: str) -> httpx.Response:
    try:
        request = http.build_request("GET", url)
        return await http.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise ExtractionError(f"The URL could not be fetched: {type(exc).__name__}.") from exc


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ExtractionError(
                    f"The page is larger than the {max_bytes} byte limit for a source."
                )
            chunks.append(chunk)
    finally:
        await response.aclose()

    if not chunks:
        raise ExtractionError("The URL returned an empty response.")
    return b"".join(chunks)
