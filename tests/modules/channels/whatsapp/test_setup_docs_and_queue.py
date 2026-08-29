"""The setup steps a tenant is handed, and the path a real deployment takes (spec §5.5, §5.6).

Two things the phase asks for that the other files do not reach.

**The generated integration guide must carry the WhatsApp setup steps**, because §5.6 says setup is
part of the docs and §10 says a developer must be able to follow them without help. A connected
agent's guide has to name their callback URL — not a placeholder.

**The queue path** is the one a deployment actually runs: ``QUEUE_MODE=redis`` claims the message in
the request and answers it in a worker. The tests elsewhere run inline because that is the default,
so this file exercises the worker function directly to prove the two paths agree on the outcome.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.channels.whatsapp.domain.models import (
    DeliveryStatus,
    MessageDirection,
    WhatsAppMessage,
)
from src.modules.channels.whatsapp.domain.services import WhatsAppService
from src.modules.channels.whatsapp.internal import tasks
from src.modules.channels.whatsapp.internal.providers.base import InboundKind, InboundMessage
from src.modules.channels.whatsapp.presentation.dependencies import (
    WebhookConnectionDep,
    WebhookServices,
    get_webhook_services,
)
from src.modules.conversations.domain.services import ConversationService
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

REPLY = "We are open until five."


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def llm() -> RecordingLLM:
    return RecordingLLM(reply=REPLY)


@pytest.fixture
async def wired(app: FastAPI, provider: FakeProvider, llm: RecordingLLM) -> AsyncIterator[FastAPI]:
    def override(session: SessionDep, connection: WebhookConnectionDep) -> WebhookServices:
        return WebhookServices(
            whatsapp=WhatsAppService(session, connection.tenant_id, provider=provider),
            conversations=ConversationService(session, connection.tenant_id, llm_client=llm),  # type: ignore[arg-type]
        )

    app.dependency_overrides[get_webhook_services] = override
    try:
        yield app
    finally:
        app.dependency_overrides.pop(get_webhook_services, None)


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


# -- the generated setup steps ---------------------------------------------------------


async def test_a_connected_agents_guide_carries_its_own_webhook_url(
    client: AsyncClient, wired: FastAPI
) -> None:
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    docs = await client.get(f"/agents/{agent['id']}/integration-docs", headers=auth)
    markdown = docs.json()["value"]["markdown"]

    assert "## WhatsApp" in markdown
    assert f"/v1/channels/whatsapp/webhook/{connection['id']}" in markdown
    # The things §6 says are easy to get wrong must be spelled out, not implied.
    assert "24-hour" in markdown
    assert "X-Hub-Signature-256" in markdown
    assert "WHATSAPP_WINDOW_CLOSED" in markdown


async def test_the_guide_never_leaks_a_credential(client: AsyncClient, wired: FastAPI) -> None:
    """The guide is written to be forwarded to a contractor. It must be safe to forward."""
    auth = await owner(client)
    agent, connection = await connected_agent(client, auth)

    docs = await client.get(f"/agents/{agent['id']}/integration-docs", headers=auth)
    markdown = docs.json()["value"]["markdown"]

    assert "EAAG-test-token" not in markdown
    assert "test-app-secret" not in markdown
    assert connection["verifyToken"] not in markdown


async def test_an_agent_with_no_number_gets_no_whatsapp_section(
    client: AsyncClient, wired: FastAPI
) -> None:
    """Setup steps for a channel a tenant has not enabled would be noise."""
    auth = await owner(client)
    created = await client.post(
        "/agents",
        json={"name": f"Web only {uuid.uuid4().hex[:6]}", "persona": "Helpful."},
        headers=auth,
    )
    agent = created.json()["value"]

    docs = await client.get(f"/agents/{agent['id']}/integration-docs", headers=auth)

    assert "## WhatsApp" not in docs.json()["value"]["markdown"]


# -- the queue path --------------------------------------------------------------------


async def test_the_worker_answers_a_message_the_request_only_claimed(
    client: AsyncClient,
    wired: FastAPI,
    provider: FakeProvider,
    session: AsyncSession,
) -> None:
    """What a deployment does: claim in the request, answer in a worker.

    The task function is called directly rather than through Celery — a broker would be testing
    Celery, not this code, and the shell around this function is four lines that open a session.
    """
    auth = await owner(client)
    _, connection_id = await _claimed_only(client, auth, session, provider)

    record = (
        (await session.execute(select(WhatsAppMessage).where(WhatsAppMessage.body.is_not(None))))
        .scalars()
        .first()
    )
    assert record is not None
    # Compared by value, not identity: an `is` check here narrows the type for the rest of the
    # function, and the assertion after the worker runs would then be unreachable to a type checker.
    assert record.status.value == DeliveryStatus.RECEIVED.value

    await _run_worker(session, connection_id, record.id, provider)

    await session.refresh(record)
    assert record.status.value == DeliveryStatus.PROCESSED.value
    assert provider.sent_text == [(CONTACT, REPLY)]


async def test_a_task_that_runs_twice_does_not_answer_twice(
    client: AsyncClient,
    wired: FastAPI,
    provider: FakeProvider,
    session: AsyncSession,
) -> None:
    """`task_acks_late` means a task can be redelivered after its worker dies mid-flight."""
    auth = await owner(client)
    _, connection_id = await _claimed_only(client, auth, session, provider)

    record = (
        (await session.execute(select(WhatsAppMessage).where(WhatsAppMessage.body.is_not(None))))
        .scalars()
        .first()
    )
    assert record is not None

    await _run_worker(session, connection_id, record.id, provider)
    await _run_worker(session, connection_id, record.id, provider)

    assert len(provider.sent_text) == 1


async def test_a_task_for_a_deleted_connection_stops_quietly(
    session: AsyncSession, provider: FakeProvider
) -> None:
    """A tenant may disconnect while a message waits. That is a normal race, not a failure."""
    result = await tasks.process_inbound(
        session, uuid.uuid4(), uuid.uuid4(), {"providerMessageId": "x", "contactId": CONTACT}
    )

    assert result is None


# -- helpers ---------------------------------------------------------------------------


async def _claimed_only(
    client: AsyncClient, auth: dict[str, str], session: AsyncSession, provider: FakeProvider
) -> tuple[dict[str, object], uuid.UUID]:
    """Deliver a webhook, then rewind the claim so the message is waiting for a worker.

    Cheaper and clearer than standing up Redis: what the queue path adds is precisely the gap
    between claiming and answering, and this reproduces that gap exactly.

    The rewind has to undo *everything* the inline path did — the outbound row and the send itself
    — or the assertions that follow would be counting the inline reply as the worker's.
    """
    agent, connection = await connected_agent(client, auth)
    posted = await post_webhook(client, connection["id"], meta_webhook())
    assert posted.status_code == 200, posted.text

    rows = (
        (
            await session.execute(
                select(WhatsAppMessage).where(WhatsAppMessage.direction == MessageDirection.INBOUND)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "expected the delivery to have been claimed"
    for row in rows:
        row.status = DeliveryStatus.RECEIVED
    outbound = (
        (
            await session.execute(
                select(WhatsAppMessage).where(
                    WhatsAppMessage.direction == MessageDirection.OUTBOUND
                )
            )
        )
        .scalars()
        .all()
    )
    for row in outbound:
        await session.delete(row)
    await session.commit()

    provider.sent_text.clear()
    provider.sent_templates.clear()
    return agent, uuid.UUID(str(connection["id"]))


async def _run_worker(
    session: AsyncSession,
    connection_id: uuid.UUID,
    record_id: uuid.UUID,
    provider: FakeProvider,
) -> None:
    """Run the task's real body, with the provider and model substituted.

    ``tasks.process_inbound`` itself, not a copy of it — a test that reimplemented the worker would
    keep passing while the worker broke.
    """
    payload = tasks.to_payload(
        InboundMessage(
            provider_message_id="wamid.HBgMMjYzNzcwMDAwMDAwFQIAEhgg",
            contact_id=CONTACT,
            kind=InboundKind.TEXT,
            text="Do you have white emulsion in 20 litres?",
        )
    )
    await tasks.process_inbound(
        session,
        connection_id,
        record_id,
        payload,
        provider=provider,
        llm_client=RecordingLLM(reply=REPLY),
    )
    await session.commit()
