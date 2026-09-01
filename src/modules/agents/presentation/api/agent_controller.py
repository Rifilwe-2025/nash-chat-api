from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Path

from src.modules.agents.domain.models import Agent, AgentVersion, ModelProvider
from src.modules.agents.domain.services import AgentService
from src.modules.agents.presentation.dtos.agent import (
    AgentResponse,
    AgentSummaryResponse,
    AgentVersionResponse,
    CreateAgentRequest,
    EngagementRules,
    Guardrails,
    ModelKeyTestRequest,
    ModelKeyTestResponse,
    ModelSettings,
    ProviderKeyTestRequest,
    RollbackRequest,
    UpdateAgentRequest,
    key_hint,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.llm.verification import KeyCheck, verify_key
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/agents", tags=["agents"])


def get_agent_service(session: SessionDep, tenant_id: CurrentTenantDep) -> AgentService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return AgentService(session, tenant_id)


AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
AgentIdPath = Annotated[uuid.UUID, Path(description="Identifier of the agent.")]
VersionPath = Annotated[int, Path(ge=1, description="Version number from the history list.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_FOUND = {
    "description": (
        "No such agent in your tenant (`AGENT_NOT_FOUND`). Another tenant's agent is reported as "
        "missing rather than forbidden, so identifiers cannot be probed."
    )
}


def _agent(agent: Agent) -> AgentResponse:
    return AgentResponse(
        id=agent.id,
        tenant_id=agent.tenant_id,
        name=agent.name,
        status=agent.status,
        version=agent.version,
        persona=agent.persona,
        engagement_rules=EngagementRules.model_validate(agent.engagement_rules),
        guardrails=Guardrails.model_validate(agent.guardrails),
        model_provider=agent.model_provider,
        model_settings=(
            ModelSettings.model_validate(agent.model_config_json)
            if agent.model_config_json.get("model")
            else None
        ),
        # The key itself is never in a response, on any route. What a builder screen needs is
        # whether one is there and which one it is, and the hint answers both without the value
        # ever crossing the wire a second time.
        has_model_api_key=bool(agent.model_api_key),
        model_api_key_hint=key_hint(agent.model_api_key),
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _summary(agent: Agent) -> AgentSummaryResponse:
    return AgentSummaryResponse(
        id=agent.id,
        name=agent.name,
        status=agent.status,
        version=agent.version,
        model_provider=agent.model_provider,
        has_model_api_key=bool(agent.model_api_key),
        updated_at=agent.updated_at,
    )


def _key_check(check: KeyCheck) -> ModelKeyTestResponse:
    return ModelKeyTestResponse(
        ok=check.ok,
        status=check.status,
        provider=ModelProvider(check.provider),
        model=check.model,
        latency_ms=check.latency_ms,
        detail=check.detail,
    )


def _version(snapshot: AgentVersion) -> AgentVersionResponse:
    return AgentVersionResponse(
        version=snapshot.version,
        note=snapshot.note,
        config=snapshot.snapshot,
        created_at=snapshot.created_at,
    )


@router.post(
    "",
    response_model=ApiResponse[AgentResponse],
    status_code=201,
    summary="Create an agent",
    description=(
        "Creates an agent in your tenant as a **draft** at version 1. Configuration may be "
        "incomplete at this point — a persona and model are only required at publish time. "
        "History starts empty; the first edit snapshots version 1 so it can be restored."
    ),
    responses={
        201: {"description": "The agent was created as a draft."},
        401: UNAUTHORIZED,
        409: {"description": "You already have an agent with that name (`AGENT_NAME_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def create_agent(
    payload: CreateAgentRequest, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    agent = await service.create(
        name=payload.name,
        persona=payload.persona,
        engagement_rules=payload.engagement_rules.model_dump(),
        guardrails=payload.guardrails.model_dump(),
        model_provider=payload.model_provider,
        model_settings=payload.model_settings.model_dump() if payload.model_settings else None,
        model_api_key=payload.model_api_key,
    )
    return ApiResponse.ok(_agent(agent), message="Agent created.")


@router.post(
    "/model-key/test",
    response_model=ApiResponse[ModelKeyTestResponse],
    summary="Test a provider API key",
    description=(
        "Checks a provider key **without storing it anywhere**, by making the smallest real call "
        "the provider allows against the model you name. Use it before creating an agent, or "
        "whenever a key has been typed but not saved.\n\n"
        "A rejected key is a **200 with `ok: false`** — the request to test succeeded, and its "
        "answer is that the credential does not work. Read `status` to know what to change: "
        "`invalid_key` is the key, `model_rejected` is the model name or this account's access to "
        "it, and `unavailable` is the provider being unreachable, which says nothing about "
        "either.\n\n"
        "The key is used for one request and discarded. It is not logged."
    ),
    responses={
        200: {"description": "The provider was asked. `ok` says what it answered."},
        401: UNAUTHORIZED,
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def test_provider_key(
    payload: ProviderKeyTestRequest, _: CurrentTenantDep
) -> ApiResponse[ModelKeyTestResponse]:
    """Declared ahead of the `/{agent_id}` routes so `model-key` is read as a literal segment."""
    check = await verify_key(
        payload.model_provider.value, payload.model.strip(), payload.model_api_key.strip()
    )
    return ApiResponse.ok(_key_check(check))


@router.get(
    "",
    response_model=PaginatedResponse[AgentSummaryResponse],
    summary="List agents",
    description=(
        "Lists the agents in your tenant, newest first. Returns summaries; fetch a single "
        "agent for its full configuration."
    ),
    responses={200: {"description": "A page of your agents."}, 401: UNAUTHORIZED},
)
async def list_agents(
    service: AgentServiceDep, page: PageParamsDep
) -> PaginatedResponse[AgentSummaryResponse]:
    result = await service.list_agents(page)
    return PaginatedResponse.of(
        items=[_summary(agent) for agent in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/{agent_id}",
    response_model=ApiResponse[AgentResponse],
    summary="Get an agent",
    description="Returns one agent's full configuration.",
    responses={200: {"description": "The agent."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def get_agent(agent_id: AgentIdPath, service: AgentServiceDep) -> ApiResponse[AgentResponse]:
    return ApiResponse.ok(_agent(await service.get(agent_id)))


@router.patch(
    "/{agent_id}",
    response_model=ApiResponse[AgentResponse],
    summary="Update an agent's configuration",
    description=(
        "Applies a partial update; omitted fields are left unchanged. **The previous configuration "
        "is snapshotted first** and the version number increments, so any edit can be rolled back. "
        "A request that changes nothing is a no-op and does not create a version."
    ),
    responses={
        200: {"description": "The updated agent."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        409: {"description": "Another agent already uses that name (`AGENT_NAME_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def update_agent(
    agent_id: AgentIdPath, payload: UpdateAgentRequest, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    changes: dict[str, object] = {
        "name": payload.name,
        "persona": payload.persona,
        "engagement_rules": (
            payload.engagement_rules.model_dump() if payload.engagement_rules else None
        ),
        "guardrails": payload.guardrails.model_dump() if payload.guardrails else None,
        "model_provider": payload.model_provider,
        "model_config_json": (
            payload.model_settings.model_dump() if payload.model_settings else None
        ),
    }
    # Applied on its own, before the configuration edit, and deliberately not part of `changes`:
    # a credential is not versioned, so it must not be what turns a no-op PATCH into a new version.
    # Ordering matters only in that the agent returned below has to carry the new hint.
    if payload.model_api_key is not None:
        await service.set_model_api_key(agent_id, payload.model_api_key)
    return ApiResponse.ok(_agent(await service.update(agent_id, changes)))


@router.delete(
    "/{agent_id}",
    response_model=ApiResponse[None],
    summary="Delete an agent",
    description="Permanently removes the agent and its version history.",
    responses={200: {"description": "The agent was deleted."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def delete_agent(agent_id: AgentIdPath, service: AgentServiceDep) -> ApiResponse[None]:
    await service.delete(agent_id)
    return ApiResponse.ok(message="Agent deleted.")


@router.post(
    "/{agent_id}/publish",
    response_model=ApiResponse[AgentResponse],
    summary="Publish an agent",
    description=(
        "Moves a draft or paused agent to **published**. Rejected while the agent is incomplete: "
        "it needs a persona, a model provider, and a model. Publishing an already published agent "
        "is a no-op."
    ),
    responses={
        200: {"description": "The agent is published."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        409: {"description": "That transition is not allowed (`INVALID_STATUS_TRANSITION`)."},
        422: {
            "description": (
                "The agent is missing configuration required to publish "
                "(`AGENT_NOT_PUBLISHABLE`); the detail lists what is absent."
            )
        },
    },
)
async def publish_agent(
    agent_id: AgentIdPath, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    return ApiResponse.ok(_agent(await service.publish(agent_id)), message="Agent published.")


@router.post(
    "/{agent_id}/pause",
    response_model=ApiResponse[AgentResponse],
    summary="Pause an agent",
    description=(
        "Stops a published agent from serving traffic while keeping its configuration and "
        "integrations intact. Only a published agent can be paused."
    ),
    responses={
        200: {"description": "The agent is paused."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        409: {"description": "Only a published agent can be paused (`INVALID_STATUS_TRANSITION`)."},
    },
)
async def pause_agent(
    agent_id: AgentIdPath, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    return ApiResponse.ok(_agent(await service.pause(agent_id)), message="Agent paused.")


@router.post(
    "/{agent_id}/unpublish",
    response_model=ApiResponse[AgentResponse],
    summary="Return an agent to draft",
    description="Takes a published or paused agent back to **draft** so it can be reworked.",
    responses={
        200: {"description": "The agent is a draft again."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        409: {"description": "That transition is not allowed (`INVALID_STATUS_TRANSITION`)."},
    },
)
async def unpublish_agent(
    agent_id: AgentIdPath, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    return ApiResponse.ok(_agent(await service.unpublish(agent_id)))


@router.get(
    "/{agent_id}/versions",
    response_model=ApiResponse[list[AgentVersionResponse]],
    summary="List configuration versions",
    description=(
        "Returns the agent's configuration history, newest first. Each entry is the configuration "
        "as it was *before* the change that superseded it."
    ),
    responses={200: {"description": "Version history."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def list_versions(
    agent_id: AgentIdPath, service: AgentServiceDep
) -> ApiResponse[list[AgentVersionResponse]]:
    versions = await service.list_versions(agent_id)
    return ApiResponse.ok([_version(snapshot) for snapshot in versions])


@router.get(
    "/{agent_id}/versions/{version}",
    response_model=ApiResponse[AgentVersionResponse],
    summary="Get one configuration version",
    description="Returns a single snapshot so it can be inspected before rolling back to it.",
    responses={
        200: {"description": "The snapshot."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent or version (`AGENT_VERSION_NOT_FOUND`)."},
    },
)
async def get_version(
    agent_id: AgentIdPath, version: VersionPath, service: AgentServiceDep
) -> ApiResponse[AgentVersionResponse]:
    return ApiResponse.ok(_version(await service.get_version(agent_id, version)))


@router.post(
    "/{agent_id}/versions/{version}/rollback",
    response_model=ApiResponse[AgentResponse],
    summary="Roll back to an earlier version",
    description=(
        "Restores an earlier configuration. History is never rewritten: the current configuration "
        "is snapshotted first and the restore lands as a **new** version, so a rollback can itself "
        "be rolled back. Status is not changed — a published agent stays published."
    ),
    responses={
        200: {"description": "The earlier configuration is now live."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent or version (`AGENT_VERSION_NOT_FOUND`)."},
    },
)
async def rollback(
    agent_id: AgentIdPath,
    version: VersionPath,
    payload: RollbackRequest,
    service: AgentServiceDep,
) -> ApiResponse[AgentResponse]:
    agent = await service.rollback(agent_id, version, note=payload.note)
    return ApiResponse.ok(_agent(agent), message=f"Rolled back to version {version}.")


@router.post(
    "/{agent_id}/model-key/test",
    response_model=ApiResponse[ModelKeyTestResponse],
    summary="Test this agent's provider key",
    description=(
        "Makes a real call to the agent's provider and reports whether it worked.\n\n"
        "Send an empty body to test the key that is **stored** on the agent — the check to run "
        "after a key is saved, or when a working agent suddenly stops answering. Send "
        "`modelApiKey` to test one that has been typed into the builder but not saved yet, and "
        "`model` to try a different model without changing the agent. Neither is written: a "
        "passing test does not store the key that passed.\n\n"
        "As with the standalone check, a credential that does not work is a **200 with "
        "`ok: false`**, not an error."
    ),
    responses={
        200: {"description": "The provider was asked. `ok` says what it answered."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: {
            "description": (
                "The agent has no provider or no model selected, so there is nothing to test "
                "against (`AGENT_NOT_CONFIGURED`)."
            )
        },
    },
)
async def test_agent_model_key(
    agent_id: AgentIdPath,
    payload: ModelKeyTestRequest,
    service: AgentServiceDep,
) -> ApiResponse[ModelKeyTestResponse]:
    check = await service.verify_model_key(
        agent_id, api_key=payload.model_api_key, model=payload.model
    )
    return ApiResponse.ok(_key_check(check))


@router.delete(
    "/{agent_id}/model-key",
    response_model=ApiResponse[AgentResponse],
    summary="Remove this agent's provider key",
    description=(
        "Forgets the stored credential. The agent falls back to whatever key the deployment is "
        "configured with for that provider — which may be none, in which case the agent stops "
        "answering until a key is set again.\n\n"
        "Removing a key does not create a version: credentials are not part of the configuration "
        "history, so this cannot be undone by rolling back. Deleting an agent with no key is a "
        "no-op rather than a 404."
    ),
    responses={
        200: {"description": "No key is stored on this agent any more."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def delete_agent_model_key(
    agent_id: AgentIdPath, service: AgentServiceDep
) -> ApiResponse[AgentResponse]:
    agent = await service.clear_model_api_key(agent_id)
    return ApiResponse.ok(_agent(agent), message="Provider key removed.")
