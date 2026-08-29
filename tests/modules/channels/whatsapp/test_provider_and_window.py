"""The pieces under the channel, tested without a database (spec §5.5).

Three things live here because they are decisions rather than plumbing, and each is cheaper to pin
down directly than through an HTTP round trip:

* **the Meta envelope parser**, which must be total — anything it cannot understand has to be
  skipped, never raised on, because a webhook that 500s is redelivered forever;
* **the 24-hour window**, whose entire behaviour is one comparison that everything else trusts;
* **the provider registry**, which is what makes "switch provider" a credential change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.modules.channels.whatsapp.internal import connection as connection_fields
from src.modules.channels.whatsapp.internal import session_window
from src.modules.channels.whatsapp.internal.providers import (
    DEFAULT_PROVIDER,
    InboundKind,
    MetaCloudProvider,
    WhatsAppError,
    build_provider,
    required_credentials,
)
from tests.modules.channels.whatsapp.helpers import APP_SECRET, CONTACT, meta_webhook

provider = MetaCloudProvider(
    phone_number_id="109876543210987", access_token="t", app_secret=APP_SECRET
)


# -- parsing Meta's envelope -----------------------------------------------------------


def test_a_text_message_is_parsed_with_everything_needed_to_answer_it() -> None:
    parsed = provider.parse_webhook(meta_webhook(text="Do you deliver to Bulawayo?"))

    assert len(parsed.messages) == 1
    message = parsed.messages[0]
    assert message.kind is InboundKind.TEXT
    assert message.text == "Do you deliver to Bulawayo?"
    assert message.contact_id == CONTACT
    assert message.contact_name == "Tariro"
    assert parsed.phone_number_id == "109876543210987"


def test_a_media_message_carries_its_reference_not_its_bytes() -> None:
    parsed = provider.parse_webhook(
        meta_webhook(media_kind="image", media_type="image/jpeg", caption="What colour is this?")
    )

    message = parsed.messages[0]
    assert message.kind is InboundKind.IMAGE
    assert message.media is not None
    assert message.media.media_id == "media-1"
    assert message.media.media_type == "image/jpeg"
    # The caption doubles as the text, so a captioned photo is still a question.
    assert message.text == "What colour is this?"


def test_an_unknown_message_type_parses_as_unsupported_rather_than_failing() -> None:
    """Stickers, locations, reactions, and whatever Meta ships next month."""
    body = meta_webhook()
    message = body["entry"][0]["changes"][0]["value"]["messages"][0]
    message["type"] = "sticker"
    message["sticker"] = {"id": "s-1", "animated": False}
    del message["text"]

    parsed = provider.parse_webhook(body)

    assert parsed.messages[0].kind is InboundKind.UNSUPPORTED


def test_a_media_message_with_no_id_degrades_to_unsupported() -> None:
    """There is nothing to download, so it cannot be treated as media."""
    body = meta_webhook(media_kind="image")
    body["entry"][0]["changes"][0]["value"]["messages"][0]["image"] = {"mime_type": "image/jpeg"}

    assert provider.parse_webhook(body).messages[0].kind is InboundKind.UNSUPPORTED


def test_a_message_with_no_id_is_dropped() -> None:
    """No id means no idempotency key, and answering it could not be made safe."""
    body = meta_webhook()
    del body["entry"][0]["changes"][0]["value"]["messages"][0]["id"]

    assert provider.parse_webhook(body).messages == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"entry": None},
        {"entry": [{"changes": "not-a-list"}]},
        {"entry": [{"changes": [{"value": "not-a-dict"}]}]},
        {"entry": [{"changes": [{"value": {"messages": ["not-a-dict"]}}]}]},
    ],
    ids=["empty", "null-entry", "bad-changes", "bad-value", "bad-messages"],
)
def test_malformed_envelopes_parse_to_nothing_rather_than_raising(body: dict[str, object]) -> None:
    parsed = provider.parse_webhook(body)

    assert parsed.messages == []
    assert parsed.statuses == []


def test_several_messages_in_one_delivery_are_all_parsed() -> None:
    """Meta batches. Reaching for `[0]` would drop everyone but the first customer."""
    body = meta_webhook()
    value = body["entry"][0]["changes"][0]["value"]
    value["messages"] = [
        {"from": "263770000001", "id": "wamid.1", "type": "text", "text": {"body": "One"}},
        {"from": "263770000002", "id": "wamid.2", "type": "text", "text": {"body": "Two"}},
    ]

    parsed = provider.parse_webhook(body)

    assert [message.text for message in parsed.messages] == ["One", "Two"]


def test_delivery_receipts_are_parsed_with_their_failure_reason() -> None:
    parsed = provider.parse_webhook(
        meta_webhook(
            text=None,
            statuses=[
                {
                    "id": "wamid.out",
                    "status": "failed",
                    "timestamp": "1735689700",
                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                }
            ],
        )
    )

    assert parsed.statuses[0].status == "failed"
    assert parsed.statuses[0].error_detail == "Re-engagement message"


# -- signatures ------------------------------------------------------------------------


def test_a_correct_signature_verifies() -> None:
    raw = json.dumps(meta_webhook()).encode("utf-8")
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()

    assert provider.verify_signature(raw, {"X-Hub-Signature-256": f"sha256={digest}"}) is True


def test_the_signature_header_is_matched_case_insensitively() -> None:
    """Header casing is not guaranteed across proxies, and this is a security check."""
    raw = b'{"a":1}'
    digest = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()

    assert provider.verify_signature(raw, {"x-hub-signature-256": f"sha256={digest}"}) is True


@pytest.mark.parametrize(
    "header",
    [{}, {"X-Hub-Signature-256": "sha256=deadbeef"}, {"X-Hub-Signature-256": "nonsense"}],
    ids=["absent", "wrong-digest", "malformed"],
)
def test_a_bad_signature_does_not_verify(header: dict[str, str]) -> None:
    assert provider.verify_signature(b'{"a":1}', header) is False


def test_a_connection_with_no_app_secret_verifies_nothing() -> None:
    """Fail closed. An unverifiable webhook endpoint is an open door, not a lenient one."""
    unsecured = MetaCloudProvider(phone_number_id="1", access_token="t", app_secret="")
    raw = b'{"a":1}'
    digest = hmac.new(b"", raw, hashlib.sha256).hexdigest()

    assert unsecured.verify_signature(raw, {"X-Hub-Signature-256": f"sha256={digest}"}) is False


# -- the 24-hour window ----------------------------------------------------------------


def test_a_recent_message_leaves_the_window_open() -> None:
    window = session_window.evaluate(datetime.now(UTC) - timedelta(hours=1))

    assert window.is_open is True
    assert window.seconds_remaining > 0
    assert window.expires_at is not None


def test_the_window_closes_after_twenty_four_hours() -> None:
    window = session_window.evaluate(datetime.now(UTC) - timedelta(hours=24, minutes=1))

    assert window.is_open is False
    assert window.seconds_remaining == 0


def test_a_contact_who_never_wrote_has_no_window_at_all() -> None:
    """Business-initiated contact is template-only, which is the same closed answer."""
    window = session_window.evaluate(None)

    assert window.is_open is False
    assert window.last_inbound_at is None
    assert window.expires_at is None


def test_a_naive_timestamp_is_read_as_utc_rather_than_raising() -> None:
    """Every timestamp column is timezone-aware; a naive value can only come from a test."""
    window = session_window.evaluate(datetime.now(UTC).replace(tzinfo=None))

    assert window.is_open is True


def test_the_window_length_follows_configuration() -> None:
    """Meta's rule is 24 hours, but the number is configuration, not a literal in the logic."""
    assert session_window.window_hours() == 24


# -- the provider registry -------------------------------------------------------------


def test_the_default_provider_is_built_without_being_named() -> None:
    built = build_provider({"phoneNumberId": "1", "accessToken": "t", "appSecret": "s"})

    assert built.name == DEFAULT_PROVIDER


def test_incomplete_credentials_are_refused_with_the_missing_keys_named() -> None:
    with pytest.raises(WhatsAppError) as raised:
        build_provider({"phoneNumberId": "1"})

    assert raised.value.code == "INCOMPLETE_CREDENTIALS"
    assert "accessToken" in str(raised.value)
    assert "appSecret" in str(raised.value)


def test_an_unknown_provider_is_refused_and_says_what_is_supported() -> None:
    with pytest.raises(WhatsAppError) as raised:
        build_provider({"provider": "carrier-pigeon"})

    assert raised.value.code == "UNKNOWN_PROVIDER"
    assert "meta" in str(raised.value)


def test_the_app_secret_is_required_not_optional() -> None:
    """Without it nothing can be verified, so a connection cannot be considered complete."""
    assert "appSecret" in required_credentials("meta")


# -- what a connection may show back ----------------------------------------------------


def test_redaction_keeps_identifiers_and_drops_every_secret() -> None:
    visible = connection_fields.redact(
        {
            "provider": "meta",
            "phoneNumberId": "109",
            "accessToken": "EAAG-secret",
            "appSecret": "hmac-secret",
            "verifyToken": "wavt_secret",
        }
    )

    assert visible == {
        "provider": "meta",
        "phoneNumberId": "109",
        "hasAccessToken": True,
        "hasAppSecret": True,
    }


def test_an_omitted_credential_is_left_alone_on_update() -> None:
    """Rotating one secret must not mean re-pasting the others."""
    merged = connection_fields.merge_credentials(
        {"accessToken": "old", "appSecret": "kept", "verifyToken": "wavt_kept"},
        {"accessToken": "new"},
    )

    assert merged["accessToken"] == "new"
    assert merged["appSecret"] == "kept"
    assert merged["verifyToken"] == "wavt_kept"


def test_a_connection_without_a_verify_token_is_given_one() -> None:
    merged = connection_fields.merge_credentials({}, {"accessToken": "t"})

    assert merged["verifyToken"].startswith("wavt_")


def test_auto_reply_is_on_unless_it_is_switched_off() -> None:
    """A connected number that answers nothing by default would be a silent failure."""
    assert connection_fields.auto_reply_enabled({}) is True
    assert connection_fields.auto_reply_enabled({"autoReply": False}) is False
