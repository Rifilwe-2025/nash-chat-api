"""What every extractor produces, and how one fails.

An extractor turns submitted bytes into readable plain text plus the metadata that describes where
the text came from. It never decides *how* the text will be used — tier routing and prompt assembly
are Phases 6 and 7 — and it never writes to the database.

Failures raise :class:`ExtractionError`, which the service records on the source as an error the
tenant can read. A password-protected PDF or a 404 URL is normal input, not a bug: it must surface
as a failed source, never as a 500 (the phase's "done when" bar).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ExtractionError(Exception):
    """A source could not be read. The message is shown to the tenant, so keep it plain."""


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """The bytes a source arrived as, before any format-specific handling."""

    data: bytes
    media_type: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Structured plain text plus the metadata stored on ``kb_source.config_json`` (spec §5.2.3)."""

    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Extractor(Protocol):
    """Every format handler is one of these.

    Async because two of them do I/O — the URL fetcher and the LLM file reader — and a caller
    should not have to know which.
    """

    async def extract(self, content: ExtractedContent) -> ExtractionResult: ...
