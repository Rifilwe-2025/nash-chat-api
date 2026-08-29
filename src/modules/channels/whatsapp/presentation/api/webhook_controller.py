"""The public WhatsApp webhook (spec §5.5, §6).

Two routes, both unauthenticated by design — WhatsApp holds no token of ours. What stands in for
authentication is the pair of secrets on the connection: the **verify token** for the handshake, and
the **app secret** the signature is computed with. Both are compared in constant time, and a
connection missing either cannot be reached at all.

**The URL carries the connection id.** A tenant creates their own Meta app and pastes their own
callback URL, so the delivery says which connection it belongs to before a byte of the body is
parsed — which is what makes signature verification possible at all, since the secret to verify with
is on that connection. The id is a random UUID and appears nowhere but in the URL the tenant pastes
into Meta.

**Two deliberate departures from the house style, both forced by the wire protocol:**

1. ``GET`` returns ``hub.challenge`` as **plain text**, not the ``ApiResponse`` envelope. Meta
   compares the body byte for byte against the challenge it sent and fails the subscription on
   anything else — an envelope would mean a webhook that can never be verified. It is the only
   route in the codebase that does this.
2. ``POST`` answers **200 even when the work fails**. Meta reads a non-2xx as "retry", so a bug on
   our side would become an unbounded redelivery loop of a message that may already have been
   answered. Failures are recorded on the message row and surfaced in the delivery log instead. The
   exceptions are the two cases where retrying is *right*: an unknown connection (404) and a bad
   signature (403), neither of which we want to acknowledge as handled.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import Path, Query, Request, Response
from fastapi.responses import PlainTextResponse

from src.modules.channels.whatsapp.presentation.dependencies import (
    WebhookConnectionDep,
    WebhookServicesDep,
)
from src.modules.channels.whatsapp.presentation.dtos.whatsapp import WebhookAckResponse
from src.shared.exceptions import ForbiddenException
from src.shared.responses import ApiResponse, create_router

router = create_router(prefix="/v1/channels/whatsapp", tags=["whatsapp"])

ConnectionIdPath = Annotated[
    uuid.UUID, Path(description="The connection id from your webhook URL.")
]

FORBIDDEN = {
    "description": (
        "The verify token did not match (`WHATSAPP_VERIFICATION_FAILED`), or the payload's "
        "signature is absent or wrong (`WHATSAPP_INVALID_SIGNATURE`)."
    )
}
NOT_FOUND = {"description": "No such WhatsApp connection (`CHANNEL_NOT_CONFIGURED`)."}


@router.get(
    "/webhook/{connection_id}",
    summary="Verify the webhook subscription",
    description=(
        "**Meta calls this, not you.** The verification handshake performed once when you save the "
        "callback URL in your Meta app's WhatsApp configuration.\n\n"
        "Meta sends `hub.mode=subscribe`, `hub.verify_token` and `hub.challenge`. When the token "
        "matches the one issued with your connection, the challenge is echoed back **as plain "
        "text** — the only route on this API that does not use the standard response envelope, "
        "because Meta compares the body byte for byte and rejects anything else.\n\n"
        "A wrong or missing token gets `403` and the subscription is not created."
    ),
    response_class=PlainTextResponse,
    responses={
        200: {
            "description": "The challenge, echoed verbatim.",
            "content": {"text/plain": {"schema": {"type": "string"}}},
        },
        403: FORBIDDEN,
        404: NOT_FOUND,
    },
)
async def verify_webhook(
    connection_id: ConnectionIdPath,
    connection: WebhookConnectionDep,
    services: WebhookServicesDep,
    mode: Annotated[str, Query(alias="hub.mode", description="Always `subscribe`.")] = "",
    token: Annotated[
        str, Query(alias="hub.verify_token", description="The verify token you pasted into Meta.")
    ] = "",
    challenge: Annotated[
        str, Query(alias="hub.challenge", description="The value to echo back.")
    ] = "",
) -> Response:
    if not services.whatsapp.verify_handshake(connection, mode, token):
        raise ForbiddenException(
            "The verify token did not match this connection.",
            code="WHATSAPP_VERIFICATION_FAILED",
        )
    return PlainTextResponse(challenge)


@router.post(
    "/webhook/{connection_id}",
    response_model=ApiResponse[WebhookAckResponse],
    summary="Receive WhatsApp messages and delivery receipts",
    description=(
        "**Meta calls this, not you.** Inbound customer messages, and delivery receipts for "
        "messages your agent sent.\n\n"
        "Every delivery must carry a valid `X-Hub-Signature-256` computed with your Meta app "
        "secret; anything else is rejected with `403` and never reaches your agent.\n\n"
        "**Replays are safe.** WhatsApp redelivers a webhook whenever it does not see a prompt "
        "`200`, and a message it has already delivered is recognised by its `wamid` and ignored — "
        "a duplicate produces no second reply and no second charge. The response counts them under "
        "`duplicates` so a redelivery storm is visible rather than invisible.\n\n"
        "**The reply is not sent from this request.** The message is claimed, acknowledged, and "
        "answered on the queue, so WhatsApp is never kept waiting on a model call.\n\n"
        "This endpoint answers `200` even when answering a message fails, because Meta reads "
        "anything else as a reason to send it again. Failures appear in the delivery log with the "
        "reason on the message."
    ),
    responses={
        200: {"description": "The delivery was accepted."},
        403: FORBIDDEN,
        404: NOT_FOUND,
    },
)
async def receive_webhook(
    connection_id: ConnectionIdPath,
    request: Request,
    connection: WebhookConnectionDep,
    services: WebhookServicesDep,
) -> ApiResponse[WebhookAckResponse]:
    # The signature covers the bytes exactly as they arrived. Parsing and re-serialising changes
    # whitespace and key order, and the signature then fails for reasons that look like a wrong
    # secret — so the raw body is read first and the parse happens after it has been verified.
    raw = await request.body()
    provider = services.whatsapp.provider_for(connection)

    if not provider.verify_signature(raw, dict(request.headers)):
        raise ForbiddenException(
            "The payload signature is missing or does not match this connection's app secret.",
            code="WHATSAPP_INVALID_SIGNATURE",
        )

    try:
        parsed_body = json.loads(raw or b"{}")
    except ValueError:
        # Signed, so it really came from Meta, but unreadable. Acknowledged rather than refused:
        # redelivering a body we could not parse the first time would not help.
        return ApiResponse.ok(WebhookAckResponse(accepted=0, duplicates=0, statuses=0))

    body: dict[str, Any] = parsed_body if isinstance(parsed_body, dict) else {}
    parsed = provider.parse_webhook(body)

    # A delivery addressed to a different number than this connection holds is refused rather than
    # answered: it means two connections' URLs have been crossed, and answering it would put one
    # tenant's agent on another tenant's number.
    expected = str(connection.credentials_json.get("phoneNumberId") or "")
    if parsed.phone_number_id and expected and parsed.phone_number_id != expected:
        raise ForbiddenException(
            "This delivery is addressed to a different phone number than the connection holds.",
            code="WHATSAPP_WRONG_NUMBER",
        )

    result = await services.whatsapp.receive(connection, parsed, services.conversations)
    return ApiResponse.ok(
        WebhookAckResponse(
            accepted=result.accepted, duplicates=result.duplicates, statuses=result.statuses
        )
    )
