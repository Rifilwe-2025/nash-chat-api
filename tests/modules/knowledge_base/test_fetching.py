"""The URL source guard (spec §5.2.1's SSRF concern, applied to ingestion).

A tenant-supplied URL makes this server issue an outbound request, so the interesting cases are the
ones where the URL points somewhere it should not: loopback, the cloud metadata endpoint, a private
range, or a public host that redirects into one.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from src.modules.knowledge_base.internal.extractors import ExtractionError
from src.modules.knowledge_base.internal.fetching import MAX_REDIRECTS, assert_fetchable, fetch

PAGE = b"<html><body><main><h1>Returns</h1><p>Within 30 days.</p></main></body></html>"


def mock_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6379/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/admin",
        "http://[::1]:8000/",
        "http://0.0.0.0:8000/",
    ],
)
def test_internal_addresses_are_refused(url: str) -> None:
    with pytest.raises(ExtractionError, match="private or internal"):
        assert_fetchable(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_only_http_and_https_are_accepted(url: str) -> None:
    with pytest.raises(ExtractionError, match="http and https"):
        assert_fetchable(url)


def test_the_guard_can_be_lifted_for_local_development(
    config_override: Callable[..., None],
) -> None:
    """Ingesting from a local server is a reasonable thing to do while developing — but it is an
    explicit opt-in, off by default, precisely because it is the SSRF hole."""
    config_override(KB_ALLOW_PRIVATE_URLS="true")

    assert_fetchable("http://127.0.0.1:8000/docs")


async def test_a_page_is_fetched_and_its_content_type_reported(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_ALLOW_PRIVATE_URLS="true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=PAGE, headers={"content-type": "text/html; charset=utf-8"}
        )

    async with mock_client(handler) as client:
        page = await fetch("http://127.0.0.1/returns", max_bytes=10_000, client=client)

    assert page.body == PAGE
    assert page.media_type == "text/html"


async def test_a_redirect_into_a_private_address_is_refused_at_the_hop() -> None:
    """The first host is public, so a check made only before the first request would miss this."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})

    async with mock_client(handler) as client:
        with pytest.raises(ExtractionError, match="private or internal"):
            await fetch("http://93.184.216.34/start", max_bytes=10_000, client=client)


async def test_a_redirect_loop_ends_rather_than_spinning(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_ALLOW_PRIVATE_URLS="true")
    hops = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hops
        hops += 1
        return httpx.Response(302, headers={"location": f"http://127.0.0.1/{hops}"})

    async with mock_client(handler) as client:
        with pytest.raises(ExtractionError, match="redirected too many times"):
            await fetch("http://127.0.0.1/start", max_bytes=10_000, client=client)

    assert hops == MAX_REDIRECTS + 1


async def test_an_oversized_page_stops_downloading_at_the_limit(
    config_override: Callable[..., None],
) -> None:
    """Enforced while streaming: a caller must not be able to make the server buffer 5 GB."""
    config_override(KB_ALLOW_PRIVATE_URLS="true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5_000, headers={"content-type": "text/html"})

    async with mock_client(handler) as client:
        with pytest.raises(ExtractionError, match="larger than"):
            await fetch("http://127.0.0.1/big", max_bytes=1_000, client=client)


async def test_an_error_status_is_reported_as_the_status(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_ALLOW_PRIVATE_URLS="true")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with mock_client(handler) as client:
        with pytest.raises(ExtractionError, match="HTTP 404"):
            await fetch("http://127.0.0.1/missing", max_bytes=10_000, client=client)


async def test_a_connection_failure_is_reported_readably(
    config_override: Callable[..., None],
) -> None:
    config_override(KB_ALLOW_PRIVATE_URLS="true")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with mock_client(handler) as client:
        with pytest.raises(ExtractionError, match="could not be fetched"):
            await fetch("http://127.0.0.1/down", max_bytes=10_000, client=client)
