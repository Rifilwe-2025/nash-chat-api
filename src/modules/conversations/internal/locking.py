"""Serialising the turns in one conversation (spec §5.4: message queueing).

Someone typing "hi", "are you there", "hello??" in three seconds must not produce three turns racing
each other. Each would read the same history, none would see the others' messages, and the
transcript would end up interleaved nonsense — with three provider calls billed for it.

A **Postgres advisory lock** is used rather than an in-process lock because the API runs as more
than one worker: an `asyncio.Lock` would only order the messages that happened to land on the same
process. The transaction-scoped variant releases on commit or rollback, so a crashed turn cannot
wedge a conversation.

This is ordering, not queueing. Phase 9 introduces the real queue and moves turns off the request
path entirely; until then a second message waits for the first, which is the behaviour that matters
and is cheap to get right now.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Namespaces the lock space so a conversation id cannot collide with some other feature's advisory
# lock. Arbitrary, but fixed: changing it would stop new turns from seeing old locks.
LOCK_NAMESPACE = 0x4B42  # "KB"


def _lock_key(conversation_id: uuid.UUID) -> int:
    """Fold a UUID into the signed 32-bit space ``pg_advisory_xact_lock(int, int)`` accepts."""
    return (conversation_id.int & 0x7FFFFFFF) - 0x40000000


async def lock_conversation(session: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Block until this conversation is ours for the rest of the transaction.

    Deliberately the blocking form rather than ``try_advisory_lock``: a rapid second message should
    be answered a moment late, not dropped.
    """
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
        {"namespace": LOCK_NAMESPACE, "key": _lock_key(conversation_id)},
    )
