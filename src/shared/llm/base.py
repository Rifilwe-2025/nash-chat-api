"""The one internal interface every LLM provider implements (spec §5.3).

Nothing outside ``src/shared/llm`` should import a vendor SDK. Modules ask the registry for a
provider and talk to it through :class:`LLMProvider`, so switching an agent from Gemini to Claude is
a configuration change with no code change (spec §10).

Deliberately *not* a lowest-common-denominator wrapper: where providers genuinely differ (Claude
requires ``max_tokens``, current Claude models reject ``temperature``, Gemini puts the system prompt
in its own field), the adapter absorbs the difference rather than the caller.
"""

from __future__ import annotations

import base64
import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any


class Role(str, enum.Enum):
    """Who is speaking.

    ``TOOL`` is not a participant — it is the *result* of a call the assistant asked for, handed
    back so the model can answer with it. Every provider models this differently (Claude puts a
    ``tool_result`` block in a user turn, OpenAI has a ``tool`` role, Gemini a ``function_response``
    part), which is precisely why it is one role here and each adapter's problem there.
    """

    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AttachmentKind(str, enum.Enum):
    """Whether a file rides as an image part or a document part.

    The distinction is the providers': every one of them has a separate wire shape for pictures and
    for PDFs, and sending a PDF through the image block is a 400 everywhere.
    """

    IMAGE = "image"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """A file handed to the model to read natively (spec §5.2.3).

    v1 deliberately has no OCR or PDF-parsing library: PDFs and images are passed to the model,
    which reads them directly. Held as raw bytes here — each adapter encodes them the way its own
    API wants, so nothing outside ``src/shared/llm`` deals in base64.
    """

    data: bytes
    media_type: str
    kind: AttachmentKind = AttachmentKind.DOCUMENT
    filename: str | None = None

    @property
    def base64_data(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    @property
    def data_uri(self) -> str:
        return f"data:{self.media_type};base64,{self.base64_data}"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A tool the model may call, described so it can decide when to (spec §5.2.1).

    ``parameters`` is JSON Schema. Every provider accepts that shape for function declarations, so
    it is passed through rather than translated — converting it into each SDK's own schema type
    would lose fidelity for no gain.
    """

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """The model asking for a tool to be run.

    ``id`` is how the result is matched back to the request. Gemini has no call id of its own and
    its adapter substitutes the tool name, which is why nothing may assume ids are unique across a
    conversation — only within the turn they belong to.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One conversational turn. The system prompt is passed separately, not as a message.

    Three of the fields exist only for tool calling, and only one combination of them is meaningful
    at a time:

    * an **assistant** turn carries ``tool_calls`` when the model asked for a tool instead of (or as
      well as) answering. It must be replayed to the model, because a tool result with no record of
      the request it answers is rejected by every provider.
    * a **tool** turn carries ``tool_call_id`` and ``tool_name``, and its ``content`` is what the
      tool returned, already rendered as text.

    ``content`` is routinely empty on an assistant turn that only made a call — that is a normal
    response, not a failed one.
    """

    role: Role
    content: str
    attachments: Sequence[MediaAttachment] = ()
    tool_calls: Sequence[ToolCall] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Per-call accounting, used for cost tracking (spec §5.3, §5.8)."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """Everything a provider needs for one turn, in provider-neutral terms."""

    messages: Sequence[ChatMessage]
    model: str
    system: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    tools: Sequence[ToolDefinition] = ()
    stop_sequences: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """A normalised response. ``raw_finish_reason`` keeps the provider's own wording for logs."""

    content: str
    usage: TokenUsage
    model: str
    provider: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw_finish_reason: str | None = None


class LLMProvider(ABC):
    """Adapters implement this and nothing else is allowed to leak out of them."""

    name: str

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResult:
        """Run one turn and return the whole response."""

    @abstractmethod
    def stream(self, request: CompletionRequest) -> AsyncIterator[str]:
        """Yield text deltas as they arrive.

        Returns the iterator rather than being an async generator itself, so implementations can
        open their own streaming context managers.
        """

    async def aclose(self) -> None:
        """Release provider resources. Overridden where the SDK holds connections."""
        return None
