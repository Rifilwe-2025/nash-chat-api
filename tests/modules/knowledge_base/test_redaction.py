"""Opt-in PII redaction (spec §5.7, Phase 13).

Two halves. The pattern tests pin down what is found and — just as importantly — what is *not*:
redaction that eats order references is redaction a tenant turns off, and an agent that cannot
answer "where is order 4539148803436467" is worse than one that repeats a phone number.

The ingestion test is the one that matters for the security claim: with the flag on, the personal
details are gone from what is **stored**, not merely from what is returned.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from src.modules.knowledge_base.internal.redaction import redact
from tests.modules.auth.test_auth_flow import auth_header, signup

DOCUMENT = (
    "Contact Ada at ada@example.com or on +263 77 123 4567.\n"
    "Her card on file is 4539 1488 0343 6467 and the order reference is 4539148803436460.\n"
    "Escalations go to support@nashpaints.co.zw."
)


# -- what the patterns find ----------------------------------------------------------


def test_emails_phones_and_cards_are_replaced() -> None:
    result = redact(DOCUMENT)

    assert "ada@example.com" not in result.text
    assert "support@nashpaints.co.zw" not in result.text
    assert "+263 77 123 4567" not in result.text
    assert "4539 1488 0343 6467" not in result.text
    assert result.counts == {"card": 1, "email": 2, "phone": 1}


def test_a_number_that_fails_the_luhn_check_survives() -> None:
    """An order reference is not a card, and an agent is asked about those constantly."""
    result = redact("Your order reference is 4539148803436460.")

    assert "4539148803436460" in result.text
    assert result.counts == {}


def test_text_with_nothing_to_redact_is_returned_unchanged() -> None:
    original = "Matte emulsion covers 12 square metres per litre."

    assert redact(original).text == original


def test_a_short_number_is_not_treated_as_a_phone_number() -> None:
    """Prices, quantities and years must survive: they are most of what a catalogue says."""
    result = redact("The 5 litre tin costs 45 dollars and covers 60 square metres.")

    assert result.counts == {}


def test_the_placeholders_say_what_was_removed() -> None:
    """A reader of the stored text should be able to tell a redaction from a gap in the document."""
    result = redact("Reach me at ada@example.com")

    assert "[redacted:email]" in result.text


# -- what is stored ------------------------------------------------------------------


async def create_kb(client: AsyncClient, auth: dict[str, str], redact_pii: bool) -> str:
    response = await client.post(
        "/knowledge-bases",
        json={"name": f"KB {redact_pii}", "redactPii": redact_pii},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    kb: dict[str, Any] = response.json()["value"]
    assert kb["redactPii"] is redact_pii
    return str(kb["id"])


async def add_source(client: AsyncClient, auth: dict[str, str], kb_id: str) -> dict[str, Any]:
    response = await client.post(
        f"/knowledge-bases/{kb_id}/sources/manual",
        json={"title": "Contacts", "body": DOCUMENT},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


async def test_ingestion_stores_the_redacted_text(client: AsyncClient) -> None:
    """Redaction happens on the way in, so the raw values never reach the database at all."""
    auth = auth_header((await signup(client))["tokens"])
    kb_id = await create_kb(client, auth, redact_pii=True)

    source = await add_source(client, auth, kb_id)

    stored = source["extractedText"]
    assert "ada@example.com" not in stored
    assert "[redacted:email]" in stored
    assert "[redacted:phone]" in stored


async def test_ingestion_keeps_everything_when_redaction_is_off(client: AsyncClient) -> None:
    """Off by default, because redaction is lossy and most knowledge bases need the details."""
    auth = auth_header((await signup(client))["tokens"])
    kb_id = await create_kb(client, auth, redact_pii=False)

    source = await add_source(client, auth, kb_id)

    assert "ada@example.com" in source["extractedText"]


async def test_the_source_records_what_was_removed(client: AsyncClient) -> None:
    """So a tenant can notice they enabled this on a knowledge base that needed the details."""
    auth = auth_header((await signup(client))["tokens"])
    kb_id = await create_kb(client, auth, redact_pii=True)

    source = await add_source(client, auth, kb_id)

    assert source["metadata"]["redacted"] == {"card": 1, "email": 2, "phone": 1}


async def test_redaction_can_be_turned_on_after_the_fact(client: AsyncClient) -> None:
    """The flag is editable, and applies to what is ingested next — never retroactively."""
    auth = auth_header((await signup(client))["tokens"])
    kb_id = await create_kb(client, auth, redact_pii=False)
    before = await add_source(client, auth, kb_id)

    updated = await client.patch(
        f"/knowledge-bases/{kb_id}", json={"redactPii": True}, headers=auth
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["value"]["redactPii"] is True

    after = await add_source(client, auth, kb_id)

    assert "ada@example.com" in before["extractedText"]
    assert "ada@example.com" not in after["extractedText"]
