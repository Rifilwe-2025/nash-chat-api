"""The 24-hour customer service window (spec §5.5, §6).

WhatsApp's rule, stated once so nothing else has to restate it: **a business may send free-form
messages to a contact only within 24 hours of that contact's last inbound message.** Outside that
window only a pre-approved *template* is delivered. Meta enforces it; sending free-form text to a
closed window is rejected by the API, and a tenant discovering that through a 400 they cannot read
is precisely the "easy to get wrong" §6 warns about.

So the window is decided here, before anything is sent, and the decision is a value the caller can
act on rather than an exception it has to catch. Three things follow from that:

* **Replies to an inbound message are always inside the window.** The contact just wrote; that is
  what opened it. The check still runs, because a slow queue, a long retry, or a conversation picked
  up by a human hours later can all put a reply outside a window that was open when it was composed.
* **The fallback is configured, not invented.** Templates must be approved by Meta before they can
  be sent, so the platform cannot synthesise one at the moment it is needed — a tenant nominates an
  approved template on their connection and that is what is used.
* **A closed window with no configured template is a refusal, not a silent drop.** The tenant gets
  ``WHATSAPP_WINDOW_CLOSED`` and knows why nothing was sent.

The window is measured from the message ledger rather than a counter kept beside it: the timestamp
is already recorded, and a second copy could only ever disagree with the first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src import configs


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """Whether free-form text can reach this contact right now, and until when."""

    is_open: bool
    last_inbound_at: datetime | None
    expires_at: datetime | None

    @property
    def seconds_remaining(self) -> int:
        """How long the window has left, floored at zero. Zero when it is closed."""
        if not self.is_open or self.expires_at is None:
            return 0
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


def window_hours() -> int:
    hours: int = configs.WHATSAPP_SESSION_WINDOW_HOURS
    return hours


def evaluate(last_inbound_at: datetime | None, now: datetime | None = None) -> SessionWindow:
    """Decide the window from the contact's last inbound message.

    A contact who has never written has no window at all — the business is the one initiating, which
    WhatsApp only permits by template. That is why "never messaged us" and "messaged us two days
    ago" produce the same closed window rather than being distinguished here.
    """
    if last_inbound_at is None:
        return SessionWindow(is_open=False, last_inbound_at=None, expires_at=None)

    # Rows read back from Postgres carry a timezone; one built in a test may not. Assuming UTC for a
    # naive value is right for this schema — every timestamp column is `DateTime(timezone=True)` —
    # and it keeps the comparison below from raising instead of answering.
    anchor = last_inbound_at if last_inbound_at.tzinfo else last_inbound_at.replace(tzinfo=UTC)
    expires_at = anchor + timedelta(hours=window_hours())
    moment = now or datetime.now(UTC)

    return SessionWindow(is_open=moment < expires_at, last_inbound_at=anchor, expires_at=expires_at)
