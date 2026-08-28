"""Provider-neutral LLM failures.

Each adapter maps its SDK's exceptions onto these, so retry policy and error handling are written
once rather than three times. ``retryable`` is what the retry helper branches on — it is a property
of the failure, not of the provider.
"""

from __future__ import annotations


class LLMError(Exception):
    """Base for every provider failure."""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        self.provider = provider
        super().__init__(message)


class LLMConfigurationError(LLMError):
    """The provider is not usable as configured — missing key, unknown provider, bad model."""


class LLMAuthenticationError(LLMError):
    """Credentials were rejected. Never retried: retrying a bad key just burns time."""


class LLMBadRequestError(LLMError):
    """The provider rejected the request itself. Retrying sends the same broken request."""


class LLMRateLimitError(LLMError):
    """Rate limited. Retryable, and the trigger for falling back to another provider."""

    retryable = True

    def __init__(
        self, message: str, *, provider: str | None = None, retry_after: float | None = None
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, provider=provider)


class LLMTimeoutError(LLMError):
    retryable = True


class LLMUnavailableError(LLMError):
    """Provider-side 5xx or connection failure."""

    retryable = True
