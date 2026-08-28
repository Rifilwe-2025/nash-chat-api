"""Gemini adapter, over the official ``google-genai`` SDK.

Differences absorbed here: the system prompt is a separate ``system_instruction`` on the config
rather than a message, roles are ``user`` / ``model`` (not ``assistant``), and generation settings
live in a config object instead of top-level fields.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from google import genai
from google.genai import errors as genai_errors

from src import configs
from src.shared.llm.base import (
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
    LLMUnavailableError,
)

DEFAULT_MODEL = "gemini-2.0-flash"

# Gemini calls the assistant "model".
_ROLE_MAP = {Role.USER: "user", Role.ASSISTANT: "model"}


@contextmanager
def _translated(provider: str) -> Iterator[None]:
    try:
        yield
    except LLMError:
        raise
    except genai_errors.ClientError as exc:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            raise LLMAuthenticationError(str(exc), provider=provider) from exc
        if code == 429:
            raise LLMRateLimitError(str(exc), provider=provider) from exc
        raise LLMBadRequestError(str(exc), provider=provider) from exc
    except genai_errors.ServerError as exc:
        raise LLMUnavailableError(str(exc), provider=provider) from exc


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, client: Any | None = None) -> None:
        if client is not None:
            self._client = client
            return
        key = api_key or configs.LLM_GEMINI_API_KEY
        if not key:
            raise LLMConfigurationError("GEMINI_API_KEY is not configured.", provider=self.name)
        self._client = genai.Client(api_key=key)

    def _contents(self, request: CompletionRequest) -> list[dict[str, Any]]:
        return [
            {"role": _ROLE_MAP[message.role], "parts": [{"text": message.content}]}
            for message in request.messages
        ]

    def _config(self, request: CompletionRequest) -> dict[str, Any]:
        config: dict[str, Any] = {"max_output_tokens": request.max_tokens}
        if request.system:
            config["system_instruction"] = request.system
        if request.temperature is not None:
            config["temperature"] = request.temperature
        if request.stop_sequences:
            config["stop_sequences"] = list(request.stop_sequences)
        if request.tools:
            # Declared as plain dicts: the SDK accepts them, and hand-converting a JSON Schema
            # into `genai_types.Schema` would lose fidelity for no benefit.
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        }
                        for tool in request.tools
                    ]
                }
            ]
        return config

    async def complete(self, request: CompletionRequest) -> CompletionResult:
        model = request.model or DEFAULT_MODEL
        with _translated(self.name):
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=self._contents(request),
                config=self._config(request),
            )

        usage = getattr(response, "usage_metadata", None)
        tool_calls = [
            ToolCall(id=call.name, name=call.name, arguments=dict(call.args or {}))
            for call in (getattr(response, "function_calls", None) or [])
        ]

        return CompletionResult(
            content=response.text or "",
            usage=TokenUsage(
                prompt_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                completion_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            ),
            model=model,
            provider=self.name,
            tool_calls=tool_calls,
            raw_finish_reason=_finish_reason(response),
        )

    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        async def iterator() -> AsyncIterator[str]:
            with _translated(self.name):
                stream = await self._client.aio.models.generate_content_stream(
                    model=request.model or DEFAULT_MODEL,
                    contents=self._contents(request),
                    config=self._config(request),
                )
                async for chunk in stream:
                    if chunk.text:
                        yield chunk.text

        return iterator()


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    return str(reason) if reason is not None else None
