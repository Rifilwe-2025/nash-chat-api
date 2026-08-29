"""Inbound media, and the tenant boundary around the whole channel (spec §5.2.3, §5.5, §5.7).

Media first: a contact photographs a price list, and the words in it have to reach the agent. The
route they take is the *same* extraction path an upload takes — reached service to service, because
``knowledge_base/internal`` is module-private — so a format the knowledge base can read is a format
WhatsApp can read, with no second implementation to keep in step.

Then isolation, which is the project's worst failure mode. A WhatsApp connection is a
``channel_config`` row and a phone number, both of which one tenant can guess about another. None of
that may be enough to reach anything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from src.modules.channels.whatsapp.domain.services import UNSUPPORTED_REPLY, WhatsAppService
from src.modules.channels.whatsapp.internal.providers.base import MediaPayload
from src.modules.channels.whatsapp.presentation.dependencies import (
    WebhookConnectionDep,
    WebhookServices,
    get_webhook_services,
    get_whatsapp_service,
)
from src.modules.conversations.domain.services import ConversationService
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from tests.modules.auth.test_auth_flow import auth_header, signup
from tests.modules.channels.whatsapp.helpers import (
    CONTACT,
    FakeProvider,
    connected_agent,
    meta_webhook,
    post_webhook,
)
from tests.modules.conversations.test_turn import RecordingLLM

PRICE_LIST = b"Product,Size,Price\nWhite emulsion,20L,58.00\nMatt black,5L,19.50\n"


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM(reply="White emulsion in 20 litres is $58.")


@pytest.fixture
def media() -> MediaPayload:
    return MediaPayload(data=PRICE_LIST, media_type="text/csv", filename="prices.csv")


@pytest.fixture
def provider(media: MediaPayload) -> FakeProvider:
    return FakeProvider(media=media)


@pytest.fixture
async def wired(app: FastAPI, provider: FakeProvider, llm: RecordingLLM) -> AsyncIterator[FastAPI]:
    def webhook_services(session: SessionDep, connection: WebhookConnectionDep) -> WebhookServices:
        return WebhookServices(
            whatsapp=WhatsAppService(session, connection.tenant_id, provider=provider),
            conversations=ConversationService(session, connection.tenant_id, llm_client=llm),  # type: ignore[arg-type]
        )

    def tenant_service(session: SessionDep, tenant_id: CurrentTenantDep) -> WhatsAppService:
        return WhatsAppService(session, tenant_id, provider=provider)

    app.dependency_overrides[get_webhook_services] = webhook_services
    app.dependency_overrides[get_whatsapp_service] = tenant_service
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_webhook_services, None)
        app.dependency_overrides.pop(get_whatsapp_service, None)


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


# -- inbound media ---------------------------------------------------------------------


async def test_a_document_is_read_and_its_contents_reach_the_model(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider, llm: RecordingLLM
) -> None:
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    response = await post_webhook(
        client,
        connection["id"],
        meta_webhook(media_kind="document", media_type="text/csv", filename="prices.csv"),
    )

    assert response.status_code == 200
    assert provider.fetched == ["media-1"]
    prompt = llm.requests[0][1].messages[-1].content
    # CSV rows become natural-language sentences on the way through (§5.2.3), so the assertion is
    # on the values rather than on the raw row.
    assert "White emulsion" in prompt
    assert "58.00" in prompt


async def test_a_caption_is_kept_alongside_what_the_file_says(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM
) -> None:
    """The caption is the customer's question; the file is the evidence. Both are needed."""
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    await post_webhook(
        client,
        connection["id"],
        meta_webhook(
            media_kind="document",
            media_type="text/csv",
            filename="prices.csv",
            caption="Is this price still right?",
        ),
    )

    prompt = llm.requests[0][1].messages[-1].content
    assert "Is this price still right?" in prompt
    assert "White emulsion" in prompt


async def test_the_extracted_text_is_fenced_as_data_not_instructions(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM
) -> None:
    """A photographed instruction is still someone else's instruction (§5.7).

    Whatever a contact sends — typed or inside a file — reaches the model inside the user fence the
    conversation engine applies. The channel adds content to that message; it never adds a message
    that escapes the fence.
    """
    from src.modules.conversations.internal.prompt.delimiters import USER_OPEN

    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    await post_webhook(
        client,
        connection["id"],
        meta_webhook(media_kind="document", media_type="text/csv", filename="prices.csv"),
    )

    prompt = llm.requests[0][1].messages[-1].content
    assert USER_OPEN in prompt
    assert prompt.index(USER_OPEN) < prompt.index("White emulsion")


async def test_a_kind_we_cannot_read_gets_a_plain_explanation(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider, llm: RecordingLLM
) -> None:
    """A voice note has no v1 extraction path. The contact is told, not ignored."""
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    response = await post_webhook(
        client, connection["id"], meta_webhook(media_kind="audio", media_type="audio/ogg")
    )

    assert response.status_code == 200
    assert provider.sent_text == [(CONTACT, UNSUPPORTED_REPLY)]
    assert llm.requests == []


async def test_media_that_cannot_be_downloaded_is_recorded_and_answered(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """A failed fetch must not leave the customer with silence, or Meta with a 500."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)
    provider.media = None  # the download will fail

    response = await post_webhook(
        client,
        connection["id"],
        meta_webhook(media_kind="image", media_type="image/jpeg"),
    )

    assert response.status_code == 200
    assert provider.sent_text == [(CONTACT, UNSUPPORTED_REPLY)]

    log = await client.get(
        f"/agents/{agent['id']}/channels/whatsapp/messages?direction=inbound", headers=auth
    )
    assert log.json()["value"][0]["status"] == "failed"
    assert log.json()["value"][0]["errorDetail"]


# -- tenant isolation ------------------------------------------------------------------


async def test_one_tenant_cannot_read_another_tenants_whatsapp_log(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth_a = await owner(client)
    agent_a, connection_a = await connected_agent(client, auth_a)
    await post_webhook(client, connection_a["id"], meta_webhook())

    auth_b = await owner(client)
    stolen = await client.get(f"/agents/{agent_a['id']}/channels/whatsapp/messages", headers=auth_b)

    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_one_tenant_cannot_send_from_another_tenants_number(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    auth_a = await owner(client)
    agent_a, _ = await connected_agent(client, auth_a)

    auth_b = await owner(client)
    stolen = await client.post(
        f"/agents/{agent_a['id']}/channels/whatsapp/messages",
        json={"to": CONTACT, "text": "Hello from somebody else."},
        headers=auth_b,
    )

    assert stolen.status_code == 404
    assert provider.sent_text == []


async def test_one_tenant_cannot_read_another_tenants_connection(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth_a = await owner(client)
    agent_a, _ = await connected_agent(client, auth_a)

    auth_b = await owner(client)
    stolen = await client.get(f"/agents/{agent_a['id']}/channels/whatsapp", headers=auth_b)

    assert stolen.status_code == 404


async def test_a_message_is_answered_by_its_own_tenants_agent(
    client: AsyncClient, wired: FastAPI
) -> None:
    """Two tenants, two connections, the same contact number. Each conversation stays put."""
    auth_a = await owner(client)
    agent_a, connection_a = await connected_agent(client, auth_a)
    auth_b = await owner(client)
    agent_b, connection_b = await connected_agent(client, auth_b)

    await post_webhook(client, connection_a["id"], meta_webhook(message_id="wamid.for-a"))
    await post_webhook(client, connection_b["id"], meta_webhook(message_id="wamid.for-b"))

    log_a = await client.get(f"/agents/{agent_a['id']}/channels/whatsapp/messages", headers=auth_a)
    log_b = await client.get(f"/agents/{agent_b['id']}/channels/whatsapp/messages", headers=auth_b)

    assert len(log_a.json()["value"]) == 2
    assert len(log_b.json()["value"]) == 2
    assert {item["id"] for item in log_a.json()["value"]}.isdisjoint(
        {item["id"] for item in log_b.json()["value"]}
    )


async def test_the_same_wamid_on_two_connections_is_not_a_duplicate(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """Idempotency is scoped to the connection, so one tenant cannot suppress another's messages."""
    auth_a = await owner(client)
    _, connection_a = await connected_agent(client, auth_a)
    auth_b = await owner(client)
    _, connection_b = await connected_agent(client, auth_b)

    first = await post_webhook(client, connection_a["id"], meta_webhook(message_id="wamid.same"))
    second = await post_webhook(client, connection_b["id"], meta_webhook(message_id="wamid.same"))

    assert first.json()["value"]["accepted"] == 1
    assert second.json()["value"]["accepted"] == 1
    assert len(provider.sent_text) == 2


# -- secrets ---------------------------------------------------------------------------


async def test_credentials_are_never_returned(client: AsyncClient, wired: FastAPI) -> None:
    """They go in and stay in. What comes back says only whether they are set (§5.7)."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    read = await client.get(f"/agents/{agent['id']}/channels/whatsapp", headers=auth)
    body = read.text

    assert "EAAG-test-token" not in body
    assert "test-app-secret" not in body
    assert connection["verifyToken"] not in body
    assert read.json()["value"]["credentials"]["hasAccessToken"] is True
