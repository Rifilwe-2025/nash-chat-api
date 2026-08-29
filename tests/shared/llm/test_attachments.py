"""Attachment mapping across the three adapters.

Knowledge base ingestion reads PDFs and images by handing the file to the model (spec §5.2.3), and
every provider has its own wire shape for that. These tests pin **our** mapping — the part that can
actually be wrong — rather than re-testing the vendor SDKs.
"""

from __future__ import annotations

import base64

from src.shared.llm.base import (
    AttachmentKind,
    ChatMessage,
    CompletionRequest,
    MediaAttachment,
    Role,
)
from src.shared.llm.providers.anthropic_provider import AnthropicProvider
from src.shared.llm.providers.gemini_provider import GeminiProvider
from src.shared.llm.providers.openai_provider import OpenAIProvider
from tests.shared.llm.fakes import FakeAnthropicClient, FakeGeminiClient, FakeOpenAIClient

PDF = b"%PDF-1.7 fake"
PNG = b"\x89PNG fake"


def request_with(*attachments: MediaAttachment) -> CompletionRequest:
    return CompletionRequest(
        messages=[ChatMessage(role=Role.USER, content="Transcribe this.", attachments=attachments)],
        model="test-model",
    )


def pdf() -> MediaAttachment:
    return MediaAttachment(
        data=PDF, media_type="application/pdf", kind=AttachmentKind.DOCUMENT, filename="prices.pdf"
    )


def image() -> MediaAttachment:
    return MediaAttachment(data=PNG, media_type="image/png", kind=AttachmentKind.IMAGE)


# -- the shape of an attachment ----------------------------------------------------


def test_bytes_are_encoded_only_where_a_provider_needs_it() -> None:
    attachment = pdf()

    assert base64.b64decode(attachment.base64_data) == PDF
    assert attachment.data_uri.startswith("data:application/pdf;base64,")


# -- Anthropic -----------------------------------------------------------------------


async def test_anthropic_sends_a_pdf_as_a_base64_document_block() -> None:
    fake = FakeAnthropicClient()
    provider = AnthropicProvider(client=fake)

    await provider.complete(request_with(pdf()))

    blocks = fake.recorder.last["messages"][0]["content"]
    assert blocks[0]["type"] == "document"
    assert blocks[0]["source"] == {
        "type": "base64",
        "media_type": "application/pdf",
        "data": base64.b64encode(PDF).decode(),
    }
    assert blocks[1] == {"type": "text", "text": "Transcribe this."}


async def test_anthropic_sends_an_image_as_an_image_block() -> None:
    fake = FakeAnthropicClient()

    await AnthropicProvider(client=fake).complete(request_with(image()))

    assert fake.recorder.last["messages"][0]["content"][0]["type"] == "image"


async def test_anthropic_keeps_the_plain_string_shape_when_nothing_is_attached() -> None:
    """An ordinary turn must not become a block list — that is the common path."""
    fake = FakeAnthropicClient()

    await AnthropicProvider(client=fake).complete(
        CompletionRequest(messages=[ChatMessage(role=Role.USER, content="Hi")], model="m")
    )

    assert fake.recorder.last["messages"][0]["content"] == "Hi"


# -- OpenAI --------------------------------------------------------------------------


async def test_openai_sends_a_pdf_as_an_inline_file_part() -> None:
    fake = FakeOpenAIClient()

    await OpenAIProvider(client=fake).complete(request_with(pdf()))

    parts = fake.recorder.last["messages"][0]["content"]
    assert parts[0]["type"] == "file"
    assert parts[0]["file"]["filename"] == "prices.pdf"
    assert parts[0]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert parts[1] == {"type": "text", "text": "Transcribe this."}


async def test_openai_sends_an_image_as_a_data_uri_image_url() -> None:
    fake = FakeOpenAIClient()

    await OpenAIProvider(client=fake).complete(request_with(image()))

    part = fake.recorder.last["messages"][0]["content"][0]
    assert part["type"] == "image_url"
    assert part["image_url"]["url"] == f"data:image/png;base64,{base64.b64encode(PNG).decode()}"


async def test_openai_keeps_the_plain_string_shape_when_nothing_is_attached() -> None:
    fake = FakeOpenAIClient()

    await OpenAIProvider(client=fake).complete(
        CompletionRequest(messages=[ChatMessage(role=Role.USER, content="Hi")], model="m")
    )

    assert fake.recorder.last["messages"][-1]["content"] == "Hi"


# -- Gemini ---------------------------------------------------------------------------


async def test_gemini_sends_raw_bytes_so_the_sdk_encodes_them_once() -> None:
    """``Blob.data`` is bytes: handing the SDK base64 would double-encode the file."""
    fake = FakeGeminiClient()

    await GeminiProvider(client=fake).complete(request_with(pdf()))

    parts = fake.recorder.last["contents"][0]["parts"]
    assert parts[0] == {"inline_data": {"mime_type": "application/pdf", "data": PDF}}
    assert parts[1] == {"text": "Transcribe this."}


async def test_gemini_treats_images_and_documents_the_same_way() -> None:
    fake = FakeGeminiClient()

    await GeminiProvider(client=fake).complete(request_with(image()))

    assert fake.recorder.last["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/png"


async def test_gemini_sends_one_text_part_when_nothing_is_attached() -> None:
    fake = FakeGeminiClient()

    await GeminiProvider(client=fake).complete(
        CompletionRequest(messages=[ChatMessage(role=Role.USER, content="Hi")], model="m")
    )

    assert fake.recorder.last["contents"][0]["parts"] == [{"text": "Hi"}]


# -- several attachments ----------------------------------------------------------------


async def test_every_attachment_is_sent_and_the_instruction_comes_last() -> None:
    """Files first, then the ask about them — the order the model reads them in."""
    fake = FakeAnthropicClient()

    await AnthropicProvider(client=fake).complete(request_with(pdf(), image()))

    blocks = fake.recorder.last["messages"][0]["content"]
    assert [block["type"] for block in blocks] == ["document", "image", "text"]
