"""HTML and web pages — boilerplate out, semantic structure kept (spec §5.2.3).

Navigation, scripts, styles, and footers are stripped before the text is read: injecting a site's
menu into every prompt costs tokens and teaches the agent nothing. Headings survive as Markdown
levels for the same reason they do in the Word extractor — they are what tells the model, and
Tier 2, where one topic ends and the next begins.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from src.modules.knowledge_base.internal.extractors.base import (
    ExtractedContent,
    ExtractionError,
    ExtractionResult,
)
from src.modules.knowledge_base.internal.extractors.text_extractor import decode

BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "svg",
    "iframe",
    "template",
)

CONTENT_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th", "pre", "blockquote")

# The page's own content root, in the order worth trying. Falls back to the whole body.
MAIN_SELECTORS = ("main", "article", "[role=main]", "#content", ".content")

# ``\s`` covers the non-breaking spaces that HTML is full of, not just spaces and tabs.
_WHITESPACE = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _root(soup: BeautifulSoup) -> Tag:
    for selector in MAIN_SELECTORS:
        found = soup.select_one(selector)
        if isinstance(found, Tag) and _clean(found.get_text(" ")):
            return found
    body = soup.body
    return body if isinstance(body, Tag) else soup


def extract_html(markup: str, *, url: str | None = None) -> ExtractionResult:
    """Shared by the file path and the URL path — a fetched page is just HTML that arrived over
    the network."""
    soup = BeautifulSoup(markup, "html.parser")

    for tag in soup.find_all(BOILERPLATE_TAGS):
        tag.decompose()

    title = _clean(soup.title.get_text()) if soup.title else None

    blocks: list[str] = []
    for element in _root(soup).find_all(CONTENT_TAGS):
        if not isinstance(element, Tag):
            continue
        text = _clean(element.get_text(" "))
        if not text:
            continue
        if element.name.startswith("h") and element.name[1:].isdigit():
            blocks.append(f"{'#' * int(element.name[1:])} {text}")
        else:
            blocks.append(text)

    # Nested tags mean the same sentence can be picked up more than once; keep first appearances.
    deduped = list(dict.fromkeys(blocks))

    if not deduped:
        raise ExtractionError("The page contains no readable text.")

    rendered = "\n\n".join(deduped)
    if title and not deduped[0].endswith(title):
        rendered = f"# {title}\n\n{rendered}"

    metadata: dict[str, object] = {
        "format": "html",
        "title": title,
        "blocks": len(deduped),
        "characters": len(rendered),
    }
    if url:
        metadata["url"] = url
    return ExtractionResult(text=rendered, metadata=metadata)


class HtmlExtractor:
    async def extract(self, content: ExtractedContent) -> ExtractionResult:
        return extract_html(decode(content.data), url=content.filename)
