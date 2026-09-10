"""Downloading an agent's integration guide (spec §5.6).

The guide already existed on screen; this is the half that lets a tenant send it to whoever is doing
the integration. Two things matter beyond "it returns a file": the file says the same thing the
screen does, and it is safe to forward — a document that leaked a key would be the worst possible
thing to attach to an email.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import auth_header, signup

PUBLISHABLE: dict[str, Any] = {
    "persona": "You are the sales assistant for Nash Paints.",
    "modelProvider": "gemini",
    "modelSettings": {"model": "gemini-2.0-flash", "temperature": 0.5, "maxTokens": 512},
}


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def published_agent(
    client: AsyncClient, auth: dict[str, str], name: str | None = None
) -> dict[str, Any]:
    created = await client.post(
        "/agents",
        json={"name": name or f"Agent {uuid.uuid4().hex[:6]}", **PUBLISHABLE},
        headers=auth,
    )
    agent: dict[str, Any] = created.json()["value"]
    await client.post(f"/agents/{agent['id']}/publish", headers=auth)
    return agent


async def test_the_markdown_download_is_the_document_the_screen_shows(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)

    onscreen = await client.get(f"/agents/{agent['id']}/integration-docs", headers=auth)
    downloaded = await client.get(
        f"/agents/{agent['id']}/integration-docs/export?format=md", headers=auth
    )

    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("text/markdown")
    assert downloaded.text == onscreen.json()["value"]["markdown"]


async def test_markdown_is_what_you_get_without_asking(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)

    response = await client.get(f"/agents/{agent['id']}/integration-docs/export", headers=auth)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")


async def test_the_pdf_download_is_a_pdf(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)

    response = await client.get(
        f"/agents/{agent['id']}/integration-docs/export?format=pdf", headers=auth
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert b"/ToUnicode" in response.content, "the text is text, not a picture of text"


@pytest.mark.parametrize(("docs_format", "extension"), [("md", "md"), ("pdf", "pdf")])
async def test_the_file_is_named_after_the_agent(
    client: AsyncClient, docs_format: str, extension: str
) -> None:
    """A folder of files called `export` helps nobody once there are three agents."""
    auth = await owner(client)
    agent = await published_agent(client, auth, name="Nash Paints Sales")

    response = await client.get(
        f"/agents/{agent['id']}/integration-docs/export?format={docs_format}", headers=auth
    )

    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="integrating-nash-paints-sales.{extension}"'


async def test_a_name_that_is_no_help_in_a_filename_still_produces_one(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth, name="中文 🙂")

    response = await client.get(f"/agents/{agent['id']}/integration-docs/export", headers=auth)

    assert response.headers["content-disposition"] == 'attachment; filename="integrating-agent.md"'


@pytest.mark.parametrize("docs_format", ["md", "pdf"])
async def test_a_shared_file_carries_the_key_prefix_and_never_the_secret(
    client: AsyncClient, docs_format: str
) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)
    issued = await client.post(
        f"/api-keys?agentId={agent['id']}", json={"name": "Widget"}, headers=auth
    )
    secret = issued.json()["value"]["key"]
    prefix = issued.json()["value"]["apiKey"]["prefix"]
    key_id = issued.json()["value"]["apiKey"]["id"]

    response = await client.get(
        f"/agents/{agent['id']}/integration-docs/export?format={docs_format}&apiKeyId={key_id}",
        headers=auth,
    )

    assert secret.encode() not in response.content, "the secret must never reach a shared file"
    if docs_format == "md":
        assert prefix in response.text


async def test_an_unknown_format_is_refused(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await published_agent(client, auth)

    response = await client.get(
        f"/agents/{agent['id']}/integration-docs/export?format=docx", headers=auth
    )

    assert response.status_code == 422


async def test_another_agents_key_cannot_be_borrowed_for_the_download(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    first = await published_agent(client, auth)
    second = await published_agent(client, auth)
    issued = await client.post(
        f"/api-keys?agentId={second['id']}", json={"name": "Widget"}, headers=auth
    )

    response = await client.get(
        f"/agents/{first['id']}/integration-docs/export"
        f"?apiKeyId={issued.json()['value']['apiKey']['id']}",
        headers=auth,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "API_KEY_NOT_FOUND"


async def test_another_tenants_agent_cannot_be_downloaded(client: AsyncClient) -> None:
    first = await owner(client)
    second = await owner(client)
    agent = await published_agent(client, first)

    response = await client.get(f"/agents/{agent['id']}/integration-docs/export", headers=second)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_the_download_requires_authentication(client: AsyncClient) -> None:
    response = await client.get(f"/agents/{uuid.uuid4()}/integration-docs/export")

    assert response.status_code == 401
