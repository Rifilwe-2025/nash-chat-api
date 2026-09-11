"""Argument types shared across tools.

Each carries its own description because the description is what a coding agent reads when deciding
what to pass: an identifier that says where to get it from is one the agent does not have to guess.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import Field

from src.shared.database.pagination import MAX_PAGE_SIZE

AgentId = Annotated[uuid.UUID, Field(description="Identifier of the agent, from list_agents.")]
KnowledgeBaseId = Annotated[
    uuid.UUID, Field(description="Identifier of the knowledge base, from list_knowledge_bases.")
]
SourceId = Annotated[
    uuid.UUID, Field(description="Identifier of the source, from list_knowledge_sources.")
]
ToolId = Annotated[
    uuid.UUID, Field(description="Identifier of the agent tool, from list_agent_tools.")
]
ConversationId = Annotated[
    uuid.UUID, Field(description="Identifier of the conversation, from list_conversations.")
]
PageNumber = Annotated[int, Field(ge=1, description="1-indexed page number.")]
PageSize = Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE, description="Rows per page.")]
