"""The WhatsApp channel end to end (spec §5.5, §6).

This file is the phase's bar, in the plan's own words:

* a WhatsApp number reaches a published agent end to end,
* a replayed webhook yields **exactly one** reply,
* a message outside the 24-hour window falls back to a template.

Everything goes through the real HTTP surface with a real signature. Only two things are faked: the
provider's network calls and the LLM, neither of which is code this repo owns.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.channels.whatsapp.domain.models import MessageDirection, WhatsAppMessage
from src.modules.channels.whatsapp.domain.services import WhatsAppService
from src.modules.channels.whatsapp.internal.providers import InboundKind
from src.modules.channels.whatsapp.presentation.dependencies import (
    WebhookConnectionDep,
    WebhookServices,
    get_webhook_services,
    get_whatsapp_service,
)
from src.modules.conversations.domain.models import Channel, Conversation
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

REPLY = "Yes — we stock 20 litre white emulsion at $58."


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM(reply=REPLY)


@pytest.fixture
async def wired(app: FastAPI, provider: FakeProvider, llm: RecordingLLM) -> AsyncIterator[FastAPI]:
    """Point both halves of the channel at the fake provider, and the engine at the fake model.

    Two overrides because the channel has two entrances — Meta's webhook and the tenant's own send
    endpoint — and a test that stubbed only the first would reach the real Graph API from the
    second. That is not hypothetical: it is exactly what these tests did before this fixture
    covered both.
    """

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


# -- the bar ---------------------------------------------------------------------------


async def test_a_whatsapp_message_reaches_a_published_agent_and_is_answered(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    response = await post_webhook(client, connection["id"], meta_webhook())

    assert response.status_code == 200, response.text
    assert response.json()["value"] == {"accepted": 1, "duplicates": 0, "statuses": 0}
    assert provider.sent_text == [(CONTACT, REPLY)]


async def test_a_replayed_webhook_yields_exactly_one_reply(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """The phase's sharpest requirement, and the reason the ledger has a unique constraint.

    Meta redelivers whenever it does not see a prompt 200 — the *same* ``wamid``, not a new one.
    Three identical deliveries must cost the tenant one model call and the customer one answer.
    """
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)
    delivery = meta_webhook(message_id="wamid.replay-me")

    first = await post_webhook(client, connection["id"], delivery)
    second = await post_webhook(client, connection["id"], delivery)
    third = await post_webhook(client, connection["id"], delivery)

    assert [r.status_code for r in (first, second, third)] == [200, 200, 200]
    assert first.json()["value"]["accepted"] == 1
    assert second.json()["value"] == {"accepted": 0, "duplicates": 1, "statuses": 0}
    assert third.json()["value"]["duplicates"] == 1
    assert len(provider.sent_text) == 1


async def test_a_message_outside_the_window_falls_back_to_a_template(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider, session: AsyncSession
) -> None:
    """Free-form text is undeliverable once the 24-hour window closes; a template is not."""
    auth = await owner(client)
    agent, connection = await connected_agent(
        client,
        auth,
        outsideWindowTemplate={
            "name": "order_update",
            "language": "en_US",
            "variables": ["Tariro"],
        },
    )

    # The contact wrote, then went quiet for two days.
    await post_webhook(client, connection["id"], meta_webhook())
    await _age_inbound(session, uuid.UUID(connection["id"]), hours=49)
    provider.sent_text.clear()

    sent = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages",
        json={"to": CONTACT, "text": "Your order is ready."},
        headers=auth,
    )

    assert sent.status_code == 201, sent.text
    assert sent.json()["value"]["messageType"] == "template"
    assert sent.json()["value"]["templateName"] == "order_update"
    assert provider.sent_text == []
    assert [name for _, template in provider.sent_templates for name in [template.name]] == [
        "order_update"
    ]


async def test_a_closed_window_with_no_template_is_refused_not_dropped(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider, session: AsyncSession
) -> None:
    """A tenant who configured no fallback is told, rather than left thinking it was delivered."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    await post_webhook(client, connection["id"], meta_webhook())
    await _age_inbound(session, uuid.UUID(connection["id"]), hours=49)

    refused = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages",
        json={"to": CONTACT, "text": "Your order is ready."},
        headers=auth,
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "WHATSAPP_WINDOW_CLOSED"
    assert provider.sent_templates == []


# -- the conversation underneath -------------------------------------------------------


async def test_the_turn_uses_the_shared_engine_and_is_stored_as_a_conversation(
    client: AsyncClient, wired: FastAPI, llm: RecordingLLM, session: AsyncSession
) -> None:
    """WhatsApp is an adapter, not a second engine: the turn lands in the same tables."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    await post_webhook(client, connection["id"], meta_webhook())

    conversations = (
        (
            await session.execute(
                select(Conversation).where(Conversation.agent_id == uuid.UUID(agent["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert len(conversations) == 1
    assert conversations[0].channel is Channel.WHATSAPP
    # The phone number is the session key, carried through untouched (§5.5).
    assert conversations[0].external_user_id == CONTACT
    assert len(llm.requests) == 1


async def test_a_second_message_continues_the_same_conversation(
    client: AsyncClient, wired: FastAPI, session: AsyncSession
) -> None:
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    await post_webhook(client, connection["id"], meta_webhook(message_id="wamid.one"))
    await post_webhook(
        client, connection["id"], meta_webhook(message_id="wamid.two", text="And in matt?")
    )

    conversations = (
        (
            await session.execute(
                select(Conversation).where(Conversation.agent_id == uuid.UUID(agent["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert len(conversations) == 1


async def test_messages_from_two_contacts_are_separate_conversations(
    client: AsyncClient, wired: FastAPI, session: AsyncSession
) -> None:
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    await post_webhook(client, connection["id"], meta_webhook(message_id="wamid.a"))
    await post_webhook(
        client,
        connection["id"],
        meta_webhook(message_id="wamid.b", contact="263771111111"),
    )

    conversations = (
        (
            await session.execute(
                select(Conversation).where(Conversation.agent_id == uuid.UUID(agent["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert {conversation.external_user_id for conversation in conversations} == {
        CONTACT,
        "263771111111",
    }


# -- sending on our own initiative -----------------------------------------------------


async def test_a_file_can_be_sent_to_a_contact(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """Outbound media: WhatsApp fetches the URL, so only the link crosses the wire."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)
    await post_webhook(client, connection["id"], meta_webhook())

    sent = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages",
        json={
            "to": CONTACT,
            "media": {
                "url": "https://example.com/catalogue.pdf",
                "kind": "document",
                "caption": "This season's range.",
            },
        },
        headers=auth,
    )

    assert sent.status_code == 201, sent.text
    assert sent.json()["value"]["messageType"] == "document"
    assert provider.sent_media == [
        (CONTACT, "https://example.com/catalogue.pdf", InboundKind.DOCUMENT, "This season's range.")
    ]


async def test_media_obeys_the_window_like_any_free_form_message(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider, session: AsyncSession
) -> None:
    """An image is free-form to WhatsApp, so a closed window refuses it exactly as it does text."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)
    await post_webhook(client, connection["id"], meta_webhook())
    await _age_inbound(session, uuid.UUID(connection["id"]), hours=49)

    refused = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages",
        json={"to": CONTACT, "media": {"url": "https://example.com/a.png", "kind": "image"}},
        headers=auth,
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "WHATSAPP_WINDOW_CLOSED"
    assert provider.sent_media == []


async def test_a_send_with_nothing_in_it_is_refused(client: AsyncClient, wired: FastAPI) -> None:
    auth = await owner(client)
    agent, _ = await connected_agent(client, auth)

    refused = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages", json={"to": CONTACT}, headers=auth
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "WHATSAPP_NOTHING_TO_SEND"


async def test_a_template_is_delivered_even_when_the_window_is_open(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """An explicit template is a choice, not a fallback, and is honoured either way."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)
    await post_webhook(client, connection["id"], meta_webhook())

    sent = await client.post(
        f"/agents/{agent['id']}/channels/whatsapp/messages",
        json={"to": CONTACT, "template": {"name": "order_ready", "variables": ["Tariro"]}},
        headers=auth,
    )

    assert sent.status_code == 201, sent.text
    assert [template.name for _, template in provider.sent_templates] == ["order_ready"]
    assert provider.sent_templates[0][1].variables == ["Tariro"]


# -- the delivery log ------------------------------------------------------------------


async def test_both_sides_of_the_exchange_appear_in_the_delivery_log(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    await post_webhook(client, connection["id"], meta_webhook())
    log = await client.get(f"/agents/{agent['id']}/channels/whatsapp/messages", headers=auth)

    assert log.status_code == 200, log.text
    items = log.json()["value"]
    assert {item["direction"] for item in items} == {"inbound", "outbound"}
    inbound = next(item for item in items if item["direction"] == "inbound")
    outbound = next(item for item in items if item["direction"] == "outbound")
    assert inbound["status"] == "processed"
    assert outbound["status"] == "sent"
    assert outbound["body"] == REPLY
    # The inbound message is tied to the conversation the turn created, so a support engineer can
    # get from a delivery receipt to the transcript.
    assert inbound["conversationId"] == outbound["conversationId"] is not None


async def test_delivery_receipts_advance_an_outbound_message(
    client: AsyncClient, wired: FastAPI, provider: FakeProvider
) -> None:
    """Meta reports what became of a message separately, keyed by the id it gave us."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)
    await post_webhook(client, connection["id"], meta_webhook())

    log = await client.get(f"/agents/{agent['id']}/channels/whatsapp/messages", headers=auth)
    outbound = next(item for item in log.json()["value"] if item["direction"] == "outbound")

    receipt = await post_webhook(
        client,
        connection["id"],
        meta_webhook(
            text=None,
            statuses=[
                {
                    "id": outbound["providerMessageId"],
                    "status": "delivered",
                    "timestamp": "1735689700",
                    "recipient_id": CONTACT,
                }
            ],
        ),
    )

    assert receipt.json()["value"]["statuses"] == 1
    refreshed = await client.get(
        f"/agents/{agent['id']}/channels/whatsapp/messages?direction=outbound", headers=auth
    )
    assert refreshed.json()["value"][0]["status"] == "delivered"
    assert refreshed.json()["value"][0]["deliveredAt"] is not None


async def test_a_receipt_for_an_unknown_message_is_ignored_not_an_error(
    client: AsyncClient, wired: FastAPI
) -> None:
    """A number may be shared with another system; its receipts are not ours to fail on."""
    auth = await owner(client)
    _, connection = await connected_agent(client, auth)

    response = await post_webhook(
        client,
        connection["id"],
        meta_webhook(
            text=None,
            statuses=[{"id": "wamid.not-ours", "status": "read", "timestamp": "1735689700"}],
        ),
    )

    assert response.status_code == 200
    assert response.json()["value"]["statuses"] == 0


# -- helpers ---------------------------------------------------------------------------


async def _age_inbound(session: AsyncSession, connection_id: uuid.UUID, hours: int) -> None:
    """Move this connection's inbound messages into the past, to close the window.

    Rewriting ``created_at`` rather than freezing the clock: the window is derived from the ledger,
    so ageing the row is the honest way to test what the query actually reads.
    """
    rows = (
        (
            await session.execute(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.connection_id == connection_id,
                    WhatsAppMessage.direction == MessageDirection.INBOUND,
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows, "expected an inbound message to age"
    for row in rows:
        row.created_at = datetime.now(UTC) - timedelta(hours=hours)
    await session.commit()
