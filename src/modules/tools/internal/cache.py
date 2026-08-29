"""Short-lived caching of identical tool calls (spec §5.2.1, "optional response caching").

A conversation asks the same question more than once: someone says "and my other order?", the model
re-checks, and a customer who is impatient sends the same message twice. Repeating an identical call
within a few seconds costs the tenant an API call and the customer a wait, for an answer that cannot
have changed.

**Off by default, and that is the important part.** ``cache_ttl_seconds`` is zero unless a tenant
sets it, because the data Pattern A exists for is per-customer and fast-changing — an order status
cached for five minutes is an order status that is wrong for five minutes. Caching is opt-in per
tool, so it applies to the lookups where a tenant knows staleness is acceptable (store hours,
shipping rates) and not to the ones where it is not.

**The key includes the tool id**, so two tenants' tools can never share an entry even if their
arguments match exactly. That is the isolation requirement (§5.7) applied to a cache, and it is the
reason the key is built here rather than from arguments alone.

In-process and per-worker, deliberately. Redis would make this shared, but a cache that outlives a
process is a cache that can serve one customer's order status to another after a deploy reorders
things — and the win it is chasing is measured in seconds. Entries are evicted lazily on read and
the map is bounded, so nothing needs a background sweep.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any

# Bounded so a busy agent cannot grow it without limit. Small on purpose: entries live for seconds,
# so a large map would mostly hold expired ones.
MAX_ENTRIES = 512


@dataclass(frozen=True, slots=True)
class _Entry:
    payload: Any
    status_code: int | None
    expires_at: float


class ResponseCache:
    """A tiny TTL map. One per process, pinned to ``app.state`` at startup."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def key(self, tool_id: uuid.UUID, arguments: dict[str, Any]) -> str:
        """A stable digest of "this tool, these arguments".

        Arguments are serialised with sorted keys so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}``
        are one entry — the model does not guarantee key order, and treating those as different
        calls would make the cache miss almost every time.
        """
        payload = json.dumps(arguments, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{tool_id}:{payload}".encode()).hexdigest()
        return digest

    def get(self, key: str) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        return entry

    def put(self, key: str, payload: Any, status_code: int | None, ttl_seconds: int) -> None:
        if ttl_seconds <= 0:
            return
        if len(self._entries) >= MAX_ENTRIES:
            self._evict()
        self._entries[key] = _Entry(
            payload=payload,
            status_code=status_code,
            expires_at=time.monotonic() + ttl_seconds,
        )

    def _evict(self) -> None:
        """Drop what has expired; if nothing has, drop the soonest to expire.

        Not an LRU: entries here live for seconds and are keyed by exact arguments, so recency of
        *use* says very little. Time to expiry is the honest ordering.
        """
        now = time.monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)

        if not expired and self._entries:
            soonest = min(self._entries, key=lambda key: self._entries[key].expires_at)
            self._entries.pop(soonest, None)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
