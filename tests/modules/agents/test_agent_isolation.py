"""Tenant isolation on every agent route (spec §5.7).

A foreign agent must be reported as **missing**, not forbidden: a 403 would confirm the identifier
exists and turn the endpoint into an oracle for probing other tenants' data.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.modules.agents.test_agent_crud import PUBLISHABLE, create, headers


async def test_agents_from_another_tenant_are_invisible_in_the_list(
    client: AsyncClient,
) -> None:
    first = await headers(client)
    second = await headers(client)
    await create(client, first, name="Tenant A Agent")
    mine = await create(client, second, name="Tenant B Agent")

    body = (await client.get("/agents", headers=second)).json()

    assert body["meta"]["totalItems"] == 1
    assert [agent["id"] for agent in body["value"]] == [mine["id"]]


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [
        ("get", "", None),
        ("patch", "", {"persona": "hijacked"}),
        ("delete", "", None),
        ("post", "/publish", None),
        ("post", "/pause", None),
        ("post", "/unpublish", None),
        ("get", "/versions", None),
        ("get", "/versions/1", None),
        ("post", "/versions/1/rollback", {}),
    ],
)
async def test_every_route_hides_another_tenants_agent(
    client: AsyncClient, method: str, suffix: str, payload: dict[str, object] | None
) -> None:
    first = await headers(client)
    second = await headers(client)
    victim = await create(client, first, **PUBLISHABLE)

    url = f"/agents/{victim['id']}{suffix}"
    kwargs: dict[str, object] = {"headers": second}
    if payload is not None:
        kwargs["json"] = payload
    response = await getattr(client, method)(url, **kwargs)

    assert response.status_code == 404
    assert response.json()["error"]["code"] in {"AGENT_NOT_FOUND", "AGENT_VERSION_NOT_FOUND"}


async def test_a_failed_cross_tenant_write_leaves_the_agent_untouched(
    client: AsyncClient,
) -> None:
    first = await headers(client)
    second = await headers(client)
    victim = await create(client, first, persona="Untouched")

    await client.patch(f"/agents/{victim['id']}", json={"persona": "hijacked"}, headers=second)

    after = (await client.get(f"/agents/{victim['id']}", headers=first)).json()["value"]
    assert after["persona"] == "Untouched"
    assert after["version"] == 1


async def test_agent_routes_require_authentication(client: AsyncClient) -> None:
    unknown = uuid.uuid4()

    assert (await client.get("/agents")).status_code == 401
    assert (await client.post("/agents", json={"name": "x"})).status_code == 401
    assert (await client.get(f"/agents/{unknown}")).status_code == 401
