"""Fake SDK clients.

Each adapter takes an injected client, so the tests exercise **our** normalisation — payload
shaping, response mapping, error translation — rather than re-testing the vendor SDKs. That is the
part of this module that can actually be wrong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Recorder:
    """Captures the payload an adapter built, so tests can assert on it."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def last(self) -> dict[str, Any]:
        return self.calls[-1]


# -- Anthropic ------------------------------------------------------------------


@dataclass
class FakeAnthropicUsage:
    input_tokens: int = 11
    output_tokens: int = 5


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeAnthropicResponse:
    content: list[Any]
    model: str = "claude-opus-5"
    stop_reason: str | None = "end_turn"
    usage: FakeAnthropicUsage = field(default_factory=FakeAnthropicUsage)


class FakeAnthropicMessages:
    def __init__(self, recorder: Recorder, response: Any, error: Exception | None = None) -> None:
        self._recorder = recorder
        self._response = response
        self._error = error

    async def create(self, **payload: Any) -> Any:
        self._recorder.calls.append(payload)
        if self._error:
            raise self._error
        return self._response

    def stream(self, **payload: Any) -> Any:
        self._recorder.calls.append(payload)
        if self._error:
            raise self._error
        return _FakeAnthropicStream(["Hel", "lo"])


class _FakeAnthropicStream:
    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *args: Any) -> bool:
        return False

    @property
    def text_stream(self) -> AsyncIterator[str]:
        async def gen() -> AsyncIterator[str]:
            for chunk in self._chunks:
                yield chunk

        return gen()


class FakeAnthropicClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.recorder = Recorder()
        self.messages = FakeAnthropicMessages(
            self.recorder,
            response or FakeAnthropicResponse(content=[FakeTextBlock(text="Hello")]),
            error,
        )


# -- OpenAI ---------------------------------------------------------------------


@dataclass
class FakeOpenAIUsage:
    prompt_tokens: int = 7
    completion_tokens: int = 3


@dataclass
class FakeOpenAIFunction:
    name: str
    arguments: str


@dataclass
class FakeOpenAIToolCall:
    id: str
    function: FakeOpenAIFunction


@dataclass
class FakeOpenAIMessage:
    content: str | None = "Hello"
    tool_calls: list[FakeOpenAIToolCall] | None = None


@dataclass
class FakeOpenAIChoice:
    message: FakeOpenAIMessage = field(default_factory=FakeOpenAIMessage)
    finish_reason: str = "stop"


@dataclass
class FakeOpenAIResponse:
    choices: list[FakeOpenAIChoice] = field(default_factory=lambda: [FakeOpenAIChoice()])
    model: str = "gpt-4o"
    usage: FakeOpenAIUsage | None = field(default_factory=FakeOpenAIUsage)


@dataclass
class FakeDelta:
    content: str | None


@dataclass
class FakeStreamChoice:
    delta: FakeDelta


@dataclass
class FakeStreamChunk:
    choices: list[FakeStreamChoice]


class FakeOpenAICompletions:
    def __init__(self, recorder: Recorder, response: Any, error: Exception | None) -> None:
        self._recorder = recorder
        self._response = response
        self._error = error

    async def create(self, **payload: Any) -> Any:
        self._recorder.calls.append(payload)
        if self._error:
            raise self._error
        if payload.get("stream"):
            return _async_iter(
                [
                    FakeStreamChunk([FakeStreamChoice(FakeDelta("Hel"))]),
                    FakeStreamChunk([]),  # keep-alive chunk with no choices
                    FakeStreamChunk([FakeStreamChoice(FakeDelta("lo"))]),
                    FakeStreamChunk([FakeStreamChoice(FakeDelta(None))]),
                ]
            )
        return self._response


class FakeOpenAIClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.recorder = Recorder()
        completions = FakeOpenAICompletions(self.recorder, response or FakeOpenAIResponse(), error)
        self.chat = type("Chat", (), {"completions": completions})()


# -- Gemini ---------------------------------------------------------------------


@dataclass
class FakeGeminiUsage:
    prompt_token_count: int = 13
    candidates_token_count: int = 4


@dataclass
class FakeGeminiCandidate:
    finish_reason: str = "STOP"


@dataclass
class FakeGeminiResponse:
    text: str | None = "Hello"
    usage_metadata: FakeGeminiUsage = field(default_factory=FakeGeminiUsage)
    candidates: list[FakeGeminiCandidate] = field(default_factory=lambda: [FakeGeminiCandidate()])
    function_calls: list[Any] = field(default_factory=list)


@dataclass
class FakeGeminiChunk:
    text: str | None


class FakeGeminiModels:
    def __init__(self, recorder: Recorder, response: Any, error: Exception | None) -> None:
        self._recorder = recorder
        self._response = response
        self._error = error

    async def generate_content(self, **payload: Any) -> Any:
        self._recorder.calls.append(payload)
        if self._error:
            raise self._error
        return self._response

    async def generate_content_stream(self, **payload: Any) -> Any:
        self._recorder.calls.append(payload)
        if self._error:
            raise self._error
        return _async_iter([FakeGeminiChunk("Hel"), FakeGeminiChunk(None), FakeGeminiChunk("lo")])


class FakeGeminiClient:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.recorder = Recorder()
        models = FakeGeminiModels(self.recorder, response or FakeGeminiResponse(), error)
        self.aio = type("Aio", (), {"models": models})()


def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    async def gen() -> AsyncIterator[Any]:
        for item in items:
            yield item

    return gen()
