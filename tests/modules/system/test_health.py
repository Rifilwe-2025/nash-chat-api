from __future__ import annotations

from httpx import AsyncClient


async def test_health_returns_envelope(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["value"]["status"] == "ok"
    assert body["value"]["version"]


async def test_health_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "test-request-id"})

    assert response.headers["x-request-id"] == "test-request-id"


async def test_health_assigns_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.headers.get("x-request-id")


async def test_health_omits_null_envelope_fields(client: AsyncClient) -> None:
    body = (await client.get("/health")).json()

    assert "error" not in body
    assert "message" not in body
