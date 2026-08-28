"""Shared builders for the knowledge base suite."""

from __future__ import annotations

import io
import zipfile
from typing import Any

from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import auth_header, signup

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def headers(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def create_kb(client: AsyncClient, auth: dict[str, str], **overrides: Any) -> dict[str, Any]:
    payload = {"name": "Product catalogue", **overrides}
    response = await client.post("/knowledge-bases", json=payload, headers=auth)
    assert response.status_code == 201, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


async def create_agent(client: AsyncClient, auth: dict[str, str], name: str) -> dict[str, Any]:
    response = await client.post("/agents", json={"name": name}, headers=auth)
    assert response.status_code == 201, response.text
    value: dict[str, Any] = response.json()["value"]
    return value


async def upload(
    client: AsyncClient,
    auth: dict[str, str],
    kb_id: str,
    filename: str,
    data: bytes,
    media_type: str = "text/plain",
) -> tuple[int, dict[str, Any]]:
    response = await client.post(
        f"/knowledge-bases/{kb_id}/sources/file",
        files={"file": (filename, data, media_type)},
        headers=auth,
    )
    return response.status_code, response.json()


async def upload_ok(
    client: AsyncClient,
    auth: dict[str, str],
    kb_id: str,
    filename: str,
    data: bytes,
    media_type: str = "text/plain",
) -> dict[str, Any]:
    status, body = await upload(client, auth, kb_id, filename, data, media_type)
    assert status == 201, body
    value: dict[str, Any] = body["value"]
    return value


def build_docx(paragraphs: list[tuple[str, str]]) -> bytes:
    """A minimal .docx, built by hand as the zip that it is.

    ``python-docx`` could build one, but then the fixture and the extractor would share a library:
    a change in how python-docx names styles would move both at once and the test would keep
    passing. Writing the WordprocessingML directly keeps the input independent of the code reading
    it. Each entry is ``(style, text)`` — e.g. ``("Heading 1", "Returns")``.
    """
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{style.replace(" ", "")}"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
        for style, text in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )

    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + "".join(
            f'<w:style w:type="paragraph" w:styleId="{style.replace(" ", "")}">'
            f'<w:name w:val="{style}"/></w:style>'
            for style in {style for style, _ in paragraphs}
        )
        + "</w:styles>"
    )

    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
        'relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )

    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )

    document_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/styles" Target="styles.xml"/>'
        "</Relationships>"
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", document_rels)
        archive.writestr("word/styles.xml", styles)
    return buffer.getvalue()
