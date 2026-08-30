"""Encryption at rest for tenant credentials (spec §5.7, Phase 13).

The property that matters is not "the function round-trips" — it is that a credential written
through the ORM is **not readable in the column**, and that turning the key on does not strand the
rows written before it. Both are checked against the real database here rather than in isolation,
because the column type is where the protection lives.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tools.domain.models import AgentTool, ToolAuthType
from src.shared.crypto import EncryptionError, decrypt, encrypt, encryption_enabled, generate_key
from tests.modules.analytics.helpers import make_agent

KEY = generate_key()
OTHER_KEY = generate_key()
SECRET = "sk_live_this_must_never_be_readable"


@pytest.fixture
def encrypted(config_override: Callable[..., None]) -> None:
    config_override(SECURITY_ENCRYPTION_KEY=KEY)


# -- the cipher ----------------------------------------------------------------------


def test_a_value_round_trips(encrypted: None) -> None:
    sealed = encrypt(SECRET)

    assert sealed != SECRET
    assert sealed.startswith("v1:")
    assert decrypt(sealed) == SECRET


def test_the_same_value_encrypts_differently_every_time(encrypted: None) -> None:
    """A fresh nonce per encryption. Identical ciphertexts would leak that two rows match."""
    assert encrypt(SECRET) != encrypt(SECRET)


def test_a_tampered_ciphertext_will_not_decrypt(encrypted: None) -> None:
    """AES-GCM authenticates: altered input fails rather than decrypting to something else."""
    sealed = encrypt(SECRET)
    tampered = sealed[:-6] + ("A" if sealed[-6] != "A" else "B") + sealed[-5:]

    with pytest.raises(EncryptionError):
        decrypt(tampered)


def test_the_wrong_key_will_not_decrypt(
    config_override: Callable[..., None], encrypted: None
) -> None:
    sealed = encrypt(SECRET)
    config_override(SECURITY_ENCRYPTION_KEY=OTHER_KEY)

    with pytest.raises(EncryptionError):
        decrypt(sealed)


def test_an_unusable_key_is_an_error_rather_than_silence(
    config_override: Callable[..., None],
) -> None:
    """A wrong key must be loud: an empty credential would look like an unconfigured one."""
    config_override(SECURITY_ENCRYPTION_KEY="not-base64!!")

    with pytest.raises(EncryptionError):
        encrypt(SECRET)


def test_with_no_key_the_value_passes_through() -> None:
    """Local development and the test suite run unencrypted, and are honest about it."""
    assert not encryption_enabled()
    assert encrypt(SECRET) == SECRET
    assert decrypt(SECRET) == SECRET


def test_a_value_written_before_the_key_is_still_readable(encrypted: None) -> None:
    """Turning encryption on is a configuration change, not a data migration."""
    assert decrypt("plain-text-from-before") == "plain-text-from-before"


def test_encrypted_data_cannot_be_read_after_the_key_is_lost(
    config_override: Callable[..., None], encrypted: None
) -> None:
    sealed = encrypt(SECRET)
    config_override(SECURITY_ENCRYPTION_KEY="")

    with pytest.raises(EncryptionError):
        decrypt(sealed)


# -- the column ----------------------------------------------------------------------


async def test_a_tool_credential_is_unreadable_in_the_column(
    session: AsyncSession, encrypted: None
) -> None:
    """The point of the whole exercise: a database dump does not hand over the tenant's API key."""
    tenant_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenant (id, name, plan) VALUES (:id, 'Acme', 'free')"),
        {"id": tenant_id},
    )
    agent = await make_agent(session, tenant_id)

    tool = AgentTool(
        tenant_id=tenant_id,
        agent_id=agent.id,
        name="check_order_status",
        description="Look up an order.",
        endpoint_url="https://api.example.test/orders",
        auth_type=ToolAuthType.BEARER,
        auth_config_json={"value": SECRET},
    )
    session.add(tool)
    await session.flush()
    session.expunge(tool)

    raw = (
        await session.execute(
            text("SELECT auth_config_json::text FROM agent_tool WHERE id = :id"), {"id": tool.id}
        )
    ).scalar_one()
    assert SECRET not in raw
    assert "v1:" in raw

    # And the application still sees the credential it stored.
    loaded = await session.get(AgentTool, tool.id)
    assert loaded is not None
    assert loaded.auth_config_json == {"value": SECRET}


async def test_an_empty_credential_stays_an_empty_object(
    session: AsyncSession, encrypted: None
) -> None:
    """ "No credential configured" is not a secret, and must not look like one that is."""
    tenant_id = uuid.uuid4()
    await session.execute(
        text("INSERT INTO tenant (id, name, plan) VALUES (:id, 'Acme', 'free')"),
        {"id": tenant_id},
    )
    agent = await make_agent(session, tenant_id)

    tool = AgentTool(
        tenant_id=tenant_id,
        agent_id=agent.id,
        name="public_lookup",
        description="No credential needed.",
        endpoint_url="https://api.example.test/public",
    )
    session.add(tool)
    await session.flush()

    raw = (
        await session.execute(
            text("SELECT auth_config_json::text FROM agent_tool WHERE id = :id"), {"id": tool.id}
        )
    ).scalar_one()
    assert raw == "{}"
