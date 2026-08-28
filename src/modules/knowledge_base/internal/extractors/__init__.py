"""Type detection and dispatch — one extractor per format (spec §5.2.3).

The upload path knows a filename and a browser-supplied content type, and neither can be trusted on
its own: browsers send ``application/octet-stream`` for anything they do not recognise, and a
tenant can rename a file to anything. So the extension decides when it is recognised, and the
declared media type is the fallback.

Whatever the format, the pipeline shape is the same: detect, extract, store text plus metadata. The
tier routing that follows is Phase 6.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from src.modules.knowledge_base.internal.extractors.base import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
    Extractor,
)
from src.modules.knowledge_base.internal.extractors.csv_extractor import CsvExtractor
from src.modules.knowledge_base.internal.extractors.docx_extractor import DocxExtractor
from src.modules.knowledge_base.internal.extractors.html_extractor import (
    HtmlExtractor,
    extract_html,
)
from src.modules.knowledge_base.internal.extractors.llm_file_extractor import LlmFileExtractor
from src.modules.knowledge_base.internal.extractors.text_extractor import TextExtractor

# Extension → canonical media type. This is also the allowlist: a file whose extension is not here
# is rejected at upload rather than guessed at.
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_EXTRACTORS: dict[str, Extractor] = {
    "text/plain": TextExtractor(),
    "text/markdown": TextExtractor(),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": DocxExtractor(),
    "text/csv": CsvExtractor(),
    "text/tab-separated-values": CsvExtractor(),
    "text/html": HtmlExtractor(),
}

# Formats with no extraction library in v1: the model reads them (spec §5.2.3).
LLM_READ_TYPES = frozenset(
    {"application/pdf", "image/png", "image/jpeg", "image/webp", "image/gif"}
)


def media_type_for(filename: str | None, declared: str | None = None) -> str:
    """Resolve the media type this file will be extracted as.

    Raises :class:`ExtractionError` when the format is not one v1 supports — the caller turns that
    into a 422 at upload time, before any bytes are stored.
    """
    if filename:
        suffix = PurePosixPath(filename).suffix.lower()
        if suffix in SUPPORTED_EXTENSIONS:
            return SUPPORTED_EXTENSIONS[suffix]

    normalised = (declared or "").split(";")[0].strip().lower()
    if normalised in _EXTRACTORS or normalised in LLM_READ_TYPES:
        return normalised

    supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
    raise ExtractionError(f"Unsupported file type. Supported extensions: {supported}.")


def get_extractor(media_type: str, llm_extractor: Extractor | None = None) -> Extractor:
    """The handler for a resolved media type.

    ``llm_extractor`` is injectable so the ingestion service can pass a client it controls, and so
    tests can exercise the PDF/image path without calling a provider.
    """
    if media_type in LLM_READ_TYPES:
        return llm_extractor or LlmFileExtractor()
    extractor = _EXTRACTORS.get(media_type)
    if extractor is None:  # pragma: no cover - media_type_for rejects these first
        raise ExtractionError(f"No extractor is registered for {media_type}.")
    return extractor


__all__ = [
    "LLM_READ_TYPES",
    "SUPPORTED_EXTENSIONS",
    "CsvExtractor",
    "DocxExtractor",
    "ExtractedContent",
    "ExtractionError",
    "ExtractionResult",
    "Extractor",
    "HtmlExtractor",
    "LlmFileExtractor",
    "TextExtractor",
    "extract_html",
    "get_extractor",
    "media_type_for",
]
