"""The web channel's origin allowlist: what counts as an origin, and what a listed one admits."""

from __future__ import annotations

import pytest

from src.modules.channels.internal.origins import (
    InvalidOriginError,
    configured,
    is_allowed,
    normalise,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://example.com", "https://example.com"),
        ("HTTPS://Shop.Example.COM/", "https://shop.example.com"),
        ("https://example.com:443", "https://example.com"),
        ("http://example.com:80", "http://example.com"),
        ("https://example.com:8443", "https://example.com:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("  https://example.com  ", "https://example.com"),
        ("http://[::1]:5173", "http://[::1]:5173"),
    ],
)
def test_an_origin_is_stored_in_the_shape_a_browser_sends(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "example.com",
        "ftp://example.com",
        "https://example.com/shop",
        "https://example.com?ref=1",
        "https://example.com#top",
        "https://user:secret@example.com",
        "https://",
        "https://example.com:99999",
        "null",
        "",
    ],
)
def test_anything_that_is_not_a_bare_origin_is_refused(raw: str) -> None:
    """A path is refused rather than trimmed: it would suggest a restriction that is not there."""
    with pytest.raises(InvalidOriginError):
        normalise(raw)


def test_a_listed_origin_admits_itself_and_nothing_that_merely_contains_it() -> None:
    allowed = ["https://example.com"]

    assert is_allowed("https://example.com", allowed)
    assert not is_allowed("https://example.com.attacker.test", allowed)
    assert not is_allowed("https://shop.example.com", allowed), "no implicit subdomains"
    assert not is_allowed("http://example.com", allowed), "the scheme is part of the origin"


def test_a_sandboxed_frame_is_never_allowed() -> None:
    """A sandboxed iframe or a file:// page sends the literal string ``null``."""
    assert not is_allowed("null", ["https://example.com"])


def test_a_malformed_stored_entry_is_skipped_rather_than_fatal() -> None:
    assert is_allowed("https://example.com", ["not an origin", "https://example.com"])


def test_the_allowlist_is_read_defensively_from_free_form_settings() -> None:
    assert configured(None) == []
    assert configured({}) == []
    assert configured({"allowedOrigins": "https://example.com"}) == []
    assert configured({"allowedOrigins": ["https://example.com"]}) == ["https://example.com"]
