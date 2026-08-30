"""Every ``error.code`` this API can return, with what it means (Phase 13, spec §10).

A caller branches on ``error.code``, not on the prose beside it, which makes the set of codes part
of the API contract as much as any response body. §10 requires a developer to integrate against the
generated docs without support — and "what can this endpoint return, and what do I do about it?" is
the question a per-route ``responses={...}`` answers locally but nothing answered globally.

So the catalogue lives here, is rendered into the OpenAPI description, and is **enforced**:
``tests/test_error_catalogue.py`` scans the source for every ``code="…"`` raised and fails when one
is missing from this file. A new failure mode is therefore documented in the same change that
introduces it, rather than discovered by whoever hits it first.

Grouped by what the caller should *do*, because that is the only grouping that helps at three in
the morning: fix the request, fix the configuration, wait and retry, or tell somebody.
"""

from __future__ import annotations

# Codes recorded on failure rows rather than returned in a response. They appear in the analytics
# failure report (`GET /analytics/failures`), so a caller does see them — just not as an error.
RECORDED_ONLY = {
    "INGESTION_FAILED": "A source's extraction failed. Read the detail on the source itself.",
    "WHATSAPP_DELIVERY_FAILED": "A WhatsApp message was never delivered to the contact.",
}

AUTHENTICATION = {
    "UNAUTHORIZED": "No access token was presented, or it is not valid.",
    "INVALID_TOKEN": "The token is malformed, expired, or was signed by something else.",
    "TOKEN_REVOKED": "The token was valid but has been revoked — sign in again.",
    "INVALID_CREDENTIALS": "The email or password is wrong. Never says which.",
    "EMAIL_TAKEN": "That email already has an account.",
    "TENANT_NOT_FOUND": "The tenant behind this token no longer exists.",
    "MISSING_API_KEY": "The public chat API needs an agent API key, as bearer or `X-API-Key`.",
    "INVALID_API_KEY": "The key does not exist, is revoked, or has expired. All read alike.",
    "INSUFFICIENT_SCOPE": "The key is valid but was not issued with the scope this route needs.",
    "API_KEY_NEEDS_SCOPE": "A key must be issued with at least one scope.",
    "UNKNOWN_SCOPE": "A scope in the request is not one this platform defines.",
    "API_KEY_NOT_FOUND": "No such key in your tenant.",
    "API_KEY_EXPIRY_IN_PAST": "A key cannot be issued already expired.",
    "INVALID_RATE_LIMIT": "The requested per-key rate limit is outside the allowed range.",
    "OPERATOR_TOKEN_INVALID": "`X-Operator-Token` is missing or wrong on an operator route.",
    "METRICS_DISABLED": "No operator token is configured, so operator metrics are closed.",
}

NOT_FOUND = {
    "AGENT_NOT_FOUND": "No such agent in your tenant.",
    "AGENT_VERSION_NOT_FOUND": "No such version of that agent.",
    "KB_NOT_FOUND": "No such knowledge base in your tenant.",
    "KB_SOURCE_NOT_FOUND": "No such source in that knowledge base.",
    "KB_LINK_NOT_FOUND": "That knowledge base is not attached to that agent.",
    "CONVERSATION_NOT_FOUND": "No such conversation in your tenant.",
    "TOOL_NOT_FOUND": "No such tool in your tenant.",
    "WEBHOOK_NOT_FOUND": "No such webhook endpoint in your tenant.",
}

REQUEST = {
    "VALIDATION_ERROR": "The payload failed validation. `error.detail` names the field.",
    "BAD_REQUEST": "The request could not be processed as sent.",
    "EMPTY_MESSAGE": "A chat message cannot be blank.",
    "MESSAGE_TOO_LONG": "The message is longer than the configured maximum.",
    "AGENT_NAME_TAKEN": "Another agent in your tenant already has that name.",
    "KB_NAME_TAKEN": "Another knowledge base in your tenant already has that name.",
    "TOOL_NAME_TAKEN": "That agent already has a tool with that name.",
    "TOOL_NAME_INVALID": "A tool name must be a valid function name for every provider.",
    "TOOL_DESCRIPTION_TOO_SHORT": "The description is prompt text; it has to say something.",
    "TOOL_ENDPOINT_INVALID": "The endpoint is not a usable http(s) URL.",
    "TOOL_PLACEHOLDER_UNDECLARED": "The URL uses a placeholder the schema does not declare.",
    "TOOL_POLICY_INVALID": "`maxCallsPerTurn` must be at least one.",
    "UNKNOWN_PROVIDER": "That LLM provider is not one this platform supports.",
    "UNKNOWN_WEBHOOK_EVENT": "That event is not one this platform publishes.",
    "WEBHOOK_NEEDS_EVENT": "A webhook endpoint must subscribe to at least one event.",
    "KB_SYNC_INTERVAL_TOO_SHORT": "The sync interval is below the configured floor.",
    "KB_SOURCE_EMPTY": "Nothing readable was extracted from that source.",
    "KB_SOURCE_TOO_LARGE": "The upload is larger than the per-source limit.",
    "KB_UNSUPPORTED_FILE_TYPE": "No extractor handles that media type.",
    "KB_RETRIEVAL_SCOPE_REQUIRED": "A retrieval request must name an agent or a knowledge base.",
    "UNSUPPORTED_MEDIA": "The media type of that attachment is not one the model can read.",
    "MEDIA_TOO_LARGE": "The inbound media is above the configured maximum.",
    "UNSUPPORTED_MESSAGE_TYPE": "That message type has no text the agent can answer.",
    "ANALYTICS_WINDOW_INVALID": "The reporting window's start is not before its end.",
    "ANALYTICS_WINDOW_TOO_LONG": "The reporting window is longer than the configured maximum.",
}

PLAN = {
    "PLAN_LIMIT_EXCEEDED": (
        "Your plan's limit for agents, monthly messages or stored knowledge is reached. "
        "`error.detail` says which one and by how much. Returned as `402`, not `429`: waiting "
        "does not help, the account needs a larger plan."
    ),
}

STATE = {
    "AGENT_NOT_PUBLISHED": "The agent is a draft or paused, so it does not serve traffic.",
    "AGENT_NOT_PUBLISHABLE": "Something required for publishing is missing. The detail lists it.",
    "AGENT_NOT_CONFIGURED": "The agent has no provider or model set.",
    "INVALID_STATUS_TRANSITION": "That status change is not allowed from the current one.",
    "CONVERSATION_NOT_ACTIVE": "The conversation is closed or escalated and takes no new turns.",
    "CHANNEL_NOT_CONFIGURED": "That channel has not been set up for this agent.",
    "CHANNEL_DISABLED": "The channel exists but is switched off.",
    "CHANNEL_NEEDS_SETUP_ROUTE": "This channel has its own connect endpoint; use that instead.",
    "INCOMPLETE_CREDENTIALS": "The channel's credentials are missing a required field.",
    "KB_SOURCE_NOT_SYNCABLE": "Only URL and API sources can be re-synced.",
    "KB_STORAGE_LIMIT_REACHED": "The tenant's stored-knowledge limit is reached.",
    "WHATSAPP_CONNECTION_INCOMPLETE": "The WhatsApp connection is missing part of its setup.",
    "WHATSAPP_INCOMPLETE_CREDENTIALS": "The Meta credentials are missing a required field.",
    "WHATSAPP_UNKNOWN_PROVIDER": "That WhatsApp provider is not one this platform supports.",
    "WHATSAPP_WRONG_NUMBER": "The webhook is for a phone number this connection does not own.",
    "WHATSAPP_INVALID_SIGNATURE": "The webhook signature did not verify.",
    "WHATSAPP_VERIFICATION_FAILED": "The verify token in Meta's handshake did not match.",
    "WHATSAPP_WINDOW_CLOSED": "Outside the 24-hour window only a template may be sent.",
    "WHATSAPP_NOTHING_TO_SEND": "The reply had no text and no template to fall back on.",
    "WHATSAPP_SEND_REJECTED": "The provider refused the message. The detail carries its reason.",
}

TRANSIENT = {
    "RATE_LIMITED": "Too many requests. `Retry-After` says when to come back.",
    "PROVIDER_UNAVAILABLE": "The agent's model could not be reached. Safe to retry.",
    "WHATSAPP_PROVIDER_UNAVAILABLE": "WhatsApp's API could not be reached. Safe to retry.",
    "KB_EXTRACTION_FAILED": "Extraction failed for that source. The detail says why.",
    "DATABASE_ERROR": "A database operation failed. Retry; report it if it persists.",
    "INTERNAL_ERROR": "Something went wrong on our side. Quote the `X-Request-ID`.",
    "SERVICE_UNAVAILABLE": "A dependency is unavailable.",
    "CONFLICT": "The request conflicts with the current state of the resource.",
    "FORBIDDEN": "The credential is valid but not allowed to do this.",
    "NOT_FOUND": "The resource does not exist.",
}

GROUPS: list[tuple[str, str, dict[str, str]]] = [
    (
        "Authentication and credentials",
        "Fix the credential, then retry.",
        AUTHENTICATION,
    ),
    (
        "Missing resources",
        "**A resource in another tenant is a 404, never a 403.** Telling the two apart would "
        "confirm that something exists, which is itself a leak (§5.7).",
        NOT_FOUND,
    ),
    ("Invalid requests", "Fix the request and send it again.", REQUEST),
    (
        "Wrong state",
        "The request is well-formed but the resource is not in a state that allows it. Change the "
        "configuration or the status first.",
        STATE,
    ),
    (
        "Plan limits",
        "The request is legitimate and the caller is permitted; the account's plan does not allow "
        "it. See `GET /billing/plan` for what is used and what is allowed.",
        PLAN,
    ),
    (
        "Transient and server-side",
        "Retry with backoff. Quote the `X-Request-ID` header if it persists.",
        TRANSIENT,
    ),
    (
        "Recorded, not returned",
        "These never appear in a response body. They label rows in `GET /analytics/failures`.",
        RECORDED_ONLY,
    ),
]

# Every documented code, flattened — what the enforcement test checks against.
ALL_CODES: dict[str, str] = {
    code: meaning for _, _, group in GROUPS for code, meaning in group.items()
}


def render() -> str:
    """The catalogue as markdown, for the OpenAPI description."""
    sections = ["## Error codes", ""]
    for title, note, codes in GROUPS:
        sections.append(f"### {title}")
        sections.append("")
        sections.append(note)
        sections.append("")
        sections.append("| Code | Meaning |")
        sections.append("| --- | --- |")
        sections.extend(f"| `{code}` | {meaning} |" for code, meaning in sorted(codes.items()))
        sections.append("")
    return "\n".join(sections)
