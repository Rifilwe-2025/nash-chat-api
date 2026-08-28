"""Plain text and Markdown — used as-is (spec §5.2.3).

The only real work is decoding. Tenants upload files saved by Windows tooling often enough that
assuming UTF-8 and raising on anything else would reject perfectly readable documents, so the
common encodings are tried in turn before giving up.
"""

from __future__ import annotations

from src.modules.knowledge_base.internal.extractors.base import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
)

ENCODINGS = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def decode(data: bytes) -> str:
    for encoding in ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExtractionError("The file is not readable as text in any supported encoding.")


class TextExtractor:
    """``.txt`` / ``.md``: no processing beyond decoding and normalising line endings."""

    async def extract(self, content: ExtractedContent) -> ExtractionResult:
        text = decode(content.data).replace("\r\n", "\n").strip()
        if not text:
            raise ExtractionError("The file is empty.")
        return ExtractionResult(
            text=text,
            metadata={"format": "text", "characters": len(text)},
        )
