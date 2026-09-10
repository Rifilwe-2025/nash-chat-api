"""Rendering a generated document as a PDF.

The value being protected here is not typography, it is that the whole document survives the trip:
every construct the integration guide uses renders, the text stays text rather than becoming a
picture, and a character the bundled fonts cannot draw does not take the endpoint down with it.
"""

from __future__ import annotations

from src.shared.documents import markdown_pdf

GUIDE = "\n".join(
    [
        "# Integrating Sales Assistant",
        "",
        "Agent id `abc-123` · base URL `https://api.example.com` · key `nsk_live_7Fq…`",
        "",
        "## Quickstart",
        "",
        "Prose with **bold**, *italic*, `code_inline` and a [link](https://example.com).",
        "",
        "```bash",
        'curl -X POST https://api.example.com/v1/chat/messages -H "Authorization: Bearer $KEY"',
        "```",
        "",
        "- a bullet",
        "- another bullet",
        "",
        "1. step one",
        "2. step two",
        "",
        "| Code | Meaning |",
        "|---|---|",
        "| `RATE_LIMITED` | Slow down; see `Retry-After`. |",
        "",
        "> An aside worth pulling out.",
        "",
        "---",
        "",
        "### A closing subsection",
    ]
)


def test_the_whole_guide_renders_to_a_pdf() -> None:
    pdf = markdown_pdf.render(GUIDE, title="Integrating Sales Assistant")

    assert pdf.startswith(b"%PDF-")
    assert pdf.rstrip().endswith(b"%%EOF")
    assert len(pdf) > 4000, "a document this long is not a near-empty page"


def test_the_text_stays_text() -> None:
    """A PDF of a picture of a document is searchable by nobody and copy-pasteable by nobody.

    Embedded fonts carry a ToUnicode map, which is what lets a reader select the code out of a
    snippet — the single most likely thing anybody does with this file.
    """
    pdf = markdown_pdf.render(GUIDE, title="Integrating Sales Assistant")

    assert b"/ToUnicode" in pdf
    # reportlab lists /ImageB and /ImageC in every page's ProcSet, so what would give a
    # scanned-looking document away is an image XObject.
    assert b"/Subtype /Image" not in pdf


def test_a_character_the_fonts_cannot_draw_does_not_break_the_render() -> None:
    """An agent's name is tenant-authored, so the document may contain anything at all."""
    pdf = markdown_pdf.render("# Agent \U0001f600 中文\n\nBody \U0001f680 text.", title="Agent")

    assert pdf.startswith(b"%PDF-")


def test_typographic_characters_survive_as_themselves_or_as_an_equivalent() -> None:
    assert (
        markdown_pdf._drawable("an em dash — and an ellipsis…") == "an em dash — and an ellipsis…"
    )
    # Courier encodes Windows-1252 and raises outside it, so code is substituted rather than lost.
    assert markdown_pdf._drawable("arrow → here", mono=True) == "arrow -> here"
    assert markdown_pdf._drawable("emoji \U0001f600", mono=True) == "emoji ?"


def test_a_long_code_line_is_folded_rather_than_running_off_the_page() -> None:
    """A code block cannot reflow itself, so anything past the frame is simply lost."""
    line = "curl -X POST https://api.example.com/v1/chat/messages " + "-H x " * 40

    folded = markdown_pdf._wrapped_code(line)

    assert len(folded.split("\n")) > 1
    assert all(len(part) <= markdown_pdf.CODE_WIDTH for part in folded.split("\n"))


def test_a_short_code_line_is_left_exactly_as_written() -> None:
    source = "alembic upgrade head\npython main.py"

    assert markdown_pdf._wrapped_code(source) == source


def test_markdown_it_cannot_render_becomes_its_own_text() -> None:
    """A construct outside the supported subset degrades; it never raises."""
    pdf = markdown_pdf.render("Term\n: definition\n\n<div>raw html</div>\n", title="Odd")

    assert pdf.startswith(b"%PDF-")
