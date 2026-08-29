"""Source ingestion over HTTP — every v1 source type, end to end (spec §5.2.3).

The phase's bar is here: every supported format can be uploaded and its extracted text inspected
through the API, and a failed extraction surfaces as a readable source error rather than a 500.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from src.modules.knowledge_base.internal import tasks as kb_tasks
from src.modules.knowledge_base.internal.fetching import FetchedPage
from tests.modules.knowledge_base.helpers import (
    DOCX_MEDIA_TYPE,
    build_docx,
    create_kb,
    headers,
    upload,
    upload_ok,
)


async def read_source(
    client: AsyncClient, auth: dict[str, str], kb_id: str, source_id: str
) -> dict[str, Any]:
    response = await client.get(f"/knowledge-bases/{kb_id}/sources/{source_id}", headers=auth)
    assert response.status_code == 200, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


# -- file uploads ----------------------------------------------------------------


async def test_a_text_file_round_trips_to_readable_extracted_text(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    source = await upload_ok(
        client, auth, knowledge_base["id"], "prices.txt", b"Matt white 5L is $45.99."
    )

    assert source["status"] == "ready"
    assert source["type"] == "file"
    assert source["extractedText"] == "Matt white 5L is $45.99."
    assert source["metadata"]["filename"] == "prices.txt"
    assert source["byteSize"] == 24


async def test_a_word_document_keeps_its_headings(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    data = build_docx([("Heading 1", "Returns"), ("Normal", "Within 30 days.")])

    source = await upload_ok(
        client, auth, knowledge_base["id"], "returns.docx", data, DOCX_MEDIA_TYPE
    )

    assert source["status"] == "ready"
    assert "# Returns" in source["extractedText"]
    assert source["metadata"]["headings"] == 1


async def test_a_csv_is_stored_as_sentences(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    source = await upload_ok(
        client,
        auth,
        knowledge_base["id"],
        "prices.csv",
        b"SKU,Price,Colour\nSKU123,45.99,Blue\n",
        "text/csv",
    )

    assert source["extractedText"] == "SKU SKU123: Price is 45.99, Colour is Blue."
    assert source["metadata"]["rows"] == 1


async def test_an_html_file_is_stripped_of_boilerplate(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    page = b"<html><body><nav>Shop</nav><main><h1>Care</h1><p>Stir well.</p></main></body></html>"

    source = await upload_ok(client, auth, knowledge_base["id"], "care.html", page, "text/html")

    assert "# Care" in source["extractedText"]
    assert "Shop" not in source["extractedText"]


async def test_an_unsupported_file_type_is_refused_before_anything_is_stored(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    status, body = await upload(
        client, auth, knowledge_base["id"], "backup.zip", b"PK\x03\x04", "application/zip"
    )

    assert status == 422
    assert body["error"]["code"] == "KB_UNSUPPORTED_FILE_TYPE"
    listed = await client.get(f"/knowledge-bases/{knowledge_base['id']}/sources", headers=auth)
    assert listed.json()["meta"]["totalItems"] == 0


async def test_a_browser_that_declares_octet_stream_is_still_understood(
    client: AsyncClient,
) -> None:
    """Browsers send ``application/octet-stream`` for anything they do not recognise, so the
    extension has to be what decides."""
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    source = await upload_ok(
        client,
        auth,
        knowledge_base["id"],
        "notes.md",
        b"# Notes\n\nMix thoroughly.",
        "application/octet-stream",
    )

    assert source["status"] == "ready"
    assert "# Notes" in source["extractedText"]


# -- failed extraction is data, not a 500 ------------------------------------------


async def test_a_corrupt_document_becomes_a_failed_source_not_an_error(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    status, body = await upload(
        client, auth, knowledge_base["id"], "broken.docx", b"this is not a docx", DOCX_MEDIA_TYPE
    )

    assert status == 201, "a document that cannot be read is a stored failure, not a request error"
    source = body["value"]
    assert source["status"] == "failed"
    assert "Word document" in source["errorDetail"]
    assert "extractedText" not in source


async def test_a_failed_source_is_listed_and_readable_afterwards(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    _, body = await upload(client, auth, knowledge_base["id"], "empty.txt", b"   ", "text/plain")

    source = await read_source(client, auth, knowledge_base["id"], body["value"]["id"])

    assert source["status"] == "failed"
    assert source["errorDetail"] == "The file is empty."
    assert source["lastSyncedAt"], "a failed attempt still records when it ran"


async def test_a_failed_source_does_not_consume_storage(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    await upload(client, auth, knowledge_base["id"], "broken.docx", b"x" * 4_000, DOCX_MEDIA_TYPE)

    usage = (await client.get("/knowledge-bases/usage", headers=auth)).json()["value"]
    assert usage["usedBytes"] == 0


# -- manual FAQ entries -------------------------------------------------------------


async def test_a_manual_entry_is_stored_with_its_question_as_a_heading(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/manual",
        json={"title": "How long does delivery take?", "body": "Next working day in Harare."},
        headers=auth,
    )

    assert response.status_code == 201
    source = response.json()["value"]
    assert source["type"] == "manual"
    assert source["status"] == "ready"
    assert source["extractedText"] == (
        "# How long does delivery take?\n\nNext working day in Harare."
    )


async def test_a_manual_entry_needs_both_a_question_and_an_answer(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/manual",
        json={"title": "Empty", "body": ""},
        headers=auth,
    )

    assert response.status_code == 422


# -- URL sources --------------------------------------------------------------------


PAGE = b"<html><head><title>Returns</title></head><body><nav>Home</nav>"
PAGE += b"<main><h1>Returns</h1><p>Within 30 days.</p></main></body></html>"


@pytest.fixture
def fetched_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the network.

    Patched on the task rather than the service: fetching a URL is the worker's job since Phase 9.
    The fetcher's own behaviour is covered in test_fetching.py.
    """

    async def fake_fetch(url: str, max_bytes: int, client: object | None = None) -> FetchedPage:
        return FetchedPage(url=url, body=PAGE, media_type="text/html")

    monkeypatch.setattr(kb_tasks, "fetch", fake_fetch)


async def test_a_url_source_stores_the_pages_readable_text(
    client: AsyncClient, fetched_page: None
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/url",
        json={"url": "https://example.com/returns", "name": "Returns policy"},
        headers=auth,
    )

    assert response.status_code == 201
    source = response.json()["value"]
    assert source["type"] == "url"
    assert source["status"] == "ready"
    assert source["name"] == "Returns policy"
    assert "# Returns" in source["extractedText"]
    assert "Home" not in source["extractedText"]
    assert source["metadata"]["url"] == "https://example.com/returns"


async def test_a_url_serving_an_unsupported_type_becomes_a_failed_source(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike an upload, this is not a 422: the tenant supplied a URL, and what the server on the
    other end chose to serve is a property of the page, not of their request."""

    async def serves_a_zip(url: str, max_bytes: int, client: object | None = None) -> FetchedPage:
        return FetchedPage(url=url, body=b"PK", media_type="application/zip")

    monkeypatch.setattr(kb_tasks, "fetch", serves_a_zip)
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/url",
        json={"url": "https://example.com/archive"},
        headers=auth,
    )

    assert response.status_code == 201
    assert response.json()["value"]["status"] == "failed"
    assert "Unsupported file type" in response.json()["value"]["errorDetail"]


async def test_an_unreachable_url_becomes_a_failed_source(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.post(
        f"/knowledge-bases/{knowledge_base['id']}/sources/url",
        json={"url": "http://127.0.0.1:9/nothing"},
        headers=auth,
    )

    assert response.status_code == 201
    source = response.json()["value"]
    assert source["status"] == "failed"
    assert "private or internal" in source["errorDetail"]


# -- listing, reading, deleting -------------------------------------------------------


async def test_the_source_list_omits_extracted_text(client: AsyncClient) -> None:
    """A page of sources would otherwise be megabytes of documents."""
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    await upload_ok(client, auth, knowledge_base["id"], "a.txt", b"Matt white is $45.")

    body = (
        await client.get(f"/knowledge-bases/{knowledge_base['id']}/sources", headers=auth)
    ).json()

    assert body["meta"]["totalItems"] == 1
    assert "extractedText" not in body["value"][0]
    assert body["value"][0]["status"] == "ready"


async def test_a_source_from_another_knowledge_base_is_not_reachable(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    first = await create_kb(client, auth, name="First")
    second = await create_kb(client, auth, name="Second")
    source = await upload_ok(client, auth, first["id"], "a.txt", b"Matt white is $45.")

    response = await client.get(
        f"/knowledge-bases/{second['id']}/sources/{source['id']}", headers=auth
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_SOURCE_NOT_FOUND"


async def test_an_unknown_source_is_reported_as_missing(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.get(
        f"/knowledge-bases/{knowledge_base['id']}/sources/{uuid.uuid4()}", headers=auth
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_SOURCE_NOT_FOUND"


async def test_deleting_a_source_frees_its_storage(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    source = await upload_ok(client, auth, knowledge_base["id"], "a.txt", b"x" * 800)

    response = await client.delete(
        f"/knowledge-bases/{knowledge_base['id']}/sources/{source['id']}", headers=auth
    )

    assert response.status_code == 200
    usage = (await client.get("/knowledge-bases/usage", headers=auth)).json()["value"]
    assert usage["usedBytes"] == 0
