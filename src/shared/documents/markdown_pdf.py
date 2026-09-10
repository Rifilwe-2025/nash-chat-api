"""Rendering a generated Markdown document as a PDF (spec §5.6).

The integration guide is generated as Markdown, which is what a developer wants. A PDF is what gets
attached to an email or handed to somebody's procurement department, so the same document has to
come out as one — with its text still *text*: selectable, searchable and copy-pasteable, rather than
a picture of a page.

Deliberately a small subset of Markdown: headings, paragraphs with inline code, emphasis and links,
bullet and numbered lists, fenced code, tables, block quotes and rules. That is everything
:mod:`src.modules.channels.internal.integration_docs` emits, and anything outside it degrades to its
own text rather than failing — a document that renders imperfectly beats an endpoint that 500s.

reportlab rather than an HTML-to-PDF engine: those need a headless browser or Pango and a set of
system libraries a managed Python host may not have. This is pure Python and draws with fonts it
ships, so the same bytes come out of a laptop and a container.
"""

from __future__ import annotations

import io
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import reportlab
from markdown_it import MarkdownIt
from markdown_it.token import Token
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    ListFlowable,
    ListItem,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)

FONT_DIR = Path(reportlab.__file__).parent / "fonts"

BODY = "NashSans"
BODY_BOLD = "NashSans-Bold"
BODY_ITALIC = "NashSans-Italic"
#: One of the standard 14 fonts, so no file is needed for the code voice.
MONO = "Courier"

BRAND = colors.HexColor("#E80706")
INK = colors.HexColor("#1A1A1A")
MUTED = colors.HexColor("#6B6B6B")
RULE = colors.HexColor("#DDDDDD")
CODE_BACKGROUND = colors.HexColor("#F5F3F0")
LINK_COLOUR = "#0B5FFF"

MARGIN = 20 * mm
TEXT_WIDTH = A4[0] - 2 * MARGIN
#: Characters of Courier 8pt that fit the text frame, so a long line wraps rather than running off.
CODE_WIDTH = 92

#: Characters worth keeping in a recognisable form when a font cannot draw them.
REPLACEMENTS = {
    "…": "...",
    "—": "-",
    "–": "-",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "→": "->",
    "←": "<-",
    "·": "-",
    "•": "*",
    "×": "x",
    " ": " ",
}


def _register_fonts() -> None:
    """Register the bundled faces, once per process."""
    if BODY in pdfmetrics.getRegisteredFontNames():
        return
    pdfmetrics.registerFont(TTFont(BODY, str(FONT_DIR / "Vera.ttf")))
    pdfmetrics.registerFont(TTFont(BODY_BOLD, str(FONT_DIR / "VeraBd.ttf")))
    pdfmetrics.registerFont(TTFont(BODY_ITALIC, str(FONT_DIR / "VeraIt.ttf")))
    pdfmetrics.registerFontFamily(
        BODY, normal=BODY, bold=BODY_BOLD, italic=BODY_ITALIC, boldItalic=BODY_BOLD
    )


def _drawable(text: str, *, mono: bool = False) -> str:
    """Text these fonts can actually draw.

    An agent's name is tenant-authored and a knowledge base can be about anything, so a document may
    contain characters neither face has a glyph for. A missing glyph draws as a blank box, or raises
    outright for the standard fonts, so it is substituted here: something readable where there is an
    obvious equivalent, a question mark where there is not.
    """
    if mono:
        # The standard fonts encode Windows-1252 and raise on anything outside it.
        substituted = "".join(REPLACEMENTS.get(character, character) for character in text)
        return substituted.encode("cp1252", errors="replace").decode("cp1252")

    glyphs = pdfmetrics.getFont(BODY).face.charToGlyph
    drawn: list[str] = []
    for character in text:
        if ord(character) in glyphs:
            drawn.append(character)
            continue
        substitute = REPLACEMENTS.get(character, "?")
        drawn.append("".join(c if ord(c) in glyphs else "?" for c in substitute))
    return "".join(drawn)


def _escaped(text: str) -> str:
    """Drawable, and safe inside reportlab's paragraph markup."""
    return _drawable(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles() -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "body", fontName=BODY, fontSize=9.5, leading=14, textColor=INK, spaceAfter=8
    )
    return {
        "body": body,
        "h1": ParagraphStyle(
            "h1", parent=body, fontName=BODY_BOLD, fontSize=19, leading=24, spaceAfter=2
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=body,
            fontName=BODY_BOLD,
            fontSize=13,
            leading=17,
            spaceBefore=16,
            spaceAfter=2,
        ),
        "h3": ParagraphStyle(
            "h3",
            parent=body,
            fontName=BODY_BOLD,
            fontSize=10.5,
            leading=14,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "code": ParagraphStyle(
            "code",
            fontName=MONO,
            fontSize=8,
            leading=11,
            textColor=INK,
            backColor=CODE_BACKGROUND,
            borderPadding=6,
            spaceBefore=2,
            spaceAfter=10,
        ),
        "th": ParagraphStyle(
            "th", parent=body, fontName=BODY_BOLD, fontSize=8.5, leading=11.5, spaceAfter=0
        ),
        "td": ParagraphStyle("td", parent=body, fontSize=8.5, leading=11.5, spaceAfter=0),
        "quote": ParagraphStyle(
            "quote", parent=body, fontName=BODY_ITALIC, textColor=MUTED, leftIndent=8
        ),
    }


def _inline(token: Token | None) -> str:
    """One inline run — text, code, emphasis and links — as paragraph markup."""
    if token is None:
        return ""
    if not token.children:
        return _escaped(token.content)

    parts: list[str] = []
    for child in token.children:
        if child.type == "text":
            parts.append(_escaped(child.content))
        elif child.type == "code_inline":
            parts.append(f'<font face="{MONO}" size="8.5">{_escaped(child.content)}</font>')
        elif child.type in ("strong_open", "strong_close"):
            parts.append("<b>" if child.type == "strong_open" else "</b>")
        elif child.type in ("em_open", "em_close"):
            parts.append("<i>" if child.type == "em_open" else "</i>")
        elif child.type == "link_open":
            href = _escaped(str(child.attrGet("href") or ""))
            parts.append(f'<link href="{href}" color="{LINK_COLOUR}">')
        elif child.type == "link_close":
            parts.append("</link>")
        elif child.type == "hardbreak":
            parts.append("<br/>")
        elif child.type == "softbreak":
            parts.append(" ")
        elif child.content:
            parts.append(_escaped(child.content))
    return "".join(parts)


def _wrapped_code(source: str) -> str:
    """Code with long lines folded, since a code block cannot reflow itself."""
    lines: list[str] = []
    for line in source.rstrip("\n").split("\n"):
        drawn = _drawable(line.replace("\t", "    "), mono=True)
        if len(drawn) <= CODE_WIDTH:
            lines.append(drawn)
            continue
        lines.extend(
            textwrap.wrap(
                drawn,
                width=CODE_WIDTH,
                subsequent_indent="  ",
                break_long_words=True,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return "\n".join(lines)


def _column_widths(columns: int) -> list[float]:
    if columns == 2:
        # Every two-column table in these documents is "name, what it means".
        return [TEXT_WIDTH * 0.34, TEXT_WIDTH * 0.66]
    return [TEXT_WIDTH / columns] * columns


def _table(
    tokens: list[Token], index: int, flowables: list[Flowable], styles: dict[str, ParagraphStyle]
) -> int:
    rows: list[list[Flowable]] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].type != "table_close":
        if tokens[cursor].type != "tr_open":
            cursor += 1
            continue

        cells: list[Flowable] = []
        cursor += 1
        while cursor < len(tokens) and tokens[cursor].type != "tr_close":
            if tokens[cursor].type in ("th_open", "td_open"):
                header = tokens[cursor].type == "th_open"
                cells.append(
                    Paragraph(_inline(tokens[cursor + 1]), styles["th" if header else "td"])
                )
                cursor += 3
            else:
                cursor += 1
        rows.append(cells)
        cursor += 1

    if rows:
        table = Table(rows, hAlign="LEFT", repeatRows=1, colWidths=_column_widths(len(rows[0])))
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, RULE),
                    ("BACKGROUND", (0, 0), (-1, 0), CODE_BACKGROUND),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        flowables.append(table)
        flowables.append(Spacer(1, 10))
    return cursor + 1


def _listing(
    tokens: list[Token], index: int, flowables: list[Flowable], styles: dict[str, ParagraphStyle]
) -> int:
    ordered = tokens[index].type == "ordered_list_open"
    closing = "ordered_list_close" if ordered else "bullet_list_close"

    items: list[ListItem] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].type != closing:
        if tokens[cursor].type != "list_item_open":
            cursor += 1
            continue
        inner: list[Flowable] = []
        cursor += 1
        while cursor < len(tokens) and tokens[cursor].type != "list_item_close":
            cursor = _block(tokens, cursor, inner, styles)
        items.append(ListItem(inner or [Paragraph("", styles["body"])], leftIndent=12))
        cursor += 1

    if items:
        options: dict[str, Any] = {
            "bulletFontName": BODY,
            "bulletFontSize": 8,
            "leftIndent": 14,
            "spaceBefore": 2,
            "spaceAfter": 8,
        }
        if ordered:
            options |= {"bulletType": "1", "start": "1"}
        else:
            options |= {"bulletType": "bullet"}
        flowables.append(ListFlowable(items, **options))
    return cursor + 1


def _quote(
    tokens: list[Token], index: int, flowables: list[Flowable], styles: dict[str, ParagraphStyle]
) -> int:
    inner: list[Flowable] = []
    cursor = index + 1
    while cursor < len(tokens) and tokens[cursor].type != "blockquote_close":
        cursor = _block(tokens, cursor, inner, styles)

    quoted = Table([[inner]], colWidths=[TEXT_WIDTH], hAlign="LEFT")
    quoted.setStyle(
        TableStyle(
            [
                ("LINEBEFORE", (0, 0), (0, -1), 2, BRAND),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )
    flowables.append(quoted)
    flowables.append(Spacer(1, 8))
    return cursor + 1


def _block(
    tokens: list[Token], index: int, flowables: list[Flowable], styles: dict[str, ParagraphStyle]
) -> int:
    """Render one block, and return where the next one starts."""
    token = tokens[index]

    if token.type == "heading_open":
        level = int(token.tag[1:] or 3)
        style = styles.get(f"h{level}", styles["h3"])
        flowables.append(Paragraph(_inline(tokens[index + 1]), style))
        if level <= 2:
            flowables.append(
                HRFlowable(
                    width="100%",
                    thickness=1.2 if level == 1 else 0.5,
                    color=BRAND if level == 1 else RULE,
                    spaceBefore=3,
                    spaceAfter=9,
                )
            )
        return index + 3

    if token.type == "paragraph_open":
        flowables.append(Paragraph(_inline(tokens[index + 1]), styles["body"]))
        return index + 3

    if token.type in ("fence", "code_block"):
        flowables.append(Preformatted(_wrapped_code(token.content), styles["code"]))
        return index + 1

    if token.type in ("bullet_list_open", "ordered_list_open"):
        return _listing(tokens, index, flowables, styles)

    if token.type == "table_open":
        return _table(tokens, index, flowables, styles)

    if token.type == "blockquote_open":
        return _quote(tokens, index, flowables, styles)

    if token.type == "hr":
        flowables.append(
            HRFlowable(width="100%", thickness=0.5, color=RULE, spaceBefore=6, spaceAfter=10)
        )
        return index + 1

    if token.type == "inline":
        flowables.append(Paragraph(_inline(token), styles["body"]))
        return index + 1

    return index + 1


def _footer(title: str) -> Callable[[Any, Any], None]:
    def draw(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont(BODY, 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 12 * mm, _drawable(title))
        canvas.drawRightString(A4[0] - MARGIN, 12 * mm, f"Page {document.page}")
        canvas.restoreState()

    return draw


def render(markdown: str, *, title: str) -> bytes:
    """The document as PDF bytes. ``title`` names the file's metadata and its running footer."""
    _register_fonts()
    styles = _styles()
    tokens = MarkdownIt("commonmark").enable("table").parse(markdown)

    flowables: list[Flowable] = []
    index = 0
    while index < len(tokens):
        index = _block(tokens, index, flowables, styles)

    buffer = io.BytesIO()
    document = BaseDocTemplate(
        buffer,
        pagesize=A4,
        title=_drawable(title),
        author="Nash Chat API",
        subject="Integration guide",
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=18 * mm,
        bottomMargin=20 * mm,
    )
    document.addPageTemplates(
        [
            PageTemplate(
                id="guide",
                frames=[
                    Frame(
                        MARGIN,
                        20 * mm,
                        TEXT_WIDTH,
                        A4[1] - 38 * mm,
                        id="body",
                        leftPadding=0,
                        rightPadding=0,
                        topPadding=0,
                        bottomPadding=0,
                    )
                ],
                onPage=_footer(title),
            )
        ]
    )
    document.build(flowables)
    return buffer.getvalue()
