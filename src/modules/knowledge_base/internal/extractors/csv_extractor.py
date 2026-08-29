"""CSV and spreadsheets — rows become sentences, not rows (spec §5.2.3).

This is the one extractor whose output deliberately differs from its input. A raw CSV row injected
into a prompt reads as noise:

    SKU123,45.99,Blue,In stock

The same row as a sentence carries the column meanings with it, which is what the model actually
needs::

    Product SKU123: price is 45.99, colour is Blue, availability is In stock.

The first column is treated as the subject of the sentence when it looks like an identifier, so the
row leads with the thing it describes rather than with a bare field name.
"""

from __future__ import annotations

import csv
import io

from src.modules.knowledge_base.internal.extractors.base import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
)
from src.modules.knowledge_base.internal.extractors.text_extractor import decode

# Beyond this a CSV is a database export, not knowledge — and Tier 1 could never inject it. The
# extraction is truncated rather than rejected so a large catalogue still yields usable knowledge.
MAX_ROWS = 5_000


def _sniff_dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel  # a single-column file gives the sniffer nothing to go on


def _sentence(header: list[str], row: list[str]) -> str:
    pairs = [
        (column.strip(), value.strip())
        for column, value in zip(header, row, strict=False)
        if value.strip()
    ]
    if not pairs:
        return ""

    subject_column, subject_value = pairs[0]
    rest = pairs[1:]
    if not rest:
        return f"{subject_column} is {subject_value}."

    details = ", ".join(f"{column} is {value}" for column, value in rest)
    return f"{subject_column} {subject_value}: {details}."


class CsvExtractor:
    def __init__(self, max_rows: int = MAX_ROWS) -> None:
        self._max_rows = max_rows

    async def extract(self, content: ExtractedContent) -> ExtractionResult:
        text = decode(content.data)
        if not text.strip():
            raise ExtractionError("The file is empty.")

        reader = csv.reader(io.StringIO(text, newline=""), _sniff_dialect(text[:4096]))
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ExtractionError("The file has no header row.") from exc

        if not any(column.strip() for column in header):
            raise ExtractionError("The first row must name the columns.")

        sentences: list[str] = []
        truncated = False
        for row in reader:
            if len(sentences) >= self._max_rows:
                truncated = True
                break
            sentence = _sentence(header, row)
            if sentence:
                sentences.append(sentence)

        if not sentences:
            raise ExtractionError("The file has a header but no data rows.")

        rendered = "\n".join(sentences)
        return ExtractionResult(
            text=rendered,
            metadata={
                "format": "csv",
                "columns": [column.strip() for column in header],
                "rows": len(sentences),
                "truncated": truncated,
                "characters": len(rendered),
            },
        )
