from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, File, Form, Path, Query, UploadFile

from src.modules.knowledge_base.domain.models import KbSource, KnowledgeBase, SourceType
from src.modules.knowledge_base.domain.services import KnowledgeBaseService
from src.modules.knowledge_base.internal import limits
from src.modules.knowledge_base.internal.retrieval import RetrievalResult, TierDecision
from src.modules.knowledge_base.presentation.dtos.knowledge_base import (
    AddManualSourceRequest,
    AddUrlSourceRequest,
    AttachedAgentsResponse,
    CitationResponse,
    CreateKnowledgeBaseRequest,
    KnowledgeBaseResponse,
    KnowledgeBaseSummaryResponse,
    PassageResponse,
    RetrievalExplainResponse,
    RetrievalRequest,
    SourceResponse,
    SourceSummaryResponse,
    StorageUsageResponse,
    UpdateKnowledgeBaseRequest,
)
from src.modules.tenants.presentation.dependencies import CurrentTenantDep
from src.shared.database.dependencies import SessionDep
from src.shared.database.pagination import PageParamsDep
from src.shared.responses import ApiResponse, PaginatedResponse, create_router

router = create_router(prefix="/knowledge-bases", tags=["knowledge-bases"])


def get_kb_service(session: SessionDep, tenant_id: CurrentTenantDep) -> KnowledgeBaseService:
    """The tenant comes from the token, so every query below is scoped before it is written."""
    return KnowledgeBaseService(session, tenant_id)


KbServiceDep = Annotated[KnowledgeBaseService, Depends(get_kb_service)]
KbIdPath = Annotated[uuid.UUID, Path(description="Identifier of the knowledge base.")]
SourceIdPath = Annotated[uuid.UUID, Path(description="Identifier of the source.")]
AgentIdPath = Annotated[uuid.UUID, Path(description="Identifier of the agent.")]

UNAUTHORIZED = {
    "description": "Access token is missing, invalid, or revoked (`UNAUTHORIZED`, `INVALID_TOKEN`)."
}
NOT_FOUND = {
    "description": (
        "No such knowledge base in your tenant (`KB_NOT_FOUND`). Another tenant's knowledge base "
        "is reported as missing rather than forbidden, so identifiers cannot be probed."
    )
}
SOURCE_NOT_FOUND = {
    "description": "No such knowledge base or source (`KB_NOT_FOUND`, `KB_SOURCE_NOT_FOUND`)."
}
INGESTION_REJECTED = {
    "description": (
        "The source was rejected before anything was stored: the file type is not supported "
        "(`KB_UNSUPPORTED_FILE_TYPE`), it is empty (`KB_SOURCE_EMPTY`), it exceeds the per-source "
        "limit (`KB_SOURCE_TOO_LARGE`), or your storage limit is reached "
        "(`KB_STORAGE_LIMIT_REACHED`)."
    )
}

EXTRACTION_NOTE = (
    "Extraction runs immediately, so the response already carries the outcome. **A document that "
    "cannot be read is not an error**: the source is stored with status `failed` and an "
    "`errorDetail` explaining why, so it can be seen and replaced. Only the limit and file-type "
    "checks above reject the request outright."
)


def _knowledge_base(
    knowledge_base: KnowledgeBase, source_count: int, agent_count: int
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        id=knowledge_base.id,
        tenant_id=knowledge_base.tenant_id,
        name=knowledge_base.name,
        description=knowledge_base.description,
        retrieval_tier=knowledge_base.retrieval_tier,
        source_count=source_count,
        agent_count=agent_count,
        created_at=knowledge_base.created_at,
        updated_at=knowledge_base.updated_at,
    )


def _summary(knowledge_base: KnowledgeBase) -> KnowledgeBaseSummaryResponse:
    return KnowledgeBaseSummaryResponse(
        id=knowledge_base.id,
        name=knowledge_base.name,
        description=knowledge_base.description,
        retrieval_tier=knowledge_base.retrieval_tier,
        updated_at=knowledge_base.updated_at,
    )


def _source_summary(source: KbSource) -> SourceSummaryResponse:
    return SourceSummaryResponse(
        id=source.id,
        kb_id=source.kb_id,
        name=source.name,
        type=source.type,
        status=source.status,
        byte_size=source.byte_size,
        error_detail=source.error_detail,
        last_synced_at=source.last_synced_at,
        source_updated_at=source.source_updated_at,
        created_at=source.created_at,
    )


def _source(source: KbSource) -> SourceResponse:
    return SourceResponse(
        **_source_summary(source).model_dump(),
        extracted_text=source.extracted_text,
        metadata=source.config_json,
    )


async def _detail(
    service: KnowledgeBaseService, knowledge_base: KnowledgeBase
) -> ApiResponse[KnowledgeBaseResponse]:
    sources, agents = await service.stats(knowledge_base.id)
    return ApiResponse.ok(_knowledge_base(knowledge_base, sources, agents))


# -- knowledge bases ------------------------------------------------------------


@router.post(
    "",
    response_model=ApiResponse[KnowledgeBaseResponse],
    status_code=201,
    summary="Create a knowledge base",
    description=(
        "Creates an empty knowledge base in your tenant. Knowledge bases are reusable — one can be "
        "attached to any number of agents — so create them around a body of knowledge rather than "
        "around a single agent."
    ),
    responses={
        201: {"description": "The knowledge base was created."},
        401: UNAUTHORIZED,
        409: {"description": "You already have one with that name (`KB_NAME_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def create_knowledge_base(
    payload: CreateKnowledgeBaseRequest, service: KbServiceDep
) -> ApiResponse[KnowledgeBaseResponse]:
    knowledge_base = await service.create(
        name=payload.name,
        description=payload.description,
        retrieval_tier=payload.retrieval_tier,
    )
    return ApiResponse.ok(
        _knowledge_base(knowledge_base, source_count=0, agent_count=0),
        message="Knowledge base created.",
    )


@router.get(
    "",
    response_model=PaginatedResponse[KnowledgeBaseSummaryResponse],
    summary="List knowledge bases",
    description=(
        "Lists the knowledge bases in your tenant, newest first. Pass `agentId` to list only the "
        "ones attached to that agent."
    ),
    responses={
        200: {"description": "A page of your knowledge bases."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
    },
)
async def list_knowledge_bases(
    service: KbServiceDep,
    page: PageParamsDep,
    agent_id: Annotated[
        uuid.UUID | None,
        Query(alias="agentId", description="Only knowledge bases attached to this agent."),
    ] = None,
) -> PaginatedResponse[KnowledgeBaseSummaryResponse]:
    result = await service.list_knowledge_bases(page, agent_id=agent_id)
    return PaginatedResponse.of(
        items=[_summary(knowledge_base) for knowledge_base in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/usage",
    response_model=ApiResponse[StorageUsageResponse],
    summary="Get knowledge base storage usage",
    description=(
        "How much of your ingestion allowance is in use, and the limits an upload is checked "
        "against. Sources that failed extraction store nothing and count as zero."
    ),
    responses={200: {"description": "Current usage and limits."}, 401: UNAUTHORIZED},
)
async def get_usage(service: KbServiceDep) -> ApiResponse[StorageUsageResponse]:
    return ApiResponse.ok(
        StorageUsageResponse(
            used_bytes=await service.storage_used(),
            limit_bytes=limits.max_tenant_bytes(),
            max_source_bytes=limits.max_source_bytes(),
        )
    )


EXPLAIN_DESCRIPTION = (
    "Runs the retrieval an agent would run for this query and shows the result **and the "
    "reasoning**: which tier ran, why, and the exact passages that would reach the prompt.\n\n"
    "Tier selection is automatic unless the knowledge base pins it. Everything that fits the "
    "injection budget is passed whole (`direct`); anything larger is searched with Postgres "
    "full-text search and only the matching passages are used (`keyword`). The budget depends on "
    "the target model, so pass `model` to see what a particular agent would get.\n\n"
    "`hasContext: false` is a real answer, not a failure — it means nothing relevant was found, "
    "and an agent seeing it uses its configured fallback response instead of guessing. "
    "`noContextReason` says which of the three ways that happened."
)


def _explain(decision: TierDecision, result: RetrievalResult) -> RetrievalExplainResponse:
    return RetrievalExplainResponse(
        tier=result.tier,
        tier_forced=decision.forced,
        tier_reason=decision.reason,
        considered_characters=result.considered_characters,
        budget_characters=result.budget_characters,
        has_context=result.has_context,
        no_context_reason=result.no_context_reason,
        passages=[
            PassageResponse(
                text=passage.text,
                citation=CitationResponse(
                    source_id=passage.citation.source_id,
                    kb_id=passage.citation.kb_id,
                    source_name=passage.citation.source_name,
                    source_type=SourceType(passage.citation.source_type),
                    url=passage.citation.url,
                ),
                score=passage.score,
            )
            for passage in result.passages
        ],
        retrieved_characters=result.characters,
    )


@router.post(
    "/retrieval/explain",
    response_model=ApiResponse[RetrievalExplainResponse],
    summary="Explain what an agent would retrieve",
    description=(
        "Retrieves across **every knowledge base attached to the agent**, which is what the agent "
        "itself will do at conversation time.\n\n" + EXPLAIN_DESCRIPTION
    ),
    responses={
        200: {"description": "The passages that would be injected, and why."},
        401: UNAUTHORIZED,
        404: {"description": "No such agent in your tenant (`AGENT_NOT_FOUND`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def explain_agent_retrieval(
    payload: RetrievalRequest,
    service: KbServiceDep,
    agent_id: Annotated[
        uuid.UUID, Query(alias="agentId", description="Agent whose knowledge bases to search.")
    ],
) -> ApiResponse[RetrievalExplainResponse]:
    decision, result = await service.explain_retrieval(
        payload.query, agent_id=agent_id, model=payload.model
    )
    return ApiResponse.ok(_explain(decision, result))


@router.post(
    "/{kb_id}/retrieval/explain",
    response_model=ApiResponse[RetrievalExplainResponse],
    summary="Explain what a knowledge base would retrieve",
    description=(
        "Retrieves against this knowledge base alone — useful for checking one body of knowledge "
        "before attaching it to anything.\n\n" + EXPLAIN_DESCRIPTION
    ),
    responses={
        200: {"description": "The passages that would be injected, and why."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def explain_kb_retrieval(
    kb_id: KbIdPath, payload: RetrievalRequest, service: KbServiceDep
) -> ApiResponse[RetrievalExplainResponse]:
    decision, result = await service.explain_retrieval(
        payload.query, kb_id=kb_id, model=payload.model
    )
    return ApiResponse.ok(_explain(decision, result))


@router.get(
    "/{kb_id}",
    response_model=ApiResponse[KnowledgeBaseResponse],
    summary="Get a knowledge base",
    description="Returns one knowledge base with its source and attached-agent counts.",
    responses={
        200: {"description": "The knowledge base."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def get_knowledge_base(
    kb_id: KbIdPath, service: KbServiceDep
) -> ApiResponse[KnowledgeBaseResponse]:
    return await _detail(service, await service.get(kb_id))


@router.patch(
    "/{kb_id}",
    response_model=ApiResponse[KnowledgeBaseResponse],
    summary="Update a knowledge base",
    description=(
        "Applies a partial update; omitted fields are left unchanged. Changing the retrieval tier "
        "does not re-extract anything — sources are stored as plain text either way."
    ),
    responses={
        200: {"description": "The updated knowledge base."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        409: {"description": "Another knowledge base already uses that name (`KB_NAME_TAKEN`)."},
        422: {"description": "The payload failed validation (`VALIDATION_ERROR`)."},
    },
)
async def update_knowledge_base(
    kb_id: KbIdPath, payload: UpdateKnowledgeBaseRequest, service: KbServiceDep
) -> ApiResponse[KnowledgeBaseResponse]:
    knowledge_base = await service.update(
        kb_id,
        {
            "name": payload.name,
            "description": payload.description,
            "retrieval_tier": payload.retrieval_tier,
        },
    )
    return await _detail(service, knowledge_base)


@router.delete(
    "/{kb_id}",
    response_model=ApiResponse[None],
    summary="Delete a knowledge base",
    description=(
        "Permanently removes the knowledge base, every source in it, and its attachments to "
        "agents. The agents themselves are untouched — they simply lose this knowledge."
    ),
    responses={
        200: {"description": "The knowledge base was deleted."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
    },
)
async def delete_knowledge_base(kb_id: KbIdPath, service: KbServiceDep) -> ApiResponse[None]:
    await service.delete(kb_id)
    return ApiResponse.ok(message="Knowledge base deleted.")


# -- sources --------------------------------------------------------------------


@router.post(
    "/{kb_id}/sources/file",
    response_model=ApiResponse[SourceResponse],
    status_code=201,
    summary="Upload a file as a source",
    description=(
        "Ingests an uploaded file. Supported: `.txt`, `.md`, `.docx`, `.csv`, `.tsv`, `.html`, "
        "`.pdf`, and images (`.png`, `.jpg`, `.webp`, `.gif`).\n\n"
        "Each format is read the way that suits it: Word documents keep their headings, CSV rows "
        "become natural-language sentences rather than raw rows, and web pages are stripped of "
        "navigation. **PDFs and images are read by the model directly** — there is no OCR step — "
        "so a scanned document works the same as a text one.\n\n" + EXTRACTION_NOTE
    ),
    responses={
        201: {"description": "The source was stored. Check `status` for the extraction outcome."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: INGESTION_REJECTED,
    },
)
async def upload_file_source(
    kb_id: KbIdPath,
    service: KbServiceDep,
    file: Annotated[UploadFile, File(description="The document to ingest.")],
    name: Annotated[
        str | None, Form(description="Label for the source. Defaults to the filename.")
    ] = None,
) -> ApiResponse[SourceResponse]:
    source = await service.add_file_source(
        kb_id,
        filename=file.filename or "upload",
        data=await file.read(),
        declared_media_type=file.content_type,
        name=name,
    )
    return ApiResponse.ok(_source(source), message="Source uploaded.")


@router.post(
    "/{kb_id}/sources/url",
    response_model=ApiResponse[SourceResponse],
    status_code=201,
    summary="Add a web page as a source",
    description=(
        "Fetches a public page and stores its readable text. Navigation, scripts, and footers are "
        "stripped; headings are kept.\n\n"
        "Only `http` and `https` are allowed, and URLs that resolve to private or internal "
        "addresses are refused at every redirect hop — the server would otherwise be usable to "
        "reach services inside its own network.\n\n" + EXTRACTION_NOTE
    ),
    responses={
        201: {"description": "The source was stored. Check `status` for the extraction outcome."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: INGESTION_REJECTED,
    },
)
async def add_url_source(
    kb_id: KbIdPath, payload: AddUrlSourceRequest, service: KbServiceDep
) -> ApiResponse[SourceResponse]:
    source = await service.add_url_source(kb_id, url=payload.url, name=payload.name)
    return ApiResponse.ok(_source(source), message="Source added.")


@router.post(
    "/{kb_id}/sources/manual",
    response_model=ApiResponse[SourceResponse],
    status_code=201,
    summary="Add a manual FAQ entry",
    description=(
        "Stores a question and answer typed directly into the builder. Nothing is extracted — the "
        "title becomes a heading above the body, so the entry reads the same way an uploaded "
        "document does."
    ),
    responses={
        201: {"description": "The entry was stored."},
        401: UNAUTHORIZED,
        404: NOT_FOUND,
        422: INGESTION_REJECTED,
    },
)
async def add_manual_source(
    kb_id: KbIdPath, payload: AddManualSourceRequest, service: KbServiceDep
) -> ApiResponse[SourceResponse]:
    source = await service.add_manual_source(kb_id, title=payload.title, body=payload.body)
    return ApiResponse.ok(_source(source), message="Source added.")


@router.get(
    "/{kb_id}/sources",
    response_model=PaginatedResponse[SourceSummaryResponse],
    summary="List sources in a knowledge base",
    description=(
        "Lists the sources in a knowledge base, newest first, with their extraction status. "
        "Extracted text is omitted here — fetch a single source to read it."
    ),
    responses={200: {"description": "A page of sources."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def list_sources(
    kb_id: KbIdPath, service: KbServiceDep, page: PageParamsDep
) -> PaginatedResponse[SourceSummaryResponse]:
    result = await service.list_sources(kb_id, page)
    return PaginatedResponse.of(
        items=[_source_summary(source) for source in result.items],
        page=result.page,
        page_size=result.page_size,
        total_items=result.total,
    )


@router.get(
    "/{kb_id}/sources/{source_id}",
    response_model=ApiResponse[SourceResponse],
    summary="Get a source and its extracted text",
    description=(
        "Returns one source in full, including `extractedText` — exactly the text retrieval will "
        "use, so it can be checked before an agent is published. A failed source carries "
        "`errorDetail` instead."
    ),
    responses={
        200: {"description": "The source."},
        401: UNAUTHORIZED,
        404: SOURCE_NOT_FOUND,
    },
)
async def get_source(
    kb_id: KbIdPath, source_id: SourceIdPath, service: KbServiceDep
) -> ApiResponse[SourceResponse]:
    return ApiResponse.ok(_source(await service.get_source(kb_id, source_id)))


@router.delete(
    "/{kb_id}/sources/{source_id}",
    response_model=ApiResponse[None],
    summary="Delete a source",
    description=(
        "Removes a source and its extracted text, freeing the storage it used. Every agent "
        "attached to the knowledge base loses that knowledge on its next turn."
    ),
    responses={
        200: {"description": "The source was deleted."},
        401: UNAUTHORIZED,
        404: SOURCE_NOT_FOUND,
    },
)
async def delete_source(
    kb_id: KbIdPath, source_id: SourceIdPath, service: KbServiceDep
) -> ApiResponse[None]:
    await service.delete_source(kb_id, source_id)
    return ApiResponse.ok(message="Source deleted.")


# -- agent attachment -----------------------------------------------------------


@router.get(
    "/{kb_id}/agents",
    response_model=ApiResponse[AttachedAgentsResponse],
    summary="List the agents using a knowledge base",
    description="Returns the agents this knowledge base is attached to, oldest attachment first.",
    responses={200: {"description": "The attached agents."}, 401: UNAUTHORIZED, 404: NOT_FOUND},
)
async def list_attached_agents(
    kb_id: KbIdPath, service: KbServiceDep
) -> ApiResponse[AttachedAgentsResponse]:
    return ApiResponse.ok(AttachedAgentsResponse(agent_ids=await service.attached_agent_ids(kb_id)))


@router.put(
    "/{kb_id}/agents/{agent_id}",
    response_model=ApiResponse[None],
    summary="Attach a knowledge base to an agent",
    description=(
        "Makes this knowledge base available to the agent. A knowledge base can serve any number "
        "of agents, and an agent can draw on several. Attaching one that is already attached "
        "succeeds and changes nothing."
    ),
    responses={
        200: {"description": "The knowledge base is attached."},
        401: UNAUTHORIZED,
        404: {
            "description": "No such knowledge base or agent (`KB_NOT_FOUND`, `AGENT_NOT_FOUND`)."
        },
    },
)
async def attach_agent(
    kb_id: KbIdPath, agent_id: AgentIdPath, service: KbServiceDep
) -> ApiResponse[None]:
    await service.attach(kb_id, agent_id)
    return ApiResponse.ok(message="Knowledge base attached.")


@router.delete(
    "/{kb_id}/agents/{agent_id}",
    response_model=ApiResponse[None],
    summary="Detach a knowledge base from an agent",
    description=(
        "Stops the agent drawing on this knowledge base. Neither the knowledge base nor the agent "
        "is deleted — only the link between them."
    ),
    responses={
        200: {"description": "The knowledge base is detached."},
        401: UNAUTHORIZED,
        404: {
            "description": (
                "No such knowledge base or agent, or they were never attached "
                "(`KB_NOT_FOUND`, `AGENT_NOT_FOUND`, `KB_LINK_NOT_FOUND`)."
            )
        },
    },
)
async def detach_agent(
    kb_id: KbIdPath, agent_id: AgentIdPath, service: KbServiceDep
) -> ApiResponse[None]:
    await service.detach(kb_id, agent_id)
    return ApiResponse.ok(message="Knowledge base detached.")
