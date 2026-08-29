"""What a WhatsApp connection stores, and what may be shown back (spec §5.5, §5.7).

``channel_config`` already holds two JSON columns; this module is the only place that knows what
goes in them for WhatsApp, so the shape is defined once instead of being spelled out at each read.

``credentials_json`` holds the access token, the app secret and the verify token. **Nothing here is
ever returned to a caller.** :func:`redact` is what the API responds with — the phone number id and
provider stay, everything secret becomes a boolean saying whether it is set. That is enough for a
tenant to see their connection is complete without the platform handing back a token that was, from
the moment it was saved, only ours to use. (These are stored rather than hashed because we must
*present* them to Meta; encrypting them at rest is Phase 13's, and is noted here rather than faked.)

``settings_json`` holds the choices a tenant makes: which approved template covers a closed session
window, and whether the agent answers automatically at all.
"""

from __future__ import annotations

import secrets
from typing import Any

# credentials_json
PROVIDER = "provider"
PHONE_NUMBER_ID = "phoneNumberId"
ACCESS_TOKEN = "accessToken"
APP_SECRET = "appSecret"
VERIFY_TOKEN = "verifyToken"
BUSINESS_ACCOUNT_ID = "businessAccountId"
DISPLAY_PHONE_NUMBER = "displayPhoneNumber"

# Never leaves the server.
SECRET_KEYS = frozenset({ACCESS_TOKEN, APP_SECRET, VERIFY_TOKEN})

# settings_json
TEMPLATE = "outsideWindowTemplate"
TEMPLATE_NAME = "name"
TEMPLATE_LANGUAGE = "language"
TEMPLATE_VARIABLES = "variables"
AUTO_REPLY = "autoReply"
MARK_READ = "markRead"

VERIFY_TOKEN_BYTES = 24


def generate_verify_token() -> str:
    """The token a tenant pastes into Meta's webhook form.

    Generated rather than chosen: it is compared against an untrusted string on a public endpoint,
    and a tenant who picks ``hello`` has made their webhook guessable. Long and random costs them
    one copy-paste.
    """
    return f"wavt_{secrets.token_urlsafe(VERIFY_TOKEN_BYTES)}"


def redact(credentials: dict[str, Any]) -> dict[str, Any]:
    """The connection as a caller may see it: identifiers kept, secrets reduced to "set or not"."""
    visible = {
        key: value
        for key, value in credentials.items()
        if key not in SECRET_KEYS and value not in (None, "")
    }
    visible["hasAccessToken"] = bool(credentials.get(ACCESS_TOKEN))
    visible["hasAppSecret"] = bool(credentials.get(APP_SECRET))
    return visible


def merge_credentials(existing: dict[str, Any], supplied: dict[str, Any] | None) -> dict[str, Any]:
    """Apply an update without wiping what it did not mention.

    A tenant rotating their access token should not have to re-paste their app secret, and an
    omitted key must therefore mean "leave it" rather than "clear it". An explicitly empty string
    still clears, so removing a value remains possible.
    """
    merged = dict(existing)
    for key, value in (supplied or {}).items():
        merged[key] = value
    if not merged.get(VERIFY_TOKEN):
        merged[VERIFY_TOKEN] = generate_verify_token()
    return merged


def template_from(settings: dict[str, Any]) -> tuple[str, str, list[str]] | None:
    """The approved template configured for a closed window, if there is one.

    Returns ``(name, language, variables)`` — deliberately not a ``TemplateMessage``, so this module
    stays free of the provider package and the layering stays one-directional.
    """
    configured = settings.get(TEMPLATE)
    if not isinstance(configured, dict):
        return None

    name = str(configured.get(TEMPLATE_NAME) or "").strip()
    if not name:
        return None

    raw_variables = configured.get(TEMPLATE_VARIABLES)
    variables = [str(value) for value in raw_variables] if isinstance(raw_variables, list) else []
    return name, str(configured.get(TEMPLATE_LANGUAGE) or "en_US"), variables


def auto_reply_enabled(settings: dict[str, Any]) -> bool:
    """Whether inbound messages are answered by the agent.

    Defaults to on. A tenant who connects a number and publishes an agent expects it to answer;
    making them find a second switch would be a connection that silently does nothing.
    """
    return bool(settings.get(AUTO_REPLY, True))


def mark_read_enabled(settings: dict[str, Any]) -> bool:
    """Whether inbound messages get a blue tick. On by default — a bot that reads should show it."""
    return bool(settings.get(MARK_READ, True))
