"""The error catalogue must stay complete (Phase 13, spec §10).

A caller branches on ``error.code``, so an undocumented code is an undocumented branch. This scans
the source for every code the application raises and fails when one is missing from the catalogue —
which means a new failure mode gets documented in the change that introduces it, rather than by
whoever hits it in production.
"""

from __future__ import annotations

import re
from pathlib import Path

from httpx import AsyncClient

from src.core.error_catalogue import ALL_CODES, render

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"

# `code="SOMETHING"` — how every AppException subclass is given its code.
RAISED = re.compile(r'code="([A-Z][A-Z0-9_]+)"')


def codes_in_source() -> set[str]:
    return {
        match
        for path in SOURCE_ROOT.rglob("*.py")
        for match in RAISED.findall(path.read_text(encoding="utf-8"))
    }


def test_every_code_raised_in_the_source_is_documented() -> None:
    undocumented = sorted(codes_in_source() - set(ALL_CODES))

    assert undocumented == [], (
        "These error codes are raised but not in src/core/error_catalogue.py: "
        f"{undocumented}. A caller branches on the code, so it is part of the contract."
    )


def test_the_catalogue_documents_nothing_that_no_longer_exists() -> None:
    """A stale entry is a promise the API no longer keeps.

    The base classes' own defaults are exempt: ``AppException`` and its subclasses carry their codes
    as class attributes rather than as a ``code="…"`` argument, so they never appear in the scan.
    """
    from_base_classes = {
        "INTERNAL_ERROR",
        "BAD_REQUEST",
        "VALIDATION_ERROR",
        "UNAUTHORIZED",
        "FORBIDDEN",
        "NOT_FOUND",
        "CONFLICT",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
    }

    stale = sorted(set(ALL_CODES) - codes_in_source() - from_base_classes)

    assert stale == [], f"Documented but no longer raised anywhere: {stale}"


def test_the_catalogue_renders_into_the_published_description() -> None:
    catalogue = render()

    assert "| `AGENT_NOT_FOUND` |" in catalogue
    assert "## Error codes" in catalogue


async def test_the_schema_carries_the_catalogue(client: AsyncClient) -> None:
    """§10's bar is integration without support, and this is the page that has to carry it."""
    schema = (await client.get("/openapi.json")).json()

    description = schema["info"]["description"]
    assert "## Error codes" in description
    assert "`PROVIDER_UNAVAILABLE`" in description
    assert "X-RateLimit-Limit" in description
