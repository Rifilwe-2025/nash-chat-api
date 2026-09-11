"""How the platform's rows read to a coding agent.

The same camelCase shapes the REST API returns, so an agent that has read the OpenAPI schema — or a
developer comparing the two — sees one vocabulary. Written field by field rather than dumped from
the models, for the reason the REST responses are: **what is left out is the point.** An agent's
provider key, a tool's credential, a channel's WhatsApp token and an API source's connector config
never appear here, so no tool can hand one to a language model by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from pydantic.alias_generators import to_camel
from pydantic_core import to_jsonable_python

from src.modules.agents.domain.models import Agent, AgentVersion
from src.modules.agents.presentation.dtos.agent import EngagementRules, Guardrails, ModelSettings
from src.modules.api_keys.domain.models import ApiKey
from src.modules.channels.domain.models import ChannelConfig
from src.modules.conversations.domain.models import Conversation, Message
from src.modules.knowledge_base.domain.models import KbSource, KnowledgeBase
from src.modules.tools.domain.models import AgentTool, ToolCallLog, ToolPolicy
from src.shared.database.pagination import Page

T = TypeVar("T")
Json = dict[str, Any]


def jsonable(value: Any) -> Any:
    """UUIDs, datetimes, enums and dataclasses, as plain JSON values."""
    return to_jsonable_python(value)


def json_object(value: Json) -> Json:
    """:func:`jsonable` for a mapping, typed as one — what every tool hands back."""
    converted: Json = to_jsonable_python(value)
    return converted


def camelised(value: Any) -> Any:
    """Rename snake_case keys to camelCase, recursively.

    For the platform's own dataclasses (analytics reports, retrieval explanations) — never for data
    a tenant or a model wrote, such as tool arguments, whose keys are theirs to name.
    """
    if isinstance(value, dict):
        return {
            to_camel(key) if isinstance(key, str) else key: camelised(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [camelised(item) for item in value]
    return value


def page(result: Page[T], render: Callable[[T], Json]) -> Json:
    total_pages = (
        (result.total + result.page_size - 1) // result.page_size if result.page_size else 0
    )
    return {
        "items": [render(item) for item in result.items],
        "page": result.page,
        "pageSize": result.page_size,
        "totalItems": result.total,
        "totalPages": total_pages,
    }


# -- agents ---------------------------------------------------------------------------


def agent_summary(agent: Agent) -> Json:
    return json_object(
        {
            "id": agent.id,
            "name": agent.name,
            "status": agent.status,
            "version": agent.version,
            "modelProvider": agent.model_provider,
            "model": agent.model_config_json.get("model"),
            "hasModelApiKey": bool(agent.model_api_key),
            "updatedAt": agent.updated_at,
        }
    )


def agent(agent: Agent) -> Json:
    settings = agent.model_config_json
    return json_object(
        {
            **agent_summary(agent),
            "persona": agent.persona,
            "engagementRules": EngagementRules.model_validate(agent.engagement_rules).model_dump(
                by_alias=True
            ),
            "guardrails": Guardrails.model_validate(agent.guardrails).model_dump(by_alias=True),
            "modelSettings": (
                ModelSettings.model_validate(settings).model_dump(by_alias=True)
                if settings.get("model")
                else None
            ),
            "createdAt": agent.created_at,
        }
    )


def agent_version(snapshot: AgentVersion) -> Json:
    return json_object(
        {
            "version": snapshot.version,
            "note": snapshot.note,
            "config": snapshot.snapshot,
            "createdAt": snapshot.created_at,
        }
    )


# -- knowledge ------------------------------------------------------------------------


def knowledge_base(knowledge_base: KnowledgeBase) -> Json:
    return json_object(
        {
            "id": knowledge_base.id,
            "name": knowledge_base.name,
            "description": knowledge_base.description,
            "retrievalTier": knowledge_base.retrieval_tier,
            "redactPii": knowledge_base.redact_pii,
            "createdAt": knowledge_base.created_at,
            "updatedAt": knowledge_base.updated_at,
        }
    )


def source(source: KbSource) -> Json:
    """A source without its text. ``config_json`` is not echoed: an API source's holds a connector
    configuration that may carry the tenant's credentials — only the harmless parts are named."""
    config = source.config_json or {}
    return json_object(
        {
            "id": source.id,
            "kbId": source.kb_id,
            "name": source.name,
            "type": source.type,
            "status": source.status,
            "url": config.get("url"),
            "filename": config.get("filename"),
            "mediaType": config.get("mediaType"),
            "byteSize": source.byte_size,
            "characters": len(source.extracted_text or ""),
            "errorDetail": source.error_detail,
            "syncIntervalMinutes": source.sync_interval_minutes,
            "lastSyncedAt": source.last_synced_at,
            "nextSyncAt": source.next_sync_at,
            "consecutiveFailures": source.consecutive_failures,
            "createdAt": source.created_at,
        }
    )


# -- tools ----------------------------------------------------------------------------


def tool(tool: AgentTool) -> Json:
    return json_object(
        {
            "id": tool.id,
            "agentId": tool.agent_id,
            "name": tool.name,
            "description": tool.description,
            "endpointUrl": tool.endpoint_url,
            "httpMethod": tool.http_method,
            "authType": tool.auth_type,
            "hasCredential": bool(tool.auth_config_json),
            "requestSchema": tool.request_schema_json,
            "responseMapping": tool.response_mapping_json,
            "status": tool.status,
            "timeoutSeconds": tool.timeout_seconds,
            "cacheTtlSeconds": tool.cache_ttl_seconds,
            "lastCalledAt": tool.last_called_at,
            "consecutiveFailures": tool.consecutive_failures,
            "lastError": tool.last_error,
            "createdAt": tool.created_at,
            "updatedAt": tool.updated_at,
        }
    )


def tool_policy(policy: ToolPolicy) -> Json:
    return json_object(
        {
            "agentId": policy.agent_id,
            "allowedHosts": [str(host) for host in policy.allowed_hosts],
            "maxCallsPerTurn": policy.max_calls_per_turn,
            "updatedAt": policy.updated_at,
        }
    )


def tool_call(call: ToolCallLog) -> Json:
    return json_object(
        {
            "id": call.id,
            "outcome": call.outcome,
            "arguments": call.arguments_json,
            "statusCode": call.status_code,
            "durationMs": call.duration_ms,
            "resultText": call.result_text,
            "errorDetail": call.error_detail,
            "conversationId": call.conversation_id,
            "createdAt": call.created_at,
        }
    )


# -- conversations --------------------------------------------------------------------


def conversation(conversation: Conversation) -> Json:
    return json_object(
        {
            "id": conversation.id,
            "agentId": conversation.agent_id,
            "channel": conversation.channel,
            "externalUserId": conversation.external_user_id,
            "status": conversation.status,
            "escalationReason": conversation.escalation_reason,
            "lastMessageAt": conversation.last_message_at,
            "createdAt": conversation.created_at,
        }
    )


def message(message: Message) -> Json:
    return json_object(
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "model": message.model,
            "promptTokens": message.prompt_tokens,
            "completionTokens": message.completion_tokens,
            "costMicroUsd": message.cost_micro_usd,
            "citations": list(message.citations_json or []),
            "createdAt": message.created_at,
        }
    )


# -- channels and keys ----------------------------------------------------------------


def channel_config(config: ChannelConfig) -> Json:
    """Settings only. ``credentials_json`` holds a WhatsApp access token and is never returned."""
    return json_object(
        {
            "id": config.id,
            "agentId": config.agent_id,
            "channelType": config.channel_type,
            "status": config.status,
            "settings": config.settings_json,
            "updatedAt": config.updated_at,
        }
    )


def api_key(api_key: ApiKey) -> Json:
    return json_object(
        {
            "id": api_key.id,
            "agentId": api_key.agent_id,
            "name": api_key.name,
            "prefix": api_key.prefix,
            "scopes": [str(scope) for scope in api_key.scopes],
            "rateLimitPerMinute": api_key.rate_limit_per_minute,
            "lastUsedAt": api_key.last_used_at,
            "revokedAt": api_key.revoked_at,
            "expiresAt": api_key.expires_at,
            "active": api_key.is_active,
            "createdAt": api_key.created_at,
        }
    )
