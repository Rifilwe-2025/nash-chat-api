"""Turning a tool's JSON response into text the model can use (spec §5.2.1).

The mapping exists because handing a model a raw API response is a bad idea twice over. It is
expensive — a verbose payload can be thousands of tokens of envelope around one useful field — and
it is leaky: an order lookup that returns the customer's payment token, internal ids, or another
customer's data would put all of it in the prompt, where the model may repeat it.

So the tenant declares what matters and the rest is dropped. ``response_mapping_json`` looks like::

    {
      "root": "data.order",
      "fields": {"status": "Status", "eta": "Estimated delivery"},
      "list_limit": 5
    }

* ``root`` narrows to the part of the payload that matters, dotted, with numeric segments indexing
  into lists.
* ``fields`` maps a source path to the label the model sees. **Its presence is what makes the
  mapping an allowlist**: declare fields and nothing else is included, no matter what the API adds
  next month.
* ``list_limit`` caps how many items of an array are rendered.

With no mapping configured the whole response is rendered, truncated. That is the right default for
getting started — a tenant should see something work before they tune it — and the truncation is
what keeps "I have not configured this yet" from becoming an unbounded prompt.

**Everything produced here is data, not instructions (§5.7).** It is somebody's API response, which
means it is somebody's user-supplied content one hop removed, and the conversation service fences it
before it reaches the model.
"""

from __future__ import annotations

import json
from typing import Any

ROOT = "root"
FIELDS = "fields"
LIST_LIMIT = "list_limit"

DEFAULT_LIST_LIMIT = 10


def extract(payload: Any, path: str) -> Any:
    """Follow a dotted path, treating numeric segments as list indices.

    Returns ``None`` for anything that does not exist rather than raising: a missing field in
    someone else's API response is normal, and one absent field must not fail the whole call.
    """
    current = payload
    for segment in path.split("."):
        if not segment:
            continue
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def render(payload: Any, mapping: dict[str, Any] | None, max_characters: int) -> str:
    """Render a response as the text the model will read."""
    config = mapping or {}

    root = str(config.get(ROOT) or "")
    body = extract(payload, root) if root else payload
    if body is None:
        return f"The response had nothing at {root!r}."

    fields = config.get(FIELDS)
    limit = _limit(config)

    if isinstance(fields, dict) and fields:
        text = _mapped(body, fields, limit)
    else:
        text = _whole(body, limit)

    return _truncate(text, max_characters)


def _limit(config: dict[str, Any]) -> int:
    try:
        return max(1, int(config.get(LIST_LIMIT, DEFAULT_LIST_LIMIT)))
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT


def _mapped(body: Any, fields: dict[str, Any], limit: int) -> str:
    """Only the declared fields, labelled. A list becomes one numbered block per item."""
    if isinstance(body, list):
        items = body[:limit]
        blocks = [f"{index + 1}. {_one(item, fields)}" for index, item in enumerate(items)]
        if len(body) > limit:
            blocks.append(f"({len(body) - limit} more not shown)")
        return "\n".join(blocks) if blocks else "No results."
    return _one(body, fields)


def _one(item: Any, fields: dict[str, Any]) -> str:
    parts: list[str] = []
    for path, label in fields.items():
        value = extract(item, str(path))
        if value is None:
            continue
        parts.append(f"{label}: {_scalar(value)}")
    # An item where every declared field was absent is reported rather than silently skipped: the
    # difference between "no data" and "wrong field paths" is one a tenant needs to see.
    return "; ".join(parts) if parts else "(no mapped fields present)"


def _whole(body: Any, limit: int) -> str:
    """No mapping configured: render the payload compactly, capping list length."""
    if isinstance(body, list):
        trimmed = body[:limit]
        text = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(body) > limit:
            text = f"{text}\n({len(body) - limit} more not shown)"
        return text
    if isinstance(body, dict):
        return json.dumps(body, ensure_ascii=False, default=str)
    return _scalar(body)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _truncate(text: str, max_characters: int) -> str:
    if max_characters <= 0 or len(text) <= max_characters:
        return text
    # Says so rather than cutting silently: a model given a truncated JSON object will otherwise
    # try to reason about the half it can see as though it were the whole.
    return f"{text[:max_characters]}\n… (response truncated)"
