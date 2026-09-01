"""Agent business logic: configuration, lifecycle, version history, and provider credentials.

Every method takes the tenant id from the caller's token — the repository is constructed with it, so
no query in this module can reach another tenant's agents.

The provider key gets its own methods rather than riding in ``update``'s change dictionary. Writing
one is not a configuration edit: it must not bump the version, must not snapshot (the snapshot is
plaintext JSONB — see the model), and "leave it alone" and "clear it" have to be distinguishable,
which a dictionary that drops ``None`` cannot express.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.agents.domain.models import Agent, AgentStatus, AgentVersion, ModelProvider
from src.modules.agents.domain.repositories import AgentRepository, AgentVersionRepository
from src.modules.agents.internal.transitions import can_transition, publish_blockers
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException, ValidationException
from src.shared.llm.verification import KeyCheck, verify_key


class AgentService:
    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.agents = AgentRepository(session, tenant_id)
        self.versions = AgentVersionRepository(session)

    # -- reads ---------------------------------------------------------------

    async def get(self, agent_id: uuid.UUID) -> Agent:
        agent = await self.agents.get(agent_id)
        if agent is None:
            raise NotFoundException("Agent does not exist.", code="AGENT_NOT_FOUND")
        return agent

    async def list_agents(self, page: PageRequest) -> Page[Agent]:
        return await self.agents.list(page)

    # -- writes --------------------------------------------------------------

    async def create(
        self,
        name: str,
        persona: str = "",
        engagement_rules: dict[str, Any] | None = None,
        guardrails: dict[str, Any] | None = None,
        model_provider: ModelProvider | None = None,
        model_settings: dict[str, Any] | None = None,
        model_api_key: str | None = None,
    ) -> Agent:
        await self._require_unique_name(name)

        # No snapshot here: a version row records a configuration that has been *superseded*, and
        # v1 is still current. The first snapshot is written by the first edit.
        return await self.agents.add(
            Agent(
                name=name,
                persona=persona,
                engagement_rules=engagement_rules or {},
                guardrails=guardrails or {},
                model_provider=model_provider,
                model_config_json=model_settings or {},
                model_api_key=_clean_key(model_api_key),
                status=AgentStatus.DRAFT,
                version=1,
            )
        )

    async def update(
        self,
        agent_id: uuid.UUID,
        changes: dict[str, Any],
        note: str | None = None,
    ) -> Agent:
        """Apply a partial update, snapshotting the configuration that came before it."""
        agent = await self.get(agent_id)

        if "name" in changes and changes["name"] is not None:
            await self._require_unique_name(changes["name"], exclude_id=agent.id)

        applied = {key: value for key, value in changes.items() if value is not None}
        if not applied:
            return agent

        await self._snapshot(agent, note=note)
        applied["version"] = agent.version + 1
        return await self.agents.update(agent, **applied)

    async def delete(self, agent_id: uuid.UUID) -> None:
        await self.agents.delete(await self.get(agent_id))

    # -- lifecycle -----------------------------------------------------------

    async def publish(self, agent_id: uuid.UUID) -> Agent:
        agent = await self.get(agent_id)
        blockers = publish_blockers(agent)
        if blockers:
            raise ValidationException(
                "; ".join(blockers), code="AGENT_NOT_PUBLISHABLE", message="Agent is incomplete."
            )
        return await self._transition(agent, AgentStatus.PUBLISHED)

    async def pause(self, agent_id: uuid.UUID) -> Agent:
        return await self._transition(await self.get(agent_id), AgentStatus.PAUSED)

    async def unpublish(self, agent_id: uuid.UUID) -> Agent:
        return await self._transition(await self.get(agent_id), AgentStatus.DRAFT)

    # -- versions ------------------------------------------------------------

    async def list_versions(self, agent_id: uuid.UUID) -> list[AgentVersion]:
        await self.get(agent_id)  # scoped existence check before touching history
        return await self.versions.list_for_agent(agent_id)

    async def get_version(self, agent_id: uuid.UUID, version: int) -> AgentVersion:
        await self.get(agent_id)
        snapshot = await self.versions.get_version(agent_id, version)
        if snapshot is None:
            raise NotFoundException(
                f"Agent has no version {version}.", code="AGENT_VERSION_NOT_FOUND"
            )
        return snapshot

    async def rollback(self, agent_id: uuid.UUID, version: int, note: str | None = None) -> Agent:
        """Restore an earlier configuration as a *new* version — history is never rewritten."""
        agent = await self.get(agent_id)
        snapshot = await self.get_version(agent_id, version)

        await self._snapshot(agent, note=note or f"before rollback to v{version}")

        restored = snapshot.snapshot
        return await self.agents.update(
            agent,
            name=restored.get("name", agent.name),
            persona=restored.get("persona", ""),
            engagement_rules=restored.get("engagement_rules", {}),
            guardrails=restored.get("guardrails", {}),
            model_provider=(
                ModelProvider(restored["model_provider"])
                if restored.get("model_provider")
                else None
            ),
            model_config_json=restored.get("model_config_json", {}),
            version=agent.version + 1,
        )

    # -- provider credentials ------------------------------------------------

    async def set_model_api_key(self, agent_id: uuid.UUID, api_key: str) -> Agent:
        """Store the tenant's own key for this agent's provider.

        No snapshot and no version bump. A version exists so a configuration change can be undone,
        and there is nothing to undo here: the previous key is not recoverable from history by
        design, and a rotated credential is not a behaviour change anybody wants to roll back into.
        """
        cleaned = _clean_key(api_key)
        if cleaned is None:
            raise ValidationException(
                "An API key cannot be blank. Use DELETE to remove the one that is stored.",
                code="MODEL_API_KEY_EMPTY",
            )
        return await self.agents.update(await self.get(agent_id), model_api_key=cleaned)

    async def clear_model_api_key(self, agent_id: uuid.UUID) -> Agent:
        """Forget the stored key. The agent falls back to the deployment's key, if it has one."""
        return await self.agents.update(await self.get(agent_id), model_api_key=None)

    async def verify_model_key(
        self,
        agent_id: uuid.UUID,
        api_key: str | None = None,
        model: str | None = None,
    ) -> KeyCheck:
        """Ask the provider whether this agent's credential actually works.

        ``api_key`` and ``model`` override what is stored, which is what lets the builder test a key
        the user has typed but not yet saved — the alternative being to save an unverified
        credential in order to find out that it is wrong.

        Nothing is written. A check is a read of the outside world, and a passing check is not a
        reason to store the key that produced it: the caller decides that.
        """
        agent = await self.get(agent_id)
        if agent.model_provider is None:
            raise ValidationException(
                "This agent has no model provider selected.", code="AGENT_NOT_CONFIGURED"
            )

        chosen_model = (model or agent.model_config_json.get("model") or "").strip()
        if not chosen_model:
            raise ValidationException(
                "This agent has no model selected.", code="AGENT_NOT_CONFIGURED"
            )

        return await verify_key(
            agent.model_provider.value,
            chosen_model,
            _clean_key(api_key) or agent.model_api_key,
        )

    # -- internals -----------------------------------------------------------

    async def _transition(self, agent: Agent, target: AgentStatus) -> Agent:
        if agent.status is target:
            return agent
        if not can_transition(agent.status, target):
            raise ConflictException(
                f"Cannot move an agent from {agent.status.value} to {target.value}.",
                code="INVALID_STATUS_TRANSITION",
            )
        return await self.agents.update(agent, status=target)

    async def _require_unique_name(self, name: str, exclude_id: uuid.UUID | None = None) -> None:
        if await self.agents.name_taken(name, exclude_id=exclude_id):
            raise ConflictException(
                "An agent with that name already exists.", code="AGENT_NAME_TAKEN"
            )

    async def _snapshot(self, agent: Agent, note: str | None = None) -> AgentVersion:
        return await self.versions.add(
            AgentVersion(
                agent_id=agent.id,
                version=agent.version,
                note=note,
                snapshot={
                    "name": agent.name,
                    "persona": agent.persona,
                    "engagement_rules": agent.engagement_rules,
                    "guardrails": agent.guardrails,
                    "model_provider": agent.model_provider.value if agent.model_provider else None,
                    "model_config_json": agent.model_config_json,
                },
            )
        )


def _clean_key(api_key: str | None) -> str | None:
    """Trim, and treat an all-whitespace key as no key at all.

    Pasting a credential picks up a trailing newline more often than not, and every provider rejects
    the key with it attached — a failure that looks exactly like a wrong key to whoever pasted it.
    """
    if api_key is None:
        return None
    return api_key.strip() or None
