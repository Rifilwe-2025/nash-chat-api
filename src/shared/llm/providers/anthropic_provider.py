"""Claude adapter, over the official ``anthropic`` SDK.

Two provider-specific rules the abstraction has to absorb:

* ``max_tokens`` is required on every request.
* Current Claude models (Opus 5 / 4.8 / 4.7, Sonnet 5, Fable 5, and the 4.6 family) **reject**
  ``temperature`` with a 400. An agent configured with a temperature must therefore not have it
  forwarded blindly — the adapter drops it for those models and keeps it for the older ones that
  still accept sampling parameters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import anthropic

from src import configs
from src.shared.llm.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    Role,
    TokenUsage,
    ToolCall,
)
from src.shared.llm.errors import (
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConfigurationError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMUnavailableError,
)

DEFAULT_MODEL = "claude-opus-5"

# Models that still accept sampling parameters. Anything newer rejects them, so an unrecognised
# model id defaults to omitting temperature — the safe direction, since sending it errors while
# omitting it merely uses the provider default.
SAMPLING_CAPABLE_PREFIXES = ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-3")


def accepts_temperature(model: str) -> bool:
    return model.startswith(SAMPLING_CAPABLE_PREFIXES)


def _content(message: ChatMessage) -> Any:
    """A plain string when there is nothing attached, so ordinary turns keep the simple shape."""
    if message.role is Role.TOOL:
        # Claude has no tool role: a result is a `tool_result` block inside a *user* turn, matched
        # to the request by `tool_use_id`. The role swap happens in `_message` below.
        return [
            {
                "type": "tool_result",
                "tool_use_id": message.tool_call_id or "",
                "content": message.content,
            }
        ]

    if message.tool_calls:
        # The request has to be replayed alongside its result, or the API rejects the pair. A text
        # block is included only when the model actually said something as well as calling.
        requested: list[dict[str, Any]] = []
        if message.content:
            requested.append({"type": "text", "text": message.content})
        requested.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in message.tool_calls
        )
        return requested

    if not message.attachments:
        return message.content

    blocks: list[dict[str, Any]] = [
        {
            "type": attachment.kind.value,
            "source": {
                "type": "base64",
                "media_type": attachment.media_type,
                "data": attachment.base64_data,
            },
        }
        for attachment in message.attachments
    ]
    # The instruction goes last: Claude reads the files first and then the ask about them.
    blocks.append({"type": "text", "text": message.content})
    return blocks


def _message(message: ChatMessage) -> dict[str, Any]:
    """One message in Claude's shape.

    A tool result is sent as a user turn, because that is where Claude expects ``tool_result``
    blocks — the only place this adapter rewrites a role rather than passing it through.
    """
    role = "user" if message.role is Role.TOOL else message.role.value
    return {"role": role, "content": _content(message)}


@contextmanager
def _translated(provider: str) -> Iterator[None]:
    """Map ``anthropic`` exceptions onto the shared hierarchy."""
    try:
        yield
    except LLMError:
        raise
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise LLMAuthenticationError(str(exc), provider=provider) from exc
    except anthropic.RateLimitError as exc:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        raw = headers.get("retry-after") if headers is not None else None
        raise LLMRateLimitError(
            str(exc), provider=provider, retry_after=float(raw) if raw else None
        ) from exc
    except anthropic.APITimeoutError as exc:
        raise LLMTimeoutError(str(exc), provider=provider) from exc
    except anthropic.APIConnectionError as exc:
        raise LLMUnavailableError(str(exc), provider=provider) from exc
    except anthropic.APIStatusError as exc:
        if exc.status_code >= 500:
            raise LLMUnavailableError(str(exc), provider=provider) from exc
        raise LLMBadRequestError(str(exc), provider=provider) from exc


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
            return
        key = api_key or configs.LLM_ANTHROPIC_API_KEY
        if not key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is not configured.", provider=self.name)
        self._client = anthropic.AsyncAnthropic(
            api_key=key,
            timeout=configs.LLM_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,  # retries are the shared policy's job, not the SDK's
        )

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        model = request.model or DEFAULT_MODEL
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": [_message(message) for message in request.messages],
        }
        if request.system:
            payload["system"] = request.system
        if request.temperature is not None and accepts_temperature(model):
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop_sequences"] = list(request.stop_sequences)
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
        return payload

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        with _translated(self.name):
            response = await self._client.messages.create(**self._payload(request))

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                )

        return CompletionResult(
            content="".join(text_parts),
            usage=TokenUsage(
                prompt_tokens=response.usage.input_tokens,
                completion_tokens=response.usage.output_tokens,
            ),
            model=response.model,
            provider=self.name,
            tool_calls=tool_calls,
            raw_finish_reason=response.stop_reason,
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            with _translated(self.name):
                async with self._client.messages.stream(**self._payload(request)) as stream:
                    async for text in stream.text_stream:
                        yield text

        return iterator()

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()
