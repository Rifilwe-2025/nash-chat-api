"""Agent tools: configuration, and executing one on the model's behalf (spec §5.2.1 Pattern A).

Two audiences, kept apart the way the API-key service keeps them apart.

**Configuration** is what a signed-in tenant does: define a tool, set the allowlist, disable one
that is misbehaving, read the call log. Tenant-scoped like everything else.

**:meth:`invoke`** is what the conversation engine calls mid-turn, with a name and arguments the
*model* produced. It is the security boundary of this phase, and it is written so that every way a
call can go wrong ends in a sentence the customer can be shown rather than an exception that fails
the turn:

1. the tool must exist, be enabled, and belong to this agent — a model naming a tool it was not
   offered gets nothing;
2. the arguments must satisfy the tool's schema;
3. the resolved URL must be on the agent's allowlist and must not resolve to a private address;
4. the call runs with a short timeout and one retry;
5. the response is mapped to text through the tenant's field allowlist, and truncated.

Every one of those is recorded on ``tool_call`` with its arguments, latency and outcome, because
when an agent gives a wrong answer the first question is always whether the tool or the model was
at fault, and only the log can answer it.

**What never crosses back:** the tenant's credential, the raw response, and the endpoint URL. The
model is given rendered text and nothing else.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src import configs
from src.modules.agents.domain.services import AgentService
from src.modules.tools.domain.models import (
    AgentTool,
    HttpMethod,
    ToolAuthType,
    ToolCallLog,
    ToolOutcome,
    ToolPolicy,
    ToolStatus,
)
from src.modules.tools.domain.repositories import (
    AgentToolRepository,
    ToolCallLogRepository,
    ToolPolicyRepository,
)
from src.modules.tools.internal import allowlist, http_executor, response_mapper, schema
from src.modules.tools.internal.cache import ResponseCache
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException, ValidationException
from src.shared.llm import ToolDefinition

logger = logging.getLogger("api.tools")

# Re-exported so other modules can name the cache without importing this module's `internal/`,
# which is private. `ConversationService` takes one and passes it straight through to us; the
# domain surface is where that type belongs.
__all__ = ["ResponseCache", "ToolResult", "ToolService"]

# What every provider accepts as a function name. Checked here rather than left to the provider,
# because a name the provider rejects fails the whole turn, not just the tool.
TOOL_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")

# What the model is told when a call could not be completed. Phrased as the spec asks (§5.2.1) —
# it admits the failure, does not blame the customer, and does not invent a reason. The *model*
# reads this, not the customer: it is context for composing a reply, which is why it also says what
# to do rather than only what happened.
FAILURE_NOTE = (
    "The {name} lookup could not be completed right now ({reason}). Tell the customer you could "
    "not check that at the moment, apologise briefly, and offer to connect them with someone who "
    "can. Do not invent an answer, and do not repeat this note to them verbatim."
)

REFUSAL_NOTE = (
    "The {name} lookup was not run because the request was not valid ({reason}). Ask the customer "
    "for the missing or corrected detail rather than guessing."
)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One executed call, as the conversation engine sees it."""

    name: str
    text: str
    outcome: ToolOutcome
    duration_ms: int
    call_id: uuid.UUID | None = None

    @property
    def ok(self) -> bool:
        return self.outcome in (ToolOutcome.SUCCEEDED, ToolOutcome.CACHED)


class ToolService:
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        cache: ResponseCache | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.tools = AgentToolRepository(session, tenant_id)
        self.policies = ToolPolicyRepository(session, tenant_id)
        self.calls = ToolCallLogRepository(session)
        self.agents = AgentService(session, tenant_id)
        # A process-wide cache is pinned to app.state; a service built without one simply does not
        # cache, which is what the worker and the tests want.
        self._cache = cache
        self._client = client

    # -- what a turn needs ----------------------------------------------------

    async def definitions_for(self, agent_id: uuid.UUID) -> list[ToolDefinition]:
        """The agent's enabled tools, in the provider-neutral shape (spec §5.3).

        Returns an empty list for an agent with no tools, which is the signal the conversation
        service uses to skip the tool loop entirely — an agent without tools must cost exactly what
        it cost before this phase.
        """
        return [
            ToolDefinition(
                name=tool.name,
                description=tool.description,
                parameters=schema.normalise(tool.request_schema_json),
            )
            for tool in await self.tools.enabled_for_agent(agent_id)
        ]

    async def max_calls_per_turn(self, agent_id: uuid.UUID) -> int:
        policy = await self.policies.for_agent(agent_id)
        configured = policy.max_calls_per_turn if policy else configs.TOOLS_MAX_CALLS_PER_TURN
        return max(1, configured)

    async def invoke(
        self,
        agent_id: uuid.UUID,
        name: str,
        arguments: dict[str, Any],
        conversation_id: uuid.UUID | None = None,
    ) -> ToolResult:
        """Run one tool the model asked for. Never raises — a failure is a result.

        That is the whole contract with the conversation engine: a tool that times out, is refused,
        or returns nonsense must degrade to a sentence the model can work with, because the
        alternative is a customer getting an error page because someone else's API was slow.
        """
        tool = await self.tools.by_name(agent_id, name)
        if tool is None or tool.status is not ToolStatus.ENABLED:
            # The model named something it was not offered, or a tool disabled since the prompt was
            # built. Not logged to `tool_call` — there is no tool row to hang it on.
            logger.warning("agent %s has no enabled tool named %r", agent_id, name)
            return ToolResult(
                name=name,
                text=REFUSAL_NOTE.format(name=name, reason="it is not available"),
                outcome=ToolOutcome.REFUSED,
                duration_ms=0,
            )

        try:
            cleaned = schema.validate(tool.request_schema_json, arguments)
        except schema.SchemaError as exc:
            call = await self._log(
                tool, conversation_id, ToolOutcome.REFUSED, arguments, error=str(exc)
            )
            return ToolResult(
                name=name,
                text=REFUSAL_NOTE.format(name=name, reason=str(exc)),
                outcome=ToolOutcome.REFUSED,
                duration_ms=0,
                call_id=call.id,
            )

        allowed = await self._allowed_hosts(agent_id)
        cached = self._from_cache(tool, cleaned)
        if cached is not None:
            text = self._render(tool, cached)
            call = await self._log(tool, conversation_id, ToolOutcome.CACHED, cleaned, result=text)
            return ToolResult(
                name=name,
                text=text,
                outcome=ToolOutcome.CACHED,
                duration_ms=0,
                call_id=call.id,
            )

        response = await http_executor.execute(tool, cleaned, allowed, client=self._client)

        if not response.ok:
            call = await self._log(
                tool,
                conversation_id,
                response.outcome,
                cleaned,
                status_code=response.status_code,
                duration_ms=response.duration_ms,
                error=response.error_detail,
            )
            await self._record_failure(tool, response.error_detail)
            note = (
                REFUSAL_NOTE if response.outcome is ToolOutcome.REFUSED else FAILURE_NOTE
            ).format(name=name, reason=_reason(response.outcome))
            return ToolResult(
                name=name,
                text=note,
                outcome=response.outcome,
                duration_ms=response.duration_ms,
                call_id=call.id,
            )

        self._to_cache(tool, cleaned, response.payload, response.status_code)
        text = self._render(tool, response.payload)
        call = await self._log(
            tool,
            conversation_id,
            ToolOutcome.SUCCEEDED,
            cleaned,
            status_code=response.status_code,
            duration_ms=response.duration_ms,
            result=text,
        )
        await self._record_success(tool)

        return ToolResult(
            name=name,
            text=text,
            outcome=ToolOutcome.SUCCEEDED,
            duration_ms=response.duration_ms,
            call_id=call.id,
        )

    async def try_out(
        self, tool_id: uuid.UUID, arguments: dict[str, Any]
    ) -> tuple[AgentTool, ToolResult]:
        """Run a tool as the tenant, from the console, before trusting it to a customer.

        The same path :meth:`invoke` takes — deliberately, since a test that exercised a different
        code path would prove nothing about what happens in a real turn.
        """
        tool = await self.get(tool_id)
        return tool, await self.invoke(tool.agent_id, tool.name, arguments)

    # -- configuration --------------------------------------------------------

    async def get(self, tool_id: uuid.UUID) -> AgentTool:
        tool = await self.tools.get(tool_id)
        if tool is None:
            raise NotFoundException("Tool does not exist.", code="TOOL_NOT_FOUND")
        return tool

    async def list_tools(self, agent_id: uuid.UUID, page: PageRequest) -> Page[AgentTool]:
        await self.agents.get(agent_id)
        return await self.tools.list_for_agent(agent_id, page)

    async def create(
        self,
        agent_id: uuid.UUID,
        name: str,
        description: str,
        endpoint_url: str,
        http_method: HttpMethod = HttpMethod.GET,
        auth_type: ToolAuthType = ToolAuthType.NONE,
        auth_config: dict[str, Any] | None = None,
        request_schema: dict[str, Any] | None = None,
        response_mapping: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        cache_ttl_seconds: int = 0,
    ) -> AgentTool:
        """Define a tool, refusing anything that could not work or should not be allowed."""
        agent = await self.agents.get(agent_id)
        self._validate_name(name)
        self._validate_description(description)
        host = self._validate_endpoint(endpoint_url, request_schema)

        if await self.tools.by_name(agent.id, name) is not None:
            raise ConflictException(
                f"This agent already has a tool named {name!r}.", code="TOOL_NAME_TAKEN"
            )

        # The *first* tool seeds the allowlist with its own host: a tenant who has just defined one
        # endpoint plainly means to allow it, and making them add it twice would teach them the
        # allowlist is bureaucracy rather than a control. Every tool after that is created against
        # the allowlist the tenant already has — if its host is not on the list the tool is refused
        # at call time, and adding the host is a separate, deliberate act.
        await self._ensure_policy(agent.id, seed_host=host)

        return await self.tools.add(
            AgentTool(
                agent_id=agent.id,
                name=name,
                description=description.strip(),
                endpoint_url=endpoint_url,
                http_method=http_method,
                auth_type=auth_type,
                auth_config_json=auth_config or {},
                request_schema_json=schema.normalise(request_schema),
                response_mapping_json=response_mapping or {},
                status=ToolStatus.ENABLED,
                timeout_seconds=timeout_seconds,
                cache_ttl_seconds=max(0, cache_ttl_seconds),
            )
        )

    async def update(self, tool_id: uuid.UUID, changes: dict[str, Any]) -> AgentTool:
        """Apply a partial update. An omitted field is left as it was."""
        tool = await self.get(tool_id)
        applied: dict[str, Any] = {}

        if "name" in changes:
            name = str(changes["name"])
            self._validate_name(name)
            existing = await self.tools.by_name(tool.agent_id, name)
            if existing is not None and existing.id != tool.id:
                raise ConflictException(
                    f"This agent already has a tool named {name!r}.", code="TOOL_NAME_TAKEN"
                )
            applied["name"] = name

        if "description" in changes:
            self._validate_description(str(changes["description"]))
            applied["description"] = str(changes["description"]).strip()

        if "endpoint_url" in changes or "request_schema" in changes:
            endpoint = str(changes.get("endpoint_url") or tool.endpoint_url)
            request_schema = changes.get("request_schema", tool.request_schema_json)
            self._validate_endpoint(endpoint, request_schema)
            applied["endpoint_url"] = endpoint
            if "request_schema" in changes:
                applied["request_schema_json"] = schema.normalise(request_schema)

        for field, column in (
            ("http_method", "http_method"),
            ("auth_type", "auth_type"),
            ("status", "status"),
            ("auth_config", "auth_config_json"),
            ("response_mapping", "response_mapping_json"),
            ("timeout_seconds", "timeout_seconds"),
        ):
            if field in changes:
                applied[column] = changes[field]

        if "cache_ttl_seconds" in changes:
            applied["cache_ttl_seconds"] = max(0, int(changes["cache_ttl_seconds"]))

        if not applied:
            return tool
        return await self.tools.update(tool, **applied)

    async def delete(self, tool_id: uuid.UUID) -> None:
        await self.tools.delete(await self.get(tool_id))

    async def call_log(self, tool_id: uuid.UUID, page: PageRequest) -> Page[ToolCallLog]:
        tool = await self.get(tool_id)
        return await self.calls.list_for_tool(tool.id, page)

    # -- the allowlist --------------------------------------------------------

    async def get_policy(self, agent_id: uuid.UUID) -> ToolPolicy:
        await self.agents.get(agent_id)
        return await self._ensure_policy(agent_id)

    async def set_policy(
        self,
        agent_id: uuid.UUID,
        allowed_hosts: list[str] | None = None,
        max_calls_per_turn: int | None = None,
    ) -> ToolPolicy:
        policy = await self.get_policy(agent_id)
        changes: dict[str, Any] = {}

        if allowed_hosts is not None:
            changes["allowed_hosts"] = allowlist.normalise_hosts(allowed_hosts)
        if max_calls_per_turn is not None:
            if max_calls_per_turn < 1:
                raise ValidationException(
                    "An agent must be allowed at least one tool call per turn.",
                    code="TOOL_POLICY_INVALID",
                )
            changes["max_calls_per_turn"] = max_calls_per_turn

        if not changes:
            return policy
        return await self.policies.update(policy, **changes)

    async def _allowed_hosts(self, agent_id: uuid.UUID) -> list[str]:
        policy = await self.policies.for_agent(agent_id)
        return [str(host) for host in (policy.allowed_hosts if policy else [])]

    async def _ensure_policy(self, agent_id: uuid.UUID, seed_host: str | None = None) -> ToolPolicy:
        policy = await self.policies.for_agent(agent_id)
        if policy is None:
            return await self.policies.add(
                ToolPolicy(
                    agent_id=agent_id,
                    allowed_hosts=[seed_host] if seed_host else [],
                    max_calls_per_turn=configs.TOOLS_MAX_CALLS_PER_TURN,
                )
            )

        # Deliberately does *not* add the host to a policy that already exists. Seeding happens
        # once, when the policy is created with the agent's first tool; after that the allowlist is
        # a decision the tenant has made, and adding a tool must not quietly widen it. That is the
        # whole reason the list lives on the agent rather than on each tool (see domain/models.py):
        # "a policy that lived on each tool could be widened by adding another tool, which is
        # precisely the thing an allowlist exists to prevent."
        return policy

    # -- internals ------------------------------------------------------------

    def _render(self, tool: AgentTool, payload: Any) -> str:
        return response_mapper.render(
            payload, tool.response_mapping_json, configs.TOOLS_MAX_RESULT_CHARACTERS
        )

    def _from_cache(self, tool: AgentTool, arguments: dict[str, Any]) -> Any | None:
        if self._cache is None or tool.cache_ttl_seconds <= 0:
            return None
        entry = self._cache.get(self._cache.key(tool.id, arguments))
        return entry.payload if entry is not None else None

    def _to_cache(
        self, tool: AgentTool, arguments: dict[str, Any], payload: Any, status: int | None
    ) -> None:
        if self._cache is None or tool.cache_ttl_seconds <= 0:
            return
        self._cache.put(
            self._cache.key(tool.id, arguments), payload, status, tool.cache_ttl_seconds
        )

    async def _log(
        self,
        tool: AgentTool,
        conversation_id: uuid.UUID | None,
        outcome: ToolOutcome,
        arguments: dict[str, Any],
        status_code: int | None = None,
        duration_ms: int = 0,
        result: str | None = None,
        error: str | None = None,
    ) -> ToolCallLog:
        return await self.calls.add(
            ToolCallLog(
                tool_id=tool.id,
                conversation_id=conversation_id,
                outcome=outcome,
                arguments_json=arguments,
                status_code=status_code,
                duration_ms=duration_ms,
                result_text=result,
                error_detail=error[:500] if error else None,
            )
        )

    async def _record_success(self, tool: AgentTool) -> None:
        await self.tools.update(
            tool, last_called_at=datetime.now(UTC), consecutive_failures=0, last_error=None
        )

    async def _record_failure(self, tool: AgentTool, detail: str | None) -> None:
        await self.tools.update(
            tool,
            last_called_at=datetime.now(UTC),
            consecutive_failures=tool.consecutive_failures + 1,
            last_error=(detail or "")[:500] or None,
        )

    # -- validation -----------------------------------------------------------

    def _validate_name(self, name: str) -> None:
        if not TOOL_NAME.match(name):
            raise ValidationException(
                "A tool name must start with a letter and contain only letters, digits, "
                "underscores and hyphens (up to 64 characters).",
                code="TOOL_NAME_INVALID",
            )

    def _validate_description(self, description: str) -> None:
        """The description is prompt text, so an empty one is a tool the model cannot choose."""
        if len(description.strip()) < 10:
            raise ValidationException(
                "Describe what this tool does and when to use it — the model reads this to decide "
                "whether to call it, so a few words is not enough.",
                code="TOOL_DESCRIPTION_TOO_SHORT",
            )

    def _validate_endpoint(self, endpoint_url: str, request_schema: Any) -> str:
        """Check the endpoint is usable and that its placeholders are declared.

        The address check runs at save time as well as at call time. Catching it here means a tenant
        finds out while they are looking at the form, rather than through an agent that silently
        fails to answer.
        """
        try:
            host = allowlist.hostname_of(endpoint_url)
        except allowlist.ToolSecurityError as exc:
            raise ValidationException(str(exc), code="TOOL_ENDPOINT_INVALID") from exc

        placeholders = allowlist.path_placeholders(endpoint_url)
        if placeholders:
            declared = schema.normalise(
                request_schema if isinstance(request_schema, dict) else None
            ).get("properties")
            known = set(declared) if isinstance(declared, dict) else set()
            undeclared = [name for name in placeholders if name not in known]
            if undeclared:
                raise ValidationException(
                    f"The endpoint uses {', '.join(undeclared)} but the request schema does not "
                    f"declare {'it' if len(undeclared) == 1 else 'them'}. The model can only fill "
                    f"in a placeholder it has been told about.",
                    code="TOOL_PLACEHOLDER_UNDECLARED",
                )
        return host


def _reason(outcome: ToolOutcome) -> str:
    """A short, non-technical cause for the note the model reads.

    Deliberately vague about *why*: the model is composing a customer-facing sentence, and the
    detail belongs in the call log where a tenant can act on it, not in a chat window.
    """
    if outcome is ToolOutcome.TIMED_OUT:
        return "it took too long to respond"
    if outcome is ToolOutcome.REFUSED:
        return "the request was not valid"
    return "the service is unavailable"
