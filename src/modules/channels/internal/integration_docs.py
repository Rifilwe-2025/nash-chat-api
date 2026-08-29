"""Per-agent integration documentation, generated from the live schema (spec §5.6).

Spec §10 sets the bar: a developer must be able to integrate against these docs without support.
That rules out a hand-written page, which drifts from the API the first time a route changes — so
the endpoint list, their summaries and their descriptions are read from **the running app's own
OpenAPI schema**. If a route changes, this changes with it, or it does not change at all.

What is *not* generated is the narrative: the quickstart, the session model, the webhook
verification snippet. Those are written here because they explain decisions a schema cannot express
— why `userId` matters, what `escalated` obliges the integrator to do, why a signature has a
timestamp in it.

The tenant's own values are filled in: their agent's id and name, their key's prefix and scopes,
their rate limit. Generic examples are what makes generated docs feel like something to translate
rather than something to paste.
"""

from __future__ import annotations

from typing import Any

CHAT_PREFIX = "/v1/chat"


def _routes(schema: dict[str, Any]) -> list[dict[str, str]]:
    """The public chat routes, as the schema currently describes them."""
    found: list[dict[str, str]] = []
    for path, operations in sorted(schema.get("paths", {}).items()):
        if not path.startswith(CHAT_PREFIX):
            continue
        for method, operation in sorted(operations.items()):
            found.append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary", ""),
                    "description": operation.get("description", ""),
                }
            )
    return found


def _quickstart(base_url: str, key_prefix: str) -> str:
    return f"""## Quickstart

Every request carries your API key. Either header works:

```http
Authorization: Bearer {key_prefix}...
X-API-Key: {key_prefix}...
```

Send a message:

```bash
curl -X POST {base_url}{CHAT_PREFIX}/messages \\
  -H "Authorization: Bearer $NASH_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"message": "Do you deliver to Bulawayo?", "userId": "visitor-8f21c3"}}'
```

```json
{{
  "success": true,
  "value": {{
    "conversationId": "…",
    "reply": "Yes — deliveries to Bulawayo take two to three working days.",
    "escalated": false
  }}
}}
```
"""


SESSIONS = """## Sessions

Conversations are keyed by the `userId` you send. Pass a **stable** value per person — your own
user id if they are signed in, or a random id kept in `localStorage` for anonymous visitors — and
every message under it continues the same conversation, with history carried across turns.

A new `userId` starts a new conversation. The same `userId` after a conversation is closed or
escalated also starts a new one, so you never need to reset anything yourself.

Messages sent in quick succession for one `userId` are answered in order. You do not need to wait
for one reply before sending the next.
"""


ESCALATION = """## Escalation

When a reply comes back with `"escalated": true`, a guardrail has handed the conversation to a
human. **The agent will not answer any further messages in it.** Show the user that someone is
coming, and either stop sending or start a fresh session with a new `userId`.

Subscribe to the `conversation.escalated` webhook to be told the moment it happens, rather than
finding out from a reply.
"""


def _webhooks(signature_header: str) -> str:
    return f"""## Webhooks

Configure an endpoint to receive `conversation.started` and `conversation.escalated`. Each delivery
is a JSON POST signed with the endpoint's secret in `{signature_header}`:

```
{signature_header}: t=1735689600,v1=<hex hmac-sha256>
```

The signature covers `<timestamp>.<raw body>`. Verify it against the **raw** body before parsing,
and reject anything older than five minutes — a signature over the body alone would be replayable
forever.

```python
import hashlib, hmac, time

def verify(secret: str, raw_body: bytes, header: str) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    timestamp, provided = parts.get("t"), parts.get("v1")
    if not timestamp or not provided:
        return False
    if abs(int(time.time()) - int(timestamp)) > 300:
        return False
    expected = hmac.new(
        secret.encode(), f"{{timestamp}}.".encode() + raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)
```

Deliveries are best effort and are not retried yet, so treat a webhook as a prompt to act rather
than the only record: the conversation endpoints remain the source of truth.
"""


def _limits(rate_limit: int) -> str:
    return f"""## Rate limits

This key allows **{rate_limit} requests per minute**. Every response carries where you stand:

```
X-RateLimit-Limit: {rate_limit}
X-RateLimit-Remaining: 57
X-RateLimit-Reset: 1735689660
```

Over the limit you get `429` with `Retry-After` in seconds. Back off for that long — retrying
sooner simply consumes the next window.
"""


ERRORS = """## Errors

Every response uses the same envelope, and failures carry a stable `error.code` you can branch on:

```json
{ "success": false, "error": { "code": "RATE_LIMITED", "detail": "…" } }
```

| Code | Meaning |
|---|---|
| `MISSING_API_KEY` / `INVALID_API_KEY` | Absent, wrong, revoked or expired — indistinguishable. |
| `INSUFFICIENT_SCOPE` | The key is valid but lacks the scope this route needs. |
| `AGENT_NOT_PUBLISHED` | The agent has been paused or returned to draft. |
| `RATE_LIMITED` | Slow down; see `Retry-After`. |
| `PROVIDER_UNAVAILABLE` | The model could not be reached. Safe to retry. |
| `EMPTY_MESSAGE` / `MESSAGE_TOO_LONG` | Fix the message and resend. |

Quote the `X-Request-ID` header when reporting a problem — it identifies the exact request in our
logs.
"""


def _whatsapp(base_url: str, connection_id: str, phone_number_id: str) -> str:
    """Setup steps for a connected WhatsApp number (spec §5.5, "setup steps in the docs").

    Written out rather than generated from the schema because the hard part is not the endpoint
    shapes — it is the order of operations in *Meta's* console, which our schema knows nothing
    about. Get the order wrong and the verification handshake fails with no useful message.
    """
    webhook = f"{base_url}/v1/channels/whatsapp/webhook/{connection_id}"
    return f"""## WhatsApp

This agent is connected to WhatsApp Business number `{phone_number_id}`. Finish the setup in your
Meta app under **WhatsApp -> Configuration**:

| Field | Value |
|---|---|
| Callback URL | `{webhook}` |
| Verify token | Shown when you save the connection. Re-save it to see the token again. |

Click **Verify and save**. Meta calls the callback URL immediately and the subscription only
succeeds if the token matches. Then **Manage** the webhook fields and subscribe to `messages` —
without it, nothing is delivered and everything looks connected.

**Every delivery is verified.** We check `X-Hub-Signature-256` against your app secret and reject
anything that does not match, so an app secret that is wrong or missing means an agent that never
answers. That is deliberate: the callback URL is not a secret.

**Duplicates are handled for you.** WhatsApp redelivers a webhook whenever it does not see a prompt
`200`. A message it has already delivered is recognised by its `wamid` and ignored, so a redelivery
never produces a second reply.

### The 24-hour window

WhatsApp only delivers **free-form** messages within 24 hours of the customer's last message to you.
Replies to an inbound message are always inside it. Sending on your own initiative may not be:

- `GET /agents/{{agentId}}/channels/whatsapp/sessions/{{contactId}}` tells you whether the window is
  open and how long is left.
- `POST /agents/{{agentId}}/channels/whatsapp/messages` sends `text` inside the window. Outside it,
  your connection's `outsideWindowTemplate` is sent instead — and with no template configured the
  send is refused with `WHATSAPP_WINDOW_CLOSED` rather than silently dropped.
- Templates must be approved by Meta before they can be sent. Create them in WhatsApp Manager, then
  name one on the connection.

`GET /agents/{{agentId}}/channels/whatsapp/messages` is the delivery log: both directions, with
`sent` / `delivered` / `read` / `failed` and WhatsApp's own reason on anything that failed.
"""


def build(
    *,
    agent_name: str,
    agent_id: str,
    base_url: str,
    key_prefix: str,
    scopes: list[str],
    rate_limit: int,
    signature_header: str,
    schema: dict[str, Any],
    whatsapp_connection_id: str | None = None,
    whatsapp_phone_number_id: str | None = None,
) -> str:
    """Render the integration guide for one agent as Markdown."""
    endpoints = "\n".join(
        f"### `{route['method']} {route['path']}`\n\n{route['summary']}\n\n{route['description']}\n"
        for route in _routes(schema)
    )

    # Only for an agent that actually has a number connected: setup steps for a channel a tenant has
    # not enabled would be noise in a document whose whole value is that it is specific to them.
    whatsapp = (
        _whatsapp(base_url, whatsapp_connection_id, whatsapp_phone_number_id or "your number")
        if whatsapp_connection_id
        else ""
    )

    return "\n".join(
        [
            f"# Integrating {agent_name}",
            "",
            f"Agent id `{agent_id}` · base URL `{base_url}` · key `{key_prefix}…`",
            f"Scopes on this key: {', '.join(f'`{scope}`' for scope in scopes) or 'none'}.",
            "",
            _quickstart(base_url, key_prefix),
            SESSIONS,
            ESCALATION,
            _limits(rate_limit),
            _webhooks(signature_header),
            whatsapp,
            ERRORS,
            "## Endpoints",
            "",
            endpoints,
        ]
    )
