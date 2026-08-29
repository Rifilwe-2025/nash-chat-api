"""The REST connector — Pattern B's pull (spec §5.2.1).

Driven against a mock transport rather than a live API: pagination, auth shaping, field mapping and
the error messages a tenant sees are all ours, and a real endpoint would test someone else's server.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.modules.knowledge_base.internal.connectors import (
    ConnectorError,
    PaginationStyle,
    RestConnector,
)

# Loopback, because the connector runs the same SSRF guard as URL ingestion and that guard does a
# real DNS lookup. A resolvable public hostname would make these tests depend on the network to
# reach a transport that never leaves the process.
ENDPOINT = "http://127.0.0.1/api/products"


@pytest.fixture(autouse=True)
def allow_loopback(config_override: Callable[..., None]) -> None:
    """Let the guard through for the mocked endpoint. The guard itself is asserted below."""
    config_override(KB_ALLOW_PRIVATE_URLS="true")


BASE_CONFIG: dict[str, Any] = {
    "url": ENDPOINT,
    "contentFields": ["sku", "name", "price"],
    "metadataFields": ["category"],
    "idField": "sku",
    "versionField": "updated_at",
}


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def product(
    sku: str, name: str = "Matt white", price: float = 45.99, **extra: Any
) -> dict[str, Any]:
    return {"sku": sku, "name": name, "price": price, "category": "Interior", **extra}


# -- field mapping ---------------------------------------------------------------------


async def test_records_become_sentences_not_raw_json() -> None:
    """The same reasoning as the CSV extractor: a raw record reads as noise in a prompt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU123")])

    async with client(handler) as http:
        result = await RestConnector(BASE_CONFIG, http).fetch()

    assert len(result.records) == 1
    assert result.records[0].text == "Sku: SKU123. Name: Matt white. Price: 45.99."
    assert "{" not in result.records[0].text


async def test_metadata_fields_are_kept_out_of_the_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU123")])

    async with client(handler) as http:
        record = (await RestConnector(BASE_CONFIG, http).fetch()).records[0]

    assert "Interior" not in record.text
    assert record.metadata["category"] == "Interior"


async def test_nested_fields_are_reachable_by_path() -> None:
    config = {**BASE_CONFIG, "contentFields": ["sku", "detail.description"], "recordsPath": "data"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"sku": "SKU1", "detail": {"description": "Washable emulsion"}}]},
        )

    async with client(handler) as http:
        record = (await RestConnector(config, http).fetch()).records[0]

    assert "Washable emulsion" in record.text


async def test_empty_fields_are_left_out_of_the_sentence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"sku": "SKU1", "name": "", "price": None}])

    async with client(handler) as http:
        record = (await RestConnector(BASE_CONFIG, http).fetch()).records[0]

    assert record.text == "Sku: SKU1."


async def test_a_record_with_nothing_indexable_is_skipped() -> None:
    """Storing an empty record would put a blank line in someone's prompt."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"sku": None, "name": None, "price": None}])

    async with client(handler) as http:
        result = await RestConnector(BASE_CONFIG, http).fetch()

    assert result.records == []


async def test_list_values_are_joined() -> None:
    config = {**BASE_CONFIG, "contentFields": ["sku", "colours"]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"sku": "SKU1", "colours": ["Blue", "Red"]}])

    async with client(handler) as http:
        record = (await RestConnector(config, http).fetch()).records[0]

    assert "Colours: Blue, Red." in record.text


# -- pagination -------------------------------------------------------------------------


async def test_page_number_pagination_walks_every_page() -> None:
    pages = {1: [product("SKU1")], 2: [product("SKU2")], 3: []}
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", 1))
        seen.append(page)
        return httpx.Response(200, json=pages.get(page, []))

    config = {**BASE_CONFIG, "pagination": PaginationStyle.PAGE_NUMBER.value}
    async with client(handler) as http:
        result = await RestConnector(config, http).fetch()

    assert seen == [1, 2, 3]
    assert [record.external_id for record in result.records] == ["SKU1", "SKU2"]


async def test_cursor_pagination_follows_the_cursor_and_stops_without_one() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"items": [product("SKU1")], "next": "abc"})
        return httpx.Response(200, json={"items": [product("SKU2")], "next": None})

    config = {
        **BASE_CONFIG,
        "pagination": PaginationStyle.CURSOR.value,
        "recordsPath": "items",
        "nextCursorPath": "next",
    }
    async with client(handler) as http:
        result = await RestConnector(config, http).fetch()

    assert [record.external_id for record in result.records] == ["SKU1", "SKU2"]


async def test_an_unpaginated_endpoint_is_fetched_once() -> None:
    """`none` is a real option — plenty of catalogues return everything at once."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=[product("SKU1")])

    async with client(handler) as http:
        result = await RestConnector(BASE_CONFIG, http).fetch()

    assert calls == 1
    assert result.pages_fetched == 1


async def test_a_very_large_source_is_truncated_rather_than_unbounded(
    config_override: Callable[..., None],
) -> None:
    config_override(SYNC_MAX_RECORDS=3)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product(f"SKU{index}") for index in range(10)])

    async with client(handler) as http:
        result = await RestConnector(BASE_CONFIG, http).fetch()

    assert len(result.records) == 3
    assert result.truncated is True


# -- auth --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("auth_type", "credentials", "header", "expected"),
    [
        ("bearer", {"token": "abc123"}, "authorization", "Bearer abc123"),
        ("api_key_header", {"header": "X-Shop-Key", "value": "k1"}, "x-shop-key", "k1"),
    ],
)
async def test_auth_is_sent_the_way_the_endpoint_expects(
    auth_type: str, credentials: dict[str, str], header: str, expected: str
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[product("SKU1")])

    config = {**BASE_CONFIG, "authType": auth_type, "credentials": credentials}
    async with client(handler) as http:
        await RestConnector(config, http).fetch()

    assert seen[header] == expected


async def test_basic_auth_is_encoded() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=[product("SKU1")])

    config = {
        **BASE_CONFIG,
        "authType": "basic",
        "credentials": {"username": "ada", "password": "hunter2"},
    }
    async with client(handler) as http:
        await RestConnector(config, http).fetch()

    decoded = base64.b64decode(seen["authorization"].removeprefix("Basic ")).decode()
    assert decoded == "ada:hunter2"


async def test_missing_credentials_are_reported_before_the_call() -> None:
    config = {**BASE_CONFIG, "authType": "bearer", "credentials": {}}

    with pytest.raises(ConnectorError, match="no token"):
        await RestConnector(config).fetch()


# -- failures the tenant has to fix ---------------------------------------------------------


async def test_expired_credentials_are_reported_as_such() -> None:
    """The most common way a scheduled sync breaks (spec §5.2.1)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    async with client(handler) as http:
        with pytest.raises(ConnectorError, match="may have expired"):
            await RestConnector(BASE_CONFIG, http).fetch()


async def test_a_changed_response_shape_is_reported_as_such() -> None:
    """The second most common way, and the one that is hardest to diagnose from a log."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"products": []})

    config = {**BASE_CONFIG, "recordsPath": "data.items"}
    async with client(handler) as http:
        with pytest.raises(ConnectorError, match="shape may have changed"):
            await RestConnector(config, http).fetch()


async def test_a_non_json_response_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with client(handler) as http:
        with pytest.raises(ConnectorError, match="did not return JSON"):
            await RestConnector(BASE_CONFIG, http).fetch()


async def test_an_unreachable_endpoint_is_reported() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with client(handler) as http:
        with pytest.raises(ConnectorError, match="could not be reached"):
            await RestConnector(BASE_CONFIG, http).fetch()


async def test_a_connector_with_no_content_fields_is_refused() -> None:
    with pytest.raises(ConnectorError, match="nothing to index"):
        await RestConnector({"url": ENDPOINT, "contentFields": []}).fetch()


async def test_an_internal_endpoint_is_refused(config_override: Callable[..., None]) -> None:
    """A connector URL is tenant-supplied, so it is the same SSRF surface as URL ingestion."""
    config_override(KB_ALLOW_PRIVATE_URLS="false")

    with pytest.raises(ConnectorError, match="private or internal"):
        await RestConnector({**BASE_CONFIG, "url": "http://169.254.169.254/latest/"}).fetch()


# -- fingerprints --------------------------------------------------------------------------


async def test_a_version_field_becomes_the_records_fingerprint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1", updated_at="2026-01-01")])

    async with client(handler) as http:
        record = (await RestConnector(BASE_CONFIG, http).fetch()).records[0]

    assert record.version == "2026-01-01"
    assert record.fingerprint() == "2026-01-01"


async def test_without_a_version_field_the_content_is_hashed() -> None:
    """Incremental sync still works against an API that exposes no version."""
    config = {**BASE_CONFIG, "versionField": None}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[product("SKU1")])

    async with client(handler) as http:
        record = (await RestConnector(config, http).fetch()).records[0]

    assert record.version is None
    assert len(record.fingerprint()) == 64
