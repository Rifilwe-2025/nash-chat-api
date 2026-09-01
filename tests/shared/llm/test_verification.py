"""Probing a provider key: what each failure is reported as, and what is never done with the key."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from src.shared.llm import registry
from src.shared.llm.base import CompletionRequest, CompletionResult, LLMProvider, TokenUsage
from src.shared.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from src.shared.llm.verification import KeyCheckStatus, verify_key

PROVIDER = "gemini"


class ProbeProvider(LLMProvider):
    """Answers, or fails the way the test asked it to, and remembers what it was given."""

    name = PROVIDER

    def __init__(self, api_key: str | None = None, error: LLMError | None = None) -> None:
        self.api_key = api_key
        self.error = error
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return CompletionResult(
            content="ok",
            usage=TokenUsage(prompt_tokens=4, completion_tokens=1),
            model=request.model,
            provider=self.name,
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        raise NotImplementedError("a probe never streams")


class Probe:
    """The registered fake, plus the knobs a test needs on it."""

    def __init__(self) -> None:
        self.built: list[ProbeProvider] = []
        self.error: LLMError | None = None

    def fail_with(self, error: LLMError) -> None:
        self.error = error

    def build(self, api_key: str | None) -> LLMProvider:
        adapter = ProbeProvider(api_key=api_key, error=self.error)
        self.built.append(adapter)
        return adapter

    @property
    def first(self) -> ProbeProvider:
        return self.built[0]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    """Stand in for the real Gemini factory, so no test here touches a network."""
    handle = Probe()
    monkeypatch.setitem(registry.PROVIDERS, PROVIDER, handle.build)
    return handle


async def test_a_working_key_reports_ok(probe: Probe) -> None:
    check = await verify_key(PROVIDER, "gemini-2.0-flash", "a-real-key")

    assert check.ok
    assert check.status is KeyCheckStatus.OK
    assert check.model == "gemini-2.0-flash"
    assert check.latency_ms >= 0


async def test_the_probe_uses_the_key_and_model_it_was_given(probe: Probe) -> None:
    await verify_key(PROVIDER, "gemini-1.5-flash", "the-tenants-key")

    adapter = probe.first
    assert adapter.api_key == "the-tenants-key", "the supplied key is the one that gets used"
    assert adapter.requests[0].model == "gemini-1.5-flash"


async def test_the_probe_is_one_tiny_call(probe: Probe) -> None:
    """A check that costs real tokens is a check nobody presses twice."""
    await verify_key(PROVIDER, "gemini-2.0-flash", "key")

    request = probe.first.requests[0]
    assert len(request.messages) == 1
    assert request.max_tokens <= 8
    assert not request.tools


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (LLMAuthenticationError("401 unauthorised"), KeyCheckStatus.INVALID_KEY),
        (LLMBadRequestError("model not found"), KeyCheckStatus.MODEL_REJECTED),
        (LLMRateLimitError("slow down"), KeyCheckStatus.RATE_LIMITED),
        (LLMConfigurationError("no key"), KeyCheckStatus.NOT_CONFIGURED),
        (LLMTimeoutError("timed out"), KeyCheckStatus.UNAVAILABLE),
        (LLMUnavailableError("502"), KeyCheckStatus.UNAVAILABLE),
    ],
)
async def test_each_failure_is_reported_as_something_actionable(
    probe: Probe, error: LLMError, expected: KeyCheckStatus
) -> None:
    probe.fail_with(error)

    check = await verify_key(PROVIDER, "gemini-2.0-flash", "key")

    assert not check.ok
    assert check.status is expected
    assert str(error) in check.detail, "the provider's own wording is the useful part"


async def test_an_unknown_provider_is_an_answer_not_an_exception() -> None:
    """Callers render the result; nothing here may raise at them."""
    check = await verify_key("not-a-provider", "some-model", "key")

    assert not check.ok
    assert check.status is KeyCheckStatus.NOT_CONFIGURED
