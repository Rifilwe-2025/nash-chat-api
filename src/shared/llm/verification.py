"""Does this credential actually work? (spec §5.3, §9 — tenant-supplied keys.)

A tenant pastes a provider key into the builder and has no way of knowing whether it is right until
an end user gets an error. There is no offline answer: key formats are not stable, a key can be
valid but lack access to the model that was typed beside it, and a project can have billing
disabled while its key still looks perfectly well-formed. Only the provider knows.

So the check is a real call — the smallest one the adapter can make. One word in, one token out,
against the exact provider *and model* the agent is configured with, because "your key is fine but
not for `gpt-4o`" is the failure this is most useful for catching.

The outcome is deliberately not an exception. A failed check is a normal, expected answer to "is
this right?", and the caller renders it rather than handling it: :class:`KeyCheck` carries a
machine-readable :class:`KeyCheckStatus` for that, mapped from the provider-neutral errors every
adapter already raises. The provider's own wording rides along in ``detail`` — it is often the only
thing that says *which* of several plausible things went wrong — and is passed through untouched.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass

from src.shared.llm.base import ChatMessage, CompletionRequest, Role
from src.shared.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from src.shared.llm.registry import get_provider

# Short, harmless, and answerable in a token. The reply is thrown away — only the fact that the
# provider produced one matters.
PROBE_PROMPT = "Reply with the single word: ok"
PROBE_MAX_TOKENS = 8


class KeyCheckStatus(str, enum.Enum):
    """What the provider said, in terms a builder UI can act on.

    Split by *what the user has to change*, not by HTTP status: a rejected key and an unreachable
    provider are both "it did not work", but only one of them is the tenant's to fix.
    """

    OK = "ok"
    INVALID_KEY = "invalid_key"
    MODEL_REJECTED = "model_rejected"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


#: Guidance shown when the provider's own message is unhelpful or absent.
ADVICE: dict[KeyCheckStatus, str] = {
    KeyCheckStatus.OK: "The key works for this model.",
    KeyCheckStatus.INVALID_KEY: (
        "The provider rejected this key. Check it was copied whole and has not been revoked."
    ),
    KeyCheckStatus.MODEL_REJECTED: (
        "The key is accepted but the request was not. The model name is usually wrong, or this "
        "key's account has no access to it."
    ),
    KeyCheckStatus.RATE_LIMITED: (
        "The key works — the provider is rate limiting it right now. Try again shortly."
    ),
    KeyCheckStatus.UNAVAILABLE: (
        "The provider could not be reached. This says nothing about the key; try again."
    ),
    KeyCheckStatus.NOT_CONFIGURED: "No key is configured for this provider.",
}

_STATUS_FOR: list[tuple[type[LLMError], KeyCheckStatus]] = [
    # Order matters: the first match wins, so subclasses would go above their bases.
    (LLMAuthenticationError, KeyCheckStatus.INVALID_KEY),
    (LLMBadRequestError, KeyCheckStatus.MODEL_REJECTED),
    (LLMRateLimitError, KeyCheckStatus.RATE_LIMITED),
    (LLMConfigurationError, KeyCheckStatus.NOT_CONFIGURED),
    (LLMTimeoutError, KeyCheckStatus.UNAVAILABLE),
    (LLMUnavailableError, KeyCheckStatus.UNAVAILABLE),
]


@dataclass(frozen=True, slots=True)
class KeyCheck:
    """The result of one probe. ``ok`` is the only thing most callers need."""

    status: KeyCheckStatus
    provider: str
    model: str
    latency_ms: int
    detail: str

    @property
    def ok(self) -> bool:
        return self.status is KeyCheckStatus.OK


def _status_for(error: LLMError) -> KeyCheckStatus:
    for kind, status in _STATUS_FOR:
        if isinstance(error, kind):
            return status
    return KeyCheckStatus.UNAVAILABLE


async def verify_key(provider: str, model: str, api_key: str | None = None) -> KeyCheck:
    """Probe ``provider`` with ``api_key`` and report what happened.

    Raises nothing a caller has to catch: an unknown provider name and a dead network both come
    back as a :class:`KeyCheck` that is not ``ok``. Retries are skipped on purpose — the adapter is
    used directly rather than through :class:`~src.shared.llm.registry.LLMClient`, because a person
    is waiting on this answer and three backoffs would turn a five-second "no" into twenty.
    """
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    try:
        adapter = get_provider(provider, api_key)
        result = await adapter.complete(
            CompletionRequest(
                messages=[ChatMessage(role=Role.USER, content=PROBE_PROMPT)],
                model=model,
                max_tokens=PROBE_MAX_TOKENS,
            )
        )
    except LLMError as exc:
        status = _status_for(exc)
        return KeyCheck(
            status=status,
            provider=provider,
            model=model,
            latency_ms=elapsed(),
            detail=str(exc) or ADVICE[status],
        )

    return KeyCheck(
        status=KeyCheckStatus.OK,
        provider=result.provider or provider,
        model=result.model or model,
        latency_ms=elapsed(),
        detail=ADVICE[KeyCheckStatus.OK],
    )


__all__ = ["ADVICE", "PROBE_PROMPT", "KeyCheck", "KeyCheckStatus", "verify_key"]
