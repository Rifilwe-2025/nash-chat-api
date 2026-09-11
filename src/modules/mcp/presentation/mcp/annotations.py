"""Tool annotations: what each tool does to the platform, stated so an MCP client can act on it.

Clients use these hints to decide what to run without asking — Claude Code, for one, can
auto-approve a read-only tool and stop to confirm one that changes something. They are hints, not a
security boundary: the scope on the token is what actually decides whether a write is allowed.

Built from the wire names with ``model_validate`` so they read exactly as the protocol spells them.
"""

from __future__ import annotations

from mcp_types import ToolAnnotations

#: Reads only. Safe to run without confirmation.
READ_ONLY = ToolAnnotations.model_validate({"readOnlyHint": True, "openWorldHint": False})

#: Adds something new. Nothing that existed is changed or lost.
CREATES = ToolAnnotations.model_validate(
    {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)

#: Changes something that already exists. Repeating it with the same arguments changes nothing
#: further, but what was there before is replaced — agent configuration can be rolled back, other
#: settings cannot.
UPDATES = ToolAnnotations.model_validate(
    {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)

#: Reaches outside the platform — a language model provider, a web page, a tenant's own API.
REACHES_OUT = ToolAnnotations.model_validate(
    {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
)

#: Destroys something permanently. There is no undo and nothing is versioned, so a client should
#: always confirm before running one of these. Idempotent only in the weak sense that deleting an
#: already-deleted thing fails rather than deleting something else.
REMOVES = ToolAnnotations.model_validate(
    {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
)
