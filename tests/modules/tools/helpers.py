"""Scaffolding for the agent-tools tests.

Two fakes, and both are deliberately thin about the thing they replace and honest about everything
else.

:class:`ToolCallingLLM` stands in for a provider that supports function calling. It is *scripted*:
each entry says either "ask for this tool" or "answer with this text", so a test can set up the
exact sequence it wants to exercise — a single lookup, two in a row, a model that never stops
asking — none of which a real model would reproduce on demand.

The outbound HTTP is stubbed with ``httpx.MockTransport``, not by patching the executor. That means
every test still runs the real allowlist check, the real credential injection, the real timeout
handling and the real response mapping — the pieces that are actually under test — and only the
socket is missing.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent, ModelProvider
from src.modules.agents.domain.services import AgentService
from src.modules.tenants.domain.models import Tenant
from src.modules.tools.domain.models import AgentTool, HttpMethod, ToolAuthType
from src.modules.tools.domain.services import ToolService
from src.shared.llm import CompletionRequest, CompletionResult, TokenUsage, ToolCall

HOST = "api.example.test"
ENDPOINT = f"https://{HOST}/orders/{{orderId}}"

ORDER = {
    "data": {
        "orderId": "A-10432",
        "status": "Out for delivery",
        "eta": "Tomorrow before 5pm",
        # Present on purpose: the mapper must be able to keep this out of the prompt, and a test
        # that asserts it never reaches the model needs it to exist in the payload.
        "customerPaymentToken": "tok_live_should_never_be_seen",
    }
}

ORDER_MAPPING: dict[str, Any] = {
    "root": "data",
    "fields": {"status": "Status", "eta": "Estimated delivery"},
}

ORDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"orderId": {"type": "string"}},
    "required": ["orderId"],
}


class Script:
    """One scripted provider turn."""

    def __init__(self, text: str = "", calls: list[ToolCall] | None = None) -> None:
        self.text = text
        self.calls = calls or []


def asks_for(name: str, **arguments: Any) -> Script:
    """A turn where the model calls a tool instead of answering."""
    return Script(
        calls=[ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=name, arguments=arguments)]
    )


def answers(text: str) -> Script:
    """A turn where the model replies."""
    return Script(text=text)


class ToolCallingLLM:
    """A provider that follows a script, and records every request it was given."""

    def __init__(self, *script: Script) -> None:
        # An empty script answers once and stops, which is what the tests that do not care about
        # the model's behaviour want.
        self.script = list(script) or [answers("Sure — how can I help?")]
        self.requests: list[tuple[str, CompletionRequest]] = []
        self.calls_made = 0

    async def complete(
        self, provider: str, request: CompletionRequest, api_key: str | None = None
    ) -> CompletionResult:
        self.requests.append((provider, request))
        step = self.script[min(self.calls_made, len(self.script) - 1)]
        self.calls_made += 1

        return CompletionResult(
            content=step.text,
            usage=TokenUsage(prompt_tokens=100, completion_tokens=20),
            model=request.model,
            provider=provider,
            tool_calls=list(step.calls),
        )

    def stream(self, provider: str, request: CompletionRequest, api_key: str | None = None):  # type: ignore[no-untyped-def]
        self.requests.append((provider, request))
        text = self.script[-1].text

        async def iterator() -> AsyncIterator[str]:
            yield text

        return iterator()

    @property
    def last(self) -> CompletionRequest:
        return self.requests[-1][1]

    def prompt_text(self) -> str:
        """Every message the model was ever handed, concatenated.

        Used by the tests that assert something never reached the model at all — a credential, a
        field the mapping excluded — where checking only the final request would miss a leak in an
        earlier one.
        """
        parts: list[str] = []
        for _, request in self.requests:
            parts.append(request.system or "")
            for message in request.messages:
                parts.append(message.content)
                for call in message.tool_calls:
                    parts.append(str(call.arguments))
        return "\n".join(parts)


# -- the stub endpoint -------------------------------------------------------------------


def mock_client(
    handler: Callable[[httpx.Request], httpx.Response], timeout: float = 5.0
) -> httpx.AsyncClient:
    """An httpx client whose transport is a function, so no socket is opened."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)


def order_endpoint(
    payload: Any = None, status_code: int = 200
) -> Callable[[httpx.Request], httpx.Response]:
    """A stub of the tenant's own API, answering with a JSON order."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload if payload is not None else ORDER)

    return handler


def recording_endpoint(
    seen: list[httpx.Request], payload: Any = None
) -> Callable[[httpx.Request], httpx.Response]:
    """A stub that keeps every request, so a test can inspect headers and the URL called."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload if payload is not None else ORDER)

    return handler


def timing_out_endpoint() -> Callable[[httpx.Request], httpx.Response]:
    """A stub that behaves exactly as a slow endpoint does from httpx's point of view."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    return handler


# -- tenant setup ------------------------------------------------------------------------


async def build_agent(session: AsyncSession, tenant: Tenant, published: bool = True) -> Agent:
    service = AgentService(session, tenant.id)
    agent = await service.create(
        name=f"Agent {uuid.uuid4().hex[:6]}",
        persona="You are the support assistant for Nash Paints.",
        model_provider=ModelProvider.GEMINI,
        model_settings={"model": "gemini-2.0-flash", "temperature": 0.4, "max_tokens": 512},
    )
    if published:
        agent = await service.publish(agent.id)
    return agent


async def add_order_tool(
    tools: ToolService,
    agent: Agent,
    endpoint: str = ENDPOINT,
    auth_type: ToolAuthType = ToolAuthType.NONE,
    auth_config: dict[str, Any] | None = None,
    **overrides: Any,
) -> AgentTool:
    return await tools.create(
        agent.id,
        name=overrides.pop("name", "check_order_status"),
        description=overrides.pop(
            "description",
            "Look up the status and estimated delivery of a customer's order by its order number.",
        ),
        endpoint_url=endpoint,
        http_method=overrides.pop("http_method", HttpMethod.GET),
        auth_type=auth_type,
        auth_config=auth_config,
        request_schema=overrides.pop("request_schema", ORDER_SCHEMA),
        response_mapping=overrides.pop("response_mapping", ORDER_MAPPING),
        **overrides,
    )
