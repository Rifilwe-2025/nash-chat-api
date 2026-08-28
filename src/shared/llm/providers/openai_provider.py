"""OpenAI adapter, over the official ``openai`` SDK.

Differences absorbed here: the system prompt is the first message rather than a separate field,
tools use the ``function`` envelope, and usage may be absent on streamed responses.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

import openai

from src import configs
from src.shared.llm.base import (
    AttachmentKind,
    ChatMessage,
    CompletionRequest,
    CompletionResult,
    LLMProvider,
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

DEFAULT_MODEL = "gpt-4o"


def _content(message: ChatMessage) -> Any:
    """Images ride as a data-URI ``image_url``; anything else as an inline ``file`` part."""
    if not message.attachments:
        return message.content

    parts: list[dict[str, Any]] = []
    for attachment in message.attachments:
        if attachment.kind is AttachmentKind.IMAGE:
            parts.append({"type": "image_url", "image_url": {"url": attachment.data_uri}})
        else:
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": attachment.filename or "attachment",
                        "file_data": attachment.data_uri,
                    },
                }
            )
    parts.append({"type": "text", "text": message.content})
    return parts


@contextmanager
def _translated(provider: str) -> Iterator[None]:
    try:
        yield
    except LLMError:
        raise
    except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
        raise LLMAuthenticationError(str(exc), provider=provider) from exc
    except openai.RateLimitError as exc:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        raw = headers.get("retry-after") if headers is not None else None
        raise LLMRateLimitError(
            str(exc), provider=provider, retry_after=float(raw) if raw else None
        ) from exc
    except openai.APITimeoutError as exc:
        raise LLMTimeoutError(str(exc), provider=provider) from exc
    except openai.APIConnectionError as exc:
        raise LLMUnavailableError(str(exc), provider=provider) from exc
    except openai.APIStatusError as exc:
        if exc.status_code >= 500:
            raise LLMUnavailableError(str(exc), provider=provider) from exc
        raise LLMBadRequestError(str(exc), provider=provider) from exc


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
            return
        key = api_key or configs.LLM_OPENAI_API_KEY
        if not key:
            raise LLMConfigurationError("OPENAI_API_KEY is not configured.", provider=self.name)
        self._client = openai.AsyncOpenAI(
            api_key=key, timeout=configs.LLM_REQUEST_TIMEOUT_SECONDS, max_retries=0
        )

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend(
            {"role": message.role.value, "content": _content(message)}
            for message in request.messages
        )

        payload: dict[str, Any] = {
            "model": request.model or DEFAULT_MODEL,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
        }
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.stop_sequences:
            payload["stop"] = list(request.stop_sequences)
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]
        return payload

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        with _translated(self.name):
            response = await self._client.chat.completions.create(**self._payload(request))

        choice = response.choices[0]
        tool_calls: list[ToolCall] = []
        for call in choice.message.tool_calls or []:
            tool_calls.append(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=_load_arguments(call.function.arguments),
                )
            )

        usage = response.usage
        return CompletionResult(
            content=choice.message.content or "",
            usage=TokenUsage(
                prompt_tokens=usage.prompt_tokens if usage else 0,
                completion_tokens=usage.completion_tokens if usage else 0,
            ),
            model=response.model,
            provider=self.name,
            tool_calls=tool_calls,
            raw_finish_reason=choice.finish_reason,
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            with _translated(self.name):
                stream = await self._client.chat.completions.create(
                    **self._payload(request), stream=True
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield delta.content

        return iterator()

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def _load_arguments(raw: str | None) -> dict[str, Any]:
    """Tool arguments arrive as a JSON string; a malformed one must not crash the turn."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
