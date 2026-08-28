"""Every adapter answers the same request identically through the shared interface (spec §10)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.shared.llm.base import (
    ChatMessage,
    CompletionRequest,
    LLMProvider,
    Role,
    ToolDefinition,
)
from src.shared.llm.providers.anthropic_provider import AnthropicProvider, accepts_temperature
from src.shared.llm.providers.gemini_provider import GeminiProvider
from src.shared.llm.providers.openai_provider import OpenAIProvider
from tests.shared.llm.fakes import (
    FakeAnthropicClient,
    FakeAnthropicResponse,
    FakeGeminiClient,
    FakeOpenAIChoice,
    FakeOpenAIClient,
    FakeOpenAIFunction,
    FakeOpenAIMessage,
    FakeOpenAIResponse,
    FakeOpenAIToolCall,
    FakeTextBlock,
    FakeToolUseBlock,
)

REQUEST = CompletionRequest(
    messages=[
        ChatMessage(role=Role.USER, content="Hi"),
        ChatMessage(role=Role.ASSISTANT, content="Hello"),
        ChatMessage(role=Role.USER, content="What paint suits a bathroom?"),
    ],
    model="",
    system="You are the Nash Paints assistant.",
    max_tokens=256,
    temperature=0.4,
)

Builder = Callable[[], LLMProvider]

BUILDERS: dict[str, Builder] = {
    "anthropic": lambda: AnthropicProvider(client=FakeAnthropicClient()),
    "openai": lambda: OpenAIProvider(client=FakeOpenAIClient()),
    "gemini": lambda: GeminiProvider(client=FakeGeminiClient()),
}


@pytest.mark.parametrize("provider_name", sorted(BUILDERS))
async def test_the_same_request_works_through_every_adapter(provider_name: str) -> None:
    provider = BUILDERS[provider_name]()

    result = await provider.complete(REQUEST)

    assert result.content == "Hello"
    assert result.provider == provider_name
    assert result.model
    assert result.usage.prompt_tokens > 0
    assert result.usage.completion_tokens > 0
    assert result.usage.total_tokens == (
        result.usage.prompt_tokens + result.usage.completion_tokens
    )


@pytest.mark.parametrize("provider_name", sorted(BUILDERS))
async def test_every_adapter_streams_text_deltas(provider_name: str) -> None:
    provider = BUILDERS[provider_name]()

    chunks = [chunk async for chunk in provider.stream(REQUEST)]

    assert "".join(chunks) == "Hello"


# -- provider-specific normalisation ---------------------------------------------


async def test_anthropic_sends_the_system_prompt_separately() -> None:
    client = FakeAnthropicClient()

    await AnthropicProvider(client=client).complete(REQUEST)

    payload = client.recorder.last
    assert payload["system"] == "You are the Nash Paints assistant."
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert payload["max_tokens"] == 256


async def test_anthropic_omits_temperature_on_models_that_reject_it() -> None:
    """Current Claude models 400 on `temperature`; forwarding agent config blindly would break."""
    client = FakeAnthropicClient()

    await AnthropicProvider(client=client).complete(REQUEST)

    assert "temperature" not in client.recorder.last


async def test_anthropic_keeps_temperature_on_older_models() -> None:
    client = FakeAnthropicClient()
    request = CompletionRequest(
        messages=REQUEST.messages, model="claude-haiku-4-5", temperature=0.4
    )

    await AnthropicProvider(client=client).complete(request)

    assert client.recorder.last["temperature"] == 0.4


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5", False),
        ("claude-sonnet-5", False),
        ("claude-opus-4-6", False),
        ("claude-haiku-4-5", True),
        ("claude-sonnet-4-5", True),
        ("some-unknown-model", False),
    ],
)
def test_sampling_support_is_decided_per_model(model: str, expected: bool) -> None:
    assert accepts_temperature(model) is expected


async def test_openai_prepends_the_system_prompt_as_a_message() -> None:
    client = FakeOpenAIClient()

    await OpenAIProvider(client=client).complete(REQUEST)

    messages = client.recorder.last["messages"]
    assert messages[0] == {"role": "system", "content": "You are the Nash Paints assistant."}
    assert len(messages) == 4
    assert client.recorder.last["max_completion_tokens"] == 256


async def test_gemini_renames_the_assistant_role_and_moves_the_system_prompt() -> None:
    client = FakeGeminiClient()

    await GeminiProvider(client=client).complete(REQUEST)

    payload = client.recorder.last
    assert [entry["role"] for entry in payload["contents"]] == ["user", "model", "user"]
    assert payload["config"]["system_instruction"] == "You are the Nash Paints assistant."
    assert payload["config"]["max_output_tokens"] == 256


# -- tool calls -------------------------------------------------------------------


async def test_anthropic_tool_calls_are_normalised() -> None:
    client = FakeAnthropicClient(
        response=FakeAnthropicResponse(
            content=[
                FakeTextBlock(text="Checking"),
                FakeToolUseBlock(id="tu_1", name="order_status", input={"order": "A1"}),
            ]
        )
    )

    result = await AnthropicProvider(client=client).complete(REQUEST)

    assert result.content == "Checking"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "order_status"
    assert result.tool_calls[0].arguments == {"order": "A1"}


async def test_openai_tool_calls_are_normalised() -> None:
    client = FakeOpenAIClient(
        response=FakeOpenAIResponse(
            choices=[
                FakeOpenAIChoice(
                    message=FakeOpenAIMessage(
                        content=None,
                        tool_calls=[
                            FakeOpenAIToolCall(
                                id="call_1",
                                function=FakeOpenAIFunction(
                                    name="order_status", arguments='{"order": "A1"}'
                                ),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ]
        )
    )

    result = await OpenAIProvider(client=client).complete(REQUEST)

    assert result.content == ""
    assert result.tool_calls[0].arguments == {"order": "A1"}


async def test_malformed_tool_arguments_do_not_crash_the_turn() -> None:
    client = FakeOpenAIClient(
        response=FakeOpenAIResponse(
            choices=[
                FakeOpenAIChoice(
                    message=FakeOpenAIMessage(
                        content=None,
                        tool_calls=[
                            FakeOpenAIToolCall(
                                id="call_1",
                                function=FakeOpenAIFunction(
                                    name="order_status", arguments="{not json"
                                ),
                            )
                        ],
                    )
                )
            ]
        )
    )

    result = await OpenAIProvider(client=client).complete(REQUEST)

    assert result.tool_calls[0].arguments == {}


async def test_tool_definitions_reach_each_provider_in_its_own_shape() -> None:
    tool = ToolDefinition(
        name="order_status",
        description="Look up an order",
        parameters={"type": "object", "properties": {"order": {"type": "string"}}},
    )
    request = CompletionRequest(messages=REQUEST.messages, model="", tools=[tool])

    anthropic_client = FakeAnthropicClient()
    await AnthropicProvider(client=anthropic_client).complete(request)
    assert anthropic_client.recorder.last["tools"][0]["input_schema"] == tool.parameters

    openai_client = FakeOpenAIClient()
    await OpenAIProvider(client=openai_client).complete(request)
    assert openai_client.recorder.last["tools"][0]["function"]["name"] == "order_status"


async def test_usage_is_recorded_even_when_the_provider_omits_it() -> None:
    client = FakeOpenAIClient(response=FakeOpenAIResponse(usage=None))

    result = await OpenAIProvider(client=client).complete(REQUEST)

    assert result.usage.total_tokens == 0
