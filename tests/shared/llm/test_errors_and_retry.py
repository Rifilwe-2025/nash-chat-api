"""Error translation, retry policy, and provider fallback."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anthropic
import httpx2
import openai
import pytest

from src.shared.llm.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Role,
    TextStream,
    TokenUsage,
)
from src.shared.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from src.shared.llm.providers.anthropic_provider import AnthropicProvider
from src.shared.llm.providers.openai_provider import OpenAIProvider
from src.shared.llm.registry import LLMClient, get_provider
from src.shared.llm.retry import backoff_delay, with_retries
from tests.shared.llm.fakes import FakeAnthropicClient, FakeOpenAIClient

REQUEST = CompletionRequest(messages=[ChatMessage(role=Role.USER, content="Hi")], model="")


def anthropic_status_error(status: int) -> anthropic.APIStatusError:
    # anthropic 1.x is built on httpx2, so its errors take httpx2 objects, not httpx ones.
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(status, request=request, json={"error": {"message": "boom"}})
    if status == 429:
        return anthropic.RateLimitError("rate limited", response=response, body=None)
    if status in (401, 403):
        return anthropic.AuthenticationError("bad key", response=response, body=None)
    return anthropic.APIStatusError("failed", response=response, body=None)


def openai_status_error(status: int) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx2.Response(status, request=request, json={"error": {"message": "boom"}})
    if status == 429:
        return openai.RateLimitError("rate limited", response=response, body=None)
    return openai.APIStatusError("failed", response=response, body=None)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthenticationError),
        (400, LLMBadRequestError),
        (429, LLMRateLimitError),
        (503, LLMUnavailableError),
    ],
)
async def test_anthropic_errors_are_translated(status: int, expected: type[LLMError]) -> None:
    provider = AnthropicProvider(client=FakeAnthropicClient(error=anthropic_status_error(status)))

    with pytest.raises(expected) as caught:
        await provider.complete(REQUEST)

    assert caught.value.provider == "anthropic"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(400, LLMBadRequestError), (429, LLMRateLimitError), (500, LLMUnavailableError)],
)
async def test_openai_errors_are_translated(status: int, expected: type[LLMError]) -> None:
    provider = OpenAIProvider(client=FakeOpenAIClient(error=openai_status_error(status)))

    with pytest.raises(expected):
        await provider.complete(REQUEST)


async def test_timeouts_are_retryable_and_bad_requests_are_not() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    timeout = AnthropicProvider(
        client=FakeAnthropicClient(error=anthropic.APITimeoutError(request))
    )

    with pytest.raises(LLMError) as caught:
        await timeout.complete(REQUEST)
    assert caught.value.retryable is True

    bad = AnthropicProvider(client=FakeAnthropicClient(error=anthropic_status_error(400)))
    with pytest.raises(LLMError) as caught_bad:
        await bad.complete(REQUEST)
    assert caught_bad.value.retryable is False


# -- retry ------------------------------------------------------------------------


async def test_retries_stop_at_the_first_success() -> None:
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise LLMUnavailableError("down")
        return "ok"

    result = await with_retries(flaky, attempts=5, base_delay=0, max_delay=0, sleep=_no_sleep)

    assert result == "ok"
    assert attempts == 3


async def test_a_non_retryable_error_is_raised_immediately() -> None:
    attempts = 0

    async def always_bad() -> str:
        nonlocal attempts
        attempts += 1
        raise LLMBadRequestError("malformed")

    with pytest.raises(LLMBadRequestError):
        await with_retries(always_bad, attempts=5, base_delay=0, max_delay=0, sleep=_no_sleep)

    assert attempts == 1, "retrying a rejected request just sends it again"


async def test_retries_are_bounded() -> None:
    attempts = 0

    async def always_down() -> str:
        nonlocal attempts
        attempts += 1
        raise LLMUnavailableError("down")

    with pytest.raises(LLMUnavailableError):
        await with_retries(always_down, attempts=3, base_delay=0, max_delay=0, sleep=_no_sleep)

    assert attempts == 3


async def test_a_rate_limits_retry_after_is_honoured() -> None:
    delays: list[float] = []

    async def record(delay: float) -> None:
        delays.append(delay)

    calls = 0

    async def limited() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LLMRateLimitError("slow down", retry_after=30.0)
        return "ok"

    await with_retries(limited, attempts=3, base_delay=0.1, max_delay=1, sleep=record)

    assert delays == [30.0], "guessing a shorter delay just earns another rejection"


def test_backoff_grows_and_stays_capped() -> None:
    assert all(0 <= backoff_delay(0, base=1, cap=10) <= 1 for _ in range(50))
    assert all(0 <= backoff_delay(3, base=1, cap=10) <= 8 for _ in range(50))
    assert all(0 <= backoff_delay(10, base=1, cap=10) <= 10 for _ in range(50))


# -- registry and fallback ---------------------------------------------------------


def test_an_unknown_provider_is_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError) as caught:
        get_provider("mistral")

    assert "Supported" in str(caught.value)


def test_a_missing_api_key_is_a_configuration_error() -> None:
    with pytest.raises(LLMConfigurationError):
        get_provider("openai", api_key=None)


async def test_the_client_falls_back_when_a_provider_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "openai")
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_MAX_ATTEMPTS", 1)

    client = LLMClient(
        provider_factory=lambda name, key: (
            AnthropicProvider(client=FakeAnthropicClient(error=anthropic_status_error(429)))
            if name == "anthropic"
            else OpenAIProvider(client=FakeOpenAIClient())
        )
    )

    result = await client.complete("anthropic", REQUEST)

    assert result.provider == "openai", "the tenant gets an answer from the second choice"


async def test_without_a_configured_fallback_the_error_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "")
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_MAX_ATTEMPTS", 1)

    client = LLMClient(
        provider_factory=lambda name, key: AnthropicProvider(
            client=FakeAnthropicClient(error=anthropic_status_error(429))
        )
    )

    with pytest.raises(LLMRateLimitError):
        await client.complete("anthropic", REQUEST)


async def test_a_bad_request_never_triggers_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falling back on a malformed request would just break twice, on someone else's bill."""
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "openai")
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_MAX_ATTEMPTS", 1)
    built: list[str] = []

    def factory(name: str, key: str | None) -> AnthropicProvider:
        built.append(name)
        return AnthropicProvider(client=FakeAnthropicClient(error=anthropic_status_error(400)))

    with pytest.raises(LLMBadRequestError):
        await LLMClient(provider_factory=factory).complete("anthropic", REQUEST)

    assert built == ["anthropic"]


async def test_fallback_to_the_same_provider_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "anthropic")
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_MAX_ATTEMPTS", 1)

    client = LLMClient(
        provider_factory=lambda name, key: AnthropicProvider(
            client=FakeAnthropicClient(error=anthropic_status_error(429))
        )
    )

    with pytest.raises(LLMRateLimitError):
        await client.complete("anthropic", REQUEST)


# -- streaming: retried up to the first token, and not after it ---------------------


class ScriptedStream(LLMProvider):
    """A provider whose stream does exactly what a test asks of it, and counts its own builds."""

    name = "anthropic"

    def __init__(self, *, text: str = "", fail: LLMError | None = None, after: int = 0) -> None:
        self.text = text
        self.fail = fail
        #: How many words to yield before failing. Zero means fail before anything is sent.
        self.after = after

    async def complete(self, request: CompletionRequest) -> CompletionResult:  # pragma: no cover
        raise NotImplementedError("this fake only streams")

    def stream(self, request: CompletionRequest) -> TextStream:
        stream = TextStream()

        async def deltas() -> AsyncIterator[str]:
            words = self.text.split(" ") if self.text else []
            for index, word in enumerate(words):
                if self.fail and index == self.after:
                    raise self.fail
                yield word if index == 0 else f" {word}"
            if self.fail and self.after >= len(words):
                raise self.fail
            stream.usage = TokenUsage(prompt_tokens=11, completion_tokens=5)

        return stream.attach(deltas())


@pytest.fixture
def instant_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real backoff, so these tests take microseconds rather than seconds."""
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_RETRY_MAX_DELAY_SECONDS", 0.0)
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_MAX_ATTEMPTS", 3)
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "")


async def read(stream: TextStream) -> str:
    return "".join([delta async for delta in stream])


async def test_a_stream_that_fails_before_the_first_token_is_retried(
    instant_retries: None,
) -> None:
    """The blip `complete` has always retried, on the path that used to surface it as a failure."""
    built: list[str] = []

    def factory(name: str, key: str | None) -> LLMProvider:
        built.append(name)
        if len(built) == 1:
            return ScriptedStream(fail=LLMUnavailableError("upstream reset", provider=name))
        return ScriptedStream(text="Silk suits a bathroom.")

    stream = LLMClient(provider_factory=factory).stream("anthropic", REQUEST)

    assert await read(stream) == "Silk suits a bathroom."
    assert built == ["anthropic", "anthropic"], "the second attempt is the same provider"
    assert stream.usage.completion_tokens == 5, "usage comes from the attempt that answered"


async def test_a_stream_that_fails_after_the_first_token_is_not_retried(
    instant_retries: None,
) -> None:
    """Bytes already sent cannot be un-sent, so a restart would repeat what was read."""
    built: list[str] = []

    def factory(name: str, key: str | None) -> LLMProvider:
        built.append(name)
        return ScriptedStream(
            text="Silk suits a bathroom.",
            fail=LLMUnavailableError("dropped mid-answer", provider=name),
            after=2,
        )

    stream = LLMClient(provider_factory=factory).stream("anthropic", REQUEST)

    seen: list[str] = []
    with pytest.raises(LLMUnavailableError):
        async for delta in stream:
            seen.append(delta)

    assert "".join(seen) == "Silk suits", "what arrived is still what arrived"
    assert built == ["anthropic"], "one attempt only, once the caller has seen something"


async def test_a_stream_that_cannot_succeed_is_not_retried_either(
    instant_retries: None,
) -> None:
    """A malformed request fails identically on every attempt; retrying just bills for it twice."""
    built: list[str] = []

    def factory(name: str, key: str | None) -> LLMProvider:
        built.append(name)
        return ScriptedStream(fail=LLMBadRequestError("model does not exist", provider=name))

    with pytest.raises(LLMBadRequestError):
        await read(LLMClient(provider_factory=factory).stream("anthropic", REQUEST))

    assert built == ["anthropic"]


async def test_a_stream_falls_back_once_its_retries_are_spent(
    instant_retries: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.shared.llm.registry.configs.LLM_FALLBACK_PROVIDER", "openai")
    built: list[str] = []

    def factory(name: str, key: str | None) -> LLMProvider:
        built.append(name)
        if name == "anthropic":
            return ScriptedStream(fail=LLMRateLimitError("saturated", provider=name))
        return ScriptedStream(text="Answered by the second choice.")

    stream = LLMClient(provider_factory=factory).stream("anthropic", REQUEST)

    assert await read(stream) == "Answered by the second choice."
    assert built == ["anthropic", "anthropic", "anthropic", "openai"]


async def test_a_stream_gives_up_when_there_is_no_fallback(instant_retries: None) -> None:
    def factory(name: str, key: str | None) -> LLMProvider:
        return ScriptedStream(fail=LLMUnavailableError("still down", provider=name))

    with pytest.raises(LLMUnavailableError):
        await read(LLMClient(provider_factory=factory).stream("anthropic", REQUEST))


def test_token_usage_adds_up() -> None:
    total = TokenUsage(10, 5) + TokenUsage(1, 2)

    assert (total.prompt_tokens, total.completion_tokens, total.total_tokens) == (11, 7, 18)


def test_completion_result_defaults_to_no_tool_calls() -> None:
    result = CompletionResult(content="hi", usage=TokenUsage(1, 1), model="m", provider="anthropic")

    assert result.tool_calls == []


async def _no_sleep(delay: float) -> None:
    return None
