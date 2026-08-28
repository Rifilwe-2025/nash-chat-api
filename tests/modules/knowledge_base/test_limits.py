"""File size, type, and per-tenant storage limits (spec §5.2).

All three are checked *before* anything is extracted or stored, so they are the one class of
ingestion failure that is a 4xx rather than a stored failed source: the tenant has to change the
request, not look at the result.
"""

from __future__ import annotations

from collections.abc import Callable

from httpx import AsyncClient

from tests.modules.knowledge_base.helpers import create_kb, headers, upload, upload_ok


async def test_a_file_over_the_per_source_limit_is_rejected(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_MAX_SOURCE_BYTES=1_000)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    status, body = await upload(client, auth, knowledge_base["id"], "big.txt", b"x" * 1_001)

    assert status == 422
    assert body["error"]["code"] == "KB_SOURCE_TOO_LARGE"
    assert "0.0 MB" in body["error"]["detail"], "the message states the size and the limit"


async def test_a_file_exactly_on_the_limit_is_accepted(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_MAX_SOURCE_BYTES=1_000)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    source = await upload_ok(client, auth, knowledge_base["id"], "exact.txt", b"x" * 1_000)

    assert source["byteSize"] == 1_000


async def test_an_empty_file_is_rejected_rather_than_stored(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    status, body = await upload(client, auth, knowledge_base["id"], "nothing.txt", b"")

    assert status == 422
    assert body["error"]["code"] == "KB_SOURCE_EMPTY"


async def test_the_tenant_storage_limit_stops_the_next_upload(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_MAX_TENANT_BYTES=2_000)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    await upload_ok(client, auth, knowledge_base["id"], "first.txt", b"x" * 1_500)

    status, body = await upload(client, auth, knowledge_base["id"], "second.txt", b"y" * 900)

    assert status == 422
    assert body["error"]["code"] == "KB_STORAGE_LIMIT_REACHED"


async def test_the_storage_limit_spans_every_knowledge_base_a_tenant_owns(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    """The quota is the tenant's, not one knowledge base's — a second knowledge base is not a way
    around it."""
    config_override(KB_MAX_TENANT_BYTES=2_000)
    auth = await headers(client)
    first = await create_kb(client, auth, name="First")
    second = await create_kb(client, auth, name="Second")
    await upload_ok(client, auth, first["id"], "first.txt", b"x" * 1_500)

    status, body = await upload(client, auth, second["id"], "second.txt", b"y" * 900)

    assert status == 422
    assert body["error"]["code"] == "KB_STORAGE_LIMIT_REACHED"


async def test_deleting_a_source_makes_room_again(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_MAX_TENANT_BYTES=2_000)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    source = await upload_ok(client, auth, knowledge_base["id"], "first.txt", b"x" * 1_500)

    await client.delete(
        f"/knowledge-bases/{knowledge_base['id']}/sources/{source['id']}", headers=auth
    )

    assert (await upload_ok(client, auth, knowledge_base["id"], "second.txt", b"y" * 1_500))[
        "status"
    ] == "ready"


async def test_a_manual_entry_counts_against_the_storage_limit(
    client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(KB_MAX_TENANT_BYTES=100)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/manual",
        json={"title": "A long question that runs on", "body": "x" * 200},
        headers=auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "KB_STORAGE_LIMIT_REACHED"
