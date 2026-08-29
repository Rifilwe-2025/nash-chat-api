"""What a Pattern B connector is (spec §5.2.1).

A connector pulls records from someone else's API and turns them into the same plain text a file
would have produced. It is **Pattern B — indexed content**: records are fetched on a schedule and
stored. Pattern A, the live tool call at query time, is a different mechanism with different
failure modes, and arrives in Phase 11.

The configuration a connector needs is the list §5.2.1 gives: endpoint, auth, pagination, and field
mapping — which JSON fields become content and which become metadata. All of it is data on the
source row, not code, so adding a tenant's product catalogue means filling in a form rather than
shipping a release.

``version`` on a record is what makes incremental sync possible. Where the API exposes a
``last_modified`` or a version field, the connector reports it and unchanged records are skipped; if
it exposes nothing, a hash of the content stands in, which achieves the same thing at the cost of
having fetched the record to discover it was unchanged.
"""

from __future__ import annotations

import enum
import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol


class AuthType(str, enum.Enum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY_HEADER = "api_key_header"
    BASIC = "basic"


class PaginationStyle(str, enum.Enum):
    """How to walk past the first page.

    ``NONE`` is a real option, not a placeholder: plenty of small catalogue endpoints return
    everything at once, and pretending otherwise would mean fetching page two of one page for ever.
    """

    NONE = "none"
    PAGE_NUMBER = "page"
    OFFSET = "offset"
    CURSOR = "cursor"
    LINK_HEADER = "link"


@dataclass(frozen=True, slots=True)
class ConnectorRecord:
    """One record pulled from the source API, already reduced to text and metadata."""

    external_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    version: str | None = None

    def fingerprint(self) -> str:
        """The version if the API gave one, else a hash of the content.

        Falling back to a hash keeps incremental sync working against APIs with no version field —
        the record still has to be fetched, but it does not have to be re-extracted or re-stored.
        """
        if self.version:
            return self.version
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ConnectorResult:
    records: list[ConnectorRecord]
    pages_fetched: int = 0
    truncated: bool = False


class ConnectorError(Exception):
    """The pull failed for a reason the tenant needs to see and fix.

    Expired credentials and a changed response shape are the two that actually happen (§5.2.1), and
    both are the tenant's to resolve — so the message is written for them, not for a log.
    """


class Connector(Protocol):
    async def fetch(self) -> ConnectorResult: ...
