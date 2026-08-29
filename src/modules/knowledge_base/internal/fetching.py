"""Fetching a URL source, safely.

A tenant-supplied URL is an outbound request this server makes on their behalf, which makes it an
SSRF surface: `http://169.254.169.254/` or `http://localhost:6379/` would otherwise reach cloud
metadata and Redis from inside the network. The address check itself lives in ``src/shared/net`` —
agent tools (§5.2.1) make the same kind of request and must apply the same rule, and one copy of a
security check cannot disagree with another. What stays here is what ingestion knows: the size cap,
the redirect policy, and the fact that a failure is an :class:`ExtractionError` a tenant reads on
their source.

The URL is re-checked on **each redirect hop**, since a public host can redirect to a private one.

The size cap is enforced while streaming rather than after: a caller must not be able to make the
server buffer an unbounded response by pointing it at a large file.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from src import configs
from src.modules.knowledge_base.internal.extractors.base import ExtractionError
from src.shared.net import UnsafeUrlError, assert_public_url

MAX_REDIRECTS = 5
USER_AGENT = "NashChatBot/1.0 (+knowledge-base ingestion)"


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    body: bytes
    media_type: str


def assert_fetchable(url: str) -> None:
    """Reject anything that is not a public http(s) URL, before a request is made.

    A thin translation over the shared check: the rule is the same one agent tools apply, and the
    only ingestion-specific part is that a rejection reads as an ``ExtractionError``, which the
    service records on the source for the tenant to see.
    """
    try:
        assert_public_url(url, allow_private=configs.KNOWLEDGE_BASE_ALLOW_PRIVATE_URLS)
    except UnsafeUrlError as exc:
        raise ExtractionError(str(exc)) from exc


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
