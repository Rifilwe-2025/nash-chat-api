from __future__ import annotations

from httpx import AsyncClient


async def test_swagger_ui_is_served(client: AsyncClient) -> None:
    response = await client.get("/docs")

    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()


async def test_redoc_is_served(client: AsyncClient) -> None:
    assert (await client.get("/redoc")).status_code == 200


async def test_openapi_schema_carries_metadata(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()

    assert schema["info"]["title"]
    assert schema["info"]["version"]
    assert schema["info"]["description"]
    assert {tag["name"] for tag in schema["tags"]} == {"system"}


async def test_every_route_is_documented(client: AsyncClient) -> None:
    """Each endpoint needs a tag and a summary so the generated docs stay usable."""
    schema = (await client.get("/openapi.json")).json()

    undocumented = [
        f"{method.upper()} {path}"
        for path, operations in schema["paths"].items()
        for method, operation in operations.items()
        if not operation.get("tags") or not operation.get("summary")
    ]

    assert undocumented == []
