"""Opt-in PII redaction for ingested documents (spec §5.7).

A knowledge base is a pile of a tenant's own documents, and those documents routinely contain
other people's details — an invoice with a customer's phone number, a policy PDF with the account
manager's email, a spreadsheet exported with a card number still in it. v1 injects extracted text
straight into the prompt (§5.2.2), so anything left in that text can reach a model, a log, and in
the worst case an answer.

**Opt-in, per knowledge base, and applied once at ingestion.** Three decisions, each deliberate:

* *Opt-in*, because redaction is lossy in a way that breaks legitimate agents. A support agent whose
  knowledge base is a table of customer order contacts stops working the moment their phone numbers
  become ``[redacted:phone]``. Silently degrading answers is worse than storing what the tenant
  chose to upload.
* *Per knowledge base* rather than per tenant, so an agent answering from public policy
  documents and one answering from customer records can live in one account under different rules.
* *At ingestion*, so the redacted form is what is **stored**. Redacting at query time would leave
  the raw text in the database, in backups, and in the source preview endpoint — that is not
  redaction, it is a filter one bug away from being bypassed.

**Regexes, and honest about what that means.** This finds the shapes it knows: email addresses,
phone numbers, and payment card numbers. It will not find a name, an address, or an account number
in a format it has never seen, and it will occasionally take a long reference code for a card. It is
a reduction in exposure, not a guarantee of anonymity, and the API says so where a tenant turns it
on. A model-based classifier would catch more and cost a model call per document; that is a v2
trade, not a v1 one.

Card numbers are Luhn-checked before they are redacted. Without that, every order reference and
tracking number of the right length disappears from the knowledge base, and the agent stops being
able to answer questions about orders — which is the single most common thing these agents do.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")

# International and local shapes: an optional +, then 9 to 15 digits with spaces, dots or
# hyphens between them. Bounded on both sides so it cannot eat half of a longer number.
PHONE = re.compile(r"(?<![\w+])\+?\d(?:[\d\s.-]{7,17}\d)(?![\w])")

# 13 to 19 digits in the groupings cards are written in. Candidates only — Luhn decides.
CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")

EMAIL_TOKEN = "[redacted:email]"
PHONE_TOKEN = "[redacted:phone]"
CARD_TOKEN = "[redacted:card]"


@dataclass(frozen=True, slots=True)
class RedactionResult:
    """The redacted text, and what was taken out of it.

    The counts are what a tenant is shown after an upload. "We removed 41 phone numbers from this
    document" is the fastest way for someone to notice that they have turned this on for a knowledge
    base that needed those phone numbers.
    """

    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def luhn(digits: str) -> bool:
    """The check digit every payment card carries. Not security — a filter against false matches."""
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact(text: str) -> RedactionResult:
    """Replace the PII shapes this module knows with labelled placeholders.

    Cards are handled first. An email address cannot contain a card number, but a long digit run
    *can* look like a phone number, and redacting phones first would leave a half-eaten card behind.
    """
    if not text:
        return RedactionResult(text=text)

    counts = {"card": 0, "email": 0, "phone": 0}

    def replace_card(match: re.Match[str]) -> str:
        digits = re.sub(r"[^\d]", "", match.group())
        if len(digits) < 13 or len(digits) > 19 or not luhn(digits):
            # Not a card. Returned untouched so an order reference survives ingestion — the agent
            # is far more often asked about those than about card numbers.
            return match.group()
        counts["card"] += 1
        return CARD_TOKEN

    redacted = CARD.sub(replace_card, text)

    def replace_email(_: re.Match[str]) -> str:
        counts["email"] += 1
        return EMAIL_TOKEN

    redacted = EMAIL.sub(replace_email, redacted)

    def replace_phone(match: re.Match[str]) -> str:
        digits = re.sub(r"[^\d]", "", match.group())
        if len(digits) < 9 or len(digits) > 15:
            return match.group()
        counts["phone"] += 1
        return PHONE_TOKEN

    redacted = PHONE.sub(replace_phone, redacted)

    return RedactionResult(text=redacted, counts={key: n for key, n in counts.items() if n})


def apply(text: str | None, enabled: bool) -> tuple[str | None, dict[str, int]]:
    """Redact when the knowledge base asks for it, and report what was removed.

    A single entry point so every place that stores extracted text — the inline upload path, the
    worker, and the scheduled connector sync — goes through the same code. Three call sites that
    each remembered to redact would be two chances to forget.
    """
    if not enabled or not text:
        return text, {}
    result = redact(text)
    return result.text, result.counts
