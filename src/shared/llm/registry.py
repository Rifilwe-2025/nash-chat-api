"""Provider resolution and the client every module actually uses.

``LLMClient`` is the seam that makes spec §10's "switching provider is a config change, no code
change" true: callers hand it an agent's stored provider and model, and it decides which adapter
runs, how failures are retried, and when to fall back.

Tenant bring-your-own keys (§9, open question) are stubbed here on purpose: ``api_key`` threads
through ``get_provider``, so the day that decision is made it is a lookup change, not a redesign.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from src import configs
from src.shared.llm.base import CompletionRequest, CompletionResult, LLMProvider, TextStream
from src.shared.llm.errors import (
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from src.shared.llm.providers.anthropic_provider import AnthropicProvider
from src.shared.llm.providers.gemini_provider import GeminiProvider
from src.shared.llm.providers.openai_provider import OpenAIProvider
from src.shared.llm.retry import backoff_delay, with_retries
from src.shared.observability import PROVIDER_CALLS, PROVIDER_DURATION, PROVIDER_ERRORS, metrics

logger = logging.getLogger("api.llm")

ProviderFactory = Callable[[str | None], LLMProvider]

PROVIDERS: dict[str, ProviderFactory] = {
    AnthropicProvider.name: lambda key: AnthropicProvider(api_key=key),
    OpenAIProvider.name: lambda key: OpenAIProvider(api_key=key),
    GeminiProvider.name: lambda key: GeminiProvider(api_key=key),
}


def get_provider(name: str, api_key: str | None = None) -> LLMProvider:
    """Build the adapter for ``name``.

    ``api_key`` overrides the platform key — the seam for tenant-supplied credentials.
    """
    factory = PROVIDERS.get(name.lower())
    if factory is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise LLMConfigurationError(f"Unknown provider {name!r}. Supported: {supported}.")
    return factory(api_key)


class LLMClient:
    """Runs one completion with the shared retry and fallback policy applied."""

    def __init__(
        self,
        provider_factory: Callable[[str, str | None], LLMProvider] = get_provider,
    ) -> None:
        self._build = provider_factory

    async def complete(
        self,
        provider: str,
        request: CompletionRequest,
        api_key: str | None = None,
    ) -> CompletionResult:
        adapter = self._build(provider, api_key)

        try:
            return await self._attempt(adapter, request)
        except (LLMRateLimitError, LLMUnavailableError) as exc:
            fallback = self._fallback_for(provider)
            if fallback is None:
                raise
            logger.warning(
                "provider %s unavailable (%s); falling back to %s",
                provider,
                type(exc).__name__,
                fallback,
            )
            return await self._attempt(self._build(fallback, None), request)

    def stream(
        self,
        provider: str,
        request: CompletionRequest,
        api_key: str | None = None,
    ) -> TextStream:
        """Stream a reply, retried only while nothing has reached the caller.

        Bytes already sent cannot be un-sent, so a stream that has produced text is not restartable
        — that half was always right. But a stream that has produced *nothing* has sent nothing, and
        a provider failing at that point is the same transient blip ``complete`` retries as a matter
        of course. Treating the whole stream as unretryable is what made one vendor hiccup a visibly
        failed turn on the streamed path and invisible on the buffered one.

        So: the same retry budget and the same fallback as ``complete``, up until the first token,
        and nothing after it.
        """
        stream = TextStream()

        async def deltas() -> AsyncIterator[str]:
            attempts = max(1, int(configs.LLM_MAX_ATTEMPTS))
            produced = False
            last: LLMError | None = None

            for attempt in range(attempts):
                metrics.increment(PROVIDER_CALLS, provider=provider)
                source = self._build(provider, api_key).stream(request)
                try:
                    async for delta in source:
                        produced = True
                        yield delta
                    stream.usage = source.usage
                    return
                except LLMError as exc:
                    metrics.increment(PROVIDER_ERRORS, provider=provider, error=type(exc).__name__)
                    # Past the first token there is nothing to retry into: the caller has already
                    # been shown part of an answer, and a second attempt would repeat it.
                    if produced or not exc.retryable:
                        raise
                    last = exc
                    if attempt < attempts - 1:
                        await asyncio.sleep(self._pause_after(exc, attempt))

            fallback = (
                self._fallback_for(provider)
                if isinstance(last, LLMRateLimitError | LLMUnavailableError)
                else None
            )
            if fallback is None:
                raise last if last else RuntimeError("stream ended without a result")

            logger.warning(
                "provider %s unavailable on a stream (%s); falling back to %s",
                provider,
                type(last).__name__,
                fallback,
            )
            metrics.increment(PROVIDER_CALLS, provider=fallback)
            second = self._build(fallback, None).stream(request)
            async for delta in second:
                yield delta
            stream.usage = second.usage

        return stream.attach(deltas())

    @staticmethod
    def _pause_after(error: LLMError, attempt: int) -> float:
        """The same backoff ``with_retries`` uses, including a rate limit's own ``retry-after``."""
        delay = backoff_delay(
            attempt,
            base=configs.LLM_RETRY_BASE_DELAY_SECONDS,
            cap=configs.LLM_RETRY_MAX_DELAY_SECONDS,
        )
        if isinstance(error, LLMRateLimitError) and error.retry_after:
            return max(delay, error.retry_after)
        return delay

    async def _attempt(self, adapter: LLMProvider, request: CompletionRequest) -> CompletionResult:
        """One provider call, timed and counted whichever way it ends.

        The measurement wraps the retries rather than each attempt, because what matters
        operationally is how long the *caller* waited — a customer does not experience three
        attempts, they experience one slow reply. The error counter is keyed by the exception class,
        so "the provider is rate limiting us" and "the provider is down" stay distinguishable
        without anyone having to read a log.
        """
        with metrics.timed(PROVIDER_DURATION, provider=adapter.name):
            metrics.increment(PROVIDER_CALLS, provider=adapter.name)
            try:
                return await with_retries(
                    lambda: adapter.complete(request),
                    attempts=configs.LLM_MAX_ATTEMPTS,
                    base_delay=configs.LLM_RETRY_BASE_DELAY_SECONDS,
                    max_delay=configs.LLM_RETRY_MAX_DELAY_SECONDS,
                )
            except LLMError as exc:
                metrics.increment(PROVIDER_ERRORS, provider=adapter.name, error=type(exc).__name__)
                raise

    def _fallback_for(self, provider: str) -> str | None:
        configured = (configs.LLM_FALLBACK_PROVIDER or "").strip().lower()
        if not configured or configured == provider.lower():
            return None
        if configured not in PROVIDERS:
            logger.error("configured fallback provider %r is not supported", configured)
            return None
        return configured


__all__ = ["PROVIDERS", "LLMClient", "LLMError", "get_provider"]
