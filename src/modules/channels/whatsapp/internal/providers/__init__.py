"""Building the right provider from a connection's stored credentials (spec §5.3, §5.5).

The registry is the whole reason switching provider is a configuration change: the service asks for
"the provider for this connection" and gets a :class:`WhatsAppProvider`, never a
``MetaCloudProvider`` it had to name. Adding Twilio or 360dialog is a module in this package
plus an entry in
``_PROVIDERS`` — no caller changes, because no caller knows.

Credentials are validated *here* rather than at the point of use, so a connection that cannot work
is refused when a tenant saves it, not discovered at three in the morning when a customer messages.
"""

from __future__ import annotations

from typing import Any

from src.modules.channels.whatsapp.internal.providers.base import (
    InboundKind,
    InboundMedia,
    InboundMessage,
    MediaPayload,
    OutboundResult,
    ParsedWebhook,
    StatusUpdate,
    TemplateMessage,
    WhatsAppError,
    WhatsAppProvider,
)
from src.modules.channels.whatsapp.internal.providers.meta import MetaCloudProvider

__all__ = [
    "DEFAULT_PROVIDER",
    "SUPPORTED_PROVIDERS",
    "InboundKind",
    "InboundMedia",
    "InboundMessage",
    "MediaPayload",
    "MetaCloudProvider",
    "OutboundResult",
    "ParsedWebhook",
    "StatusUpdate",
    "TemplateMessage",
    "WhatsAppError",
    "WhatsAppProvider",
    "build_provider",
    "required_credentials",
]

DEFAULT_PROVIDER = "meta"

# Provider name -> the credential keys it cannot work without. `appSecret` is required rather than
# optional: without it no webhook can be verified, and an unverified webhook endpoint is an open
# door to running a tenant's agent (and spending their tokens) for anyone who finds the URL.
_REQUIRED: dict[str, tuple[str, ...]] = {
    "meta": ("phoneNumberId", "accessToken", "appSecret"),
}

SUPPORTED_PROVIDERS = tuple(sorted(_REQUIRED))


def required_credentials(provider: str) -> tuple[str, ...]:
    """Which credential keys this provider needs. Raises for an unknown provider."""
    try:
        return _REQUIRED[provider]
    except KeyError:
        raise WhatsAppError(
            f"Unknown WhatsApp provider {provider!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}.",
            code="UNKNOWN_PROVIDER",
        ) from None


def build_provider(credentials: dict[str, Any]) -> WhatsAppProvider:
    """Construct the provider a connection's credentials describe.

    ``provider`` defaults to Meta because that is what a tenant following the generated setup steps
    will have, and requiring them to name it would be a field whose only correct value is the
    default.
    """
    name = str(credentials.get("provider") or DEFAULT_PROVIDER).strip().lower()
    missing = [key for key in required_credentials(name) if not str(credentials.get(key) or "")]
    if missing:
        raise WhatsAppError(
            f"The {name} connection is missing: {', '.join(missing)}.",
            code="INCOMPLETE_CREDENTIALS",
        )

    if name == "meta":
        return MetaCloudProvider(
            phone_number_id=str(credentials["phoneNumberId"]),
            access_token=str(credentials["accessToken"]),
            app_secret=str(credentials["appSecret"]),
        )

    # Unreachable while `_REQUIRED` and this branch list agree, and a loud failure rather than a
    # silent `None` if they ever stop agreeing.
    raise WhatsAppError(f"No adapter is built for provider {name!r}.", code="UNKNOWN_PROVIDER")
