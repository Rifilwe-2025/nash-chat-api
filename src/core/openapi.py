"""OpenAPI / Swagger metadata.

The interactive docs are a deliverable of this API, not a by-product: the integration
documentation handed to tenants is generated from this schema. Every module adds its tag here with
a one-line description of what the tag covers.
"""

from __future__ import annotations

from typing import Any

API_DESCRIPTION = """
Backend for a multi-tenant platform for building, configuring, and deploying custom AI chat agents.

**Response envelope** — every endpoint returns the same shape, with camelCase keys.
`value` and `error` are mutually exclusive, and null fields are omitted:

```json
{ "success": true, "value": { }, "message": null }
```

**Errors** carry a stable `error.code` alongside a human-readable `error.detail`.

**Request tracing** — every response carries an `X-Request-ID` header. Send your own to have it
echoed back, and quote it when reporting a problem.
"""

TAGS_METADATA: list[dict[str, Any]] = [
    {
        "name": "system",
        "description": "Service health and readiness probes.",
    },
    {
        "name": "auth",
        "description": (
            "Account creation and session management. Sign up or sign in to receive an access "
            "token and a refresh token; send the access token as `Authorization: Bearer <token>`. "
            "Issuing a new pair revokes every token held previously, and logout takes effect "
            "immediately."
        ),
    },
    {
        "name": "agents",
        "description": (
            "Create and configure chat agents: persona, engagement rules, guardrails, and which "
            "LLM runs them. Agents start as drafts and must be published before they serve "
            "traffic. Every configuration change is versioned and can be rolled back."
        ),
    },
    {
        "name": "knowledge-bases",
        "description": (
            "Knowledge an agent answers from. A knowledge base holds sources — uploaded files, "
            "web pages, and typed FAQ entries — and each source is stored as the plain text "
            "extracted from it, which you can read back to see exactly what an agent will use. "
            "One knowledge base can be attached to any number of agents."
        ),
    },
    {
        "name": "conversations",
        "description": (
            "Talking to an agent, and the record of what was said. A turn retrieves knowledge, "
            "assembles a prompt, calls the agent's model, and stores both sides with the tokens "
            "and cost it used. Guardrails are applied before the model is called: an escalation "
            "trigger hands the conversation to a human, and a restricted topic is declined "
            "outright. Retrieved knowledge and user messages reach the model as data, never as "
            "instructions."
        ),
    },
    {
        "name": "api-keys",
        "description": (
            "Credentials for integrating an agent into your own product. A key is issued for one "
            "agent, carries explicit scopes and its own rate limit, and is shown exactly once — "
            "only a hash is stored. Revocation takes effect on the next request."
        ),
    },
    {
        "name": "channels",
        "description": (
            "Where an agent is reachable, and what the platform tells you about it. Channel "
            "settings per agent, outbound webhooks for conversation events, and a generated "
            "integration guide for handing to whoever is doing the integration."
        ),
    },
    {
        "name": "chat",
        "description": (
            "**The public chat API.** Authenticated by an agent API key rather than a user token, "
            "and the only surface a tenant's own product talks to. Send a message and get a reply, "
            "stream it as server-sent events, or fetch a conversation's history. Every response "
            "carries the key's remaining rate-limit allowance."
        ),
    },
    {
        "name": "account",
        "description": (
            "The signed-in user and the tenant they belong to. The tenant is always resolved from "
            "the access token, so these endpoints can only ever read or modify the caller's own "
            "organisation."
        ),
    },
]
