"""Attaching knowledge bases to agents (spec §5.2: knowledge bases are reusable).

The phase's bar is that one knowledge base can serve two agents. The rest of the file covers the
edges around that: attaching twice, detaching what was never attached, and deleting either end.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from tests.modules.knowledge_base.helpers import create_agent, create_kb, headers, upload_ok


async def attach(client: AsyncClient, auth: dict[str, str], kb_id: str, agent_id: str) -> int:
    response = await client.put(f"/knowledge-bases/{kb_id}/agents/{agent_id}", headers=auth)
    return response.status_code


async def test_one_knowledge_base_serves_two_agents(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    await upload_ok(client, auth, knowledge_base["id"], "prices.txt", b"Matt white is $45.")
    sales = await create_agent(client, auth, "Sales Assistant")
    support = await create_agent(client, auth, "Support Bot")

    assert await attach(client, auth, knowledge_base["id"], sales["id"]) == 200
    assert await attach(client, auth, knowledge_base["id"], support["id"]) == 200

    attached = (
        await client.get(f"/knowledge-bases/{knowledge_base['id']}/agents", headers=auth)
    ).json()["value"]["agentIds"]
    assert set(attached) == {sales["id"], support["id"]}

    for agent in (sales, support):
        listed = (await client.get(f"/knowledge-bases?agentId={agent['id']}", headers=auth)).json()
        assert [item["id"] for item in listed["value"]] == [knowledge_base["id"]]


async def test_one_agent_can_draw_on_several_knowledge_bases(client: AsyncClient) -> None:
    auth = await headers(client)
    policies = await create_kb(client, auth, name="Policies")
    prices = await create_kb(client, auth, name="Prices")
    agent = await create_agent(client, auth, "Sales Assistant")

    await attach(client, auth, policies["id"], agent["id"])
    await attach(client, auth, prices["id"], agent["id"])

    listed = (await client.get(f"/knowledge-bases?agentId={agent['id']}", headers=auth)).json()
    assert listed["meta"]["totalItems"] == 2


async def test_attaching_twice_changes_nothing(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    agent = await create_agent(client, auth, "Sales Assistant")

    assert await attach(client, auth, knowledge_base["id"], agent["id"]) == 200
    assert await attach(client, auth, knowledge_base["id"], agent["id"]) == 200

    body = (await client.get(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)).json()
    assert body["value"]["agentCount"] == 1


async def test_detaching_leaves_both_the_agent_and_the_knowledge_base(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    agent = await create_agent(client, auth, "Sales Assistant")
    await attach(client, auth, knowledge_base["id"], agent["id"])

    response = await client.delete(
        f"/knowledge-bases/{knowledge_base['id']}/agents/{agent['id']}", headers=auth
    )

    assert response.status_code == 200
    assert (await client.get(f"/agents/{agent['id']}", headers=auth)).status_code == 200
    body = (await client.get(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)).json()
    assert body["value"]["agentCount"] == 0


async def test_detaching_something_never_attached_is_reported_as_missing(
    client: AsyncClient,
) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    agent = await create_agent(client, auth, "Sales Assistant")

    response = await client.delete(
        f"/knowledge-bases/{knowledge_base['id']}/agents/{agent['id']}", headers=auth
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "KB_LINK_NOT_FOUND"


async def test_attaching_an_unknown_agent_is_reported_as_missing(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)

    response = await client.put(
        f"/knowledge-bases/{knowledge_base['id']}/agents/{uuid.uuid4()}", headers=auth
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_deleting_an_agent_leaves_the_knowledge_base_intact(client: AsyncClient) -> None:
    """The link goes; the knowledge it holds is reusable and stays."""
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    agent = await create_agent(client, auth, "Sales Assistant")
    await attach(client, auth, knowledge_base["id"], agent["id"])

    await client.delete(f"/agents/{agent['id']}", headers=auth)

    body = (await client.get(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)).json()
    assert body["value"]["agentCount"] == 0


async def test_deleting_a_knowledge_base_leaves_the_agent_intact(client: AsyncClient) -> None:
    auth = await headers(client)
    knowledge_base = await create_kb(client, auth)
    agent = await create_agent(client, auth, "Sales Assistant")
    await attach(client, auth, knowledge_base["id"], agent["id"])

    await client.delete(f"/knowledge-bases/{knowledge_base['id']}", headers=auth)

    assert (await client.get(f"/agents/{agent['id']}", headers=auth)).status_code == 200
    listed = (await client.get(f"/knowledge-bases?agentId={agent['id']}", headers=auth)).json()
    assert listed["meta"]["totalItems"] == 0
