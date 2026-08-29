"""The tenant-facing tools API, and the boundary around it (spec §5.2.1, §5.7).

Everything here goes through the real HTTP surface with a real access token. What it checks is the
half of the phase a tenant actually touches: defining a tool, being stopped from defining a broken
one, seeing what the model would be shown before going live, and never getting their own credential
back out of the API.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from httpx import AsyncClient

from tests.modules.auth.test_auth_flow import auth_header, signup

TOOL: dict[str, Any] = {
    "name": "check_order_status",
    "description": "Look up the status and delivery date of a customer's order by order number.",
    "endpointUrl": "https://api.example.com/orders/{orderId}",
    "httpMethod": "get",
    "authType": "bearer",
    "authConfig": {"value": "sk_live_never_returned"},
    "requestSchema": {
        "type": "object",
        "properties": {"orderId": {"type": "string"}},
        "required": ["orderId"],
    },
    "responseMapping": {"root": "data", "fields": {"status": "Status"}},
}


@pytest.fixture(autouse=True)
def reachable(config_override: Callable[..., None]) -> None:
    """``api.example.com`` does not resolve; the address guard has its own tests."""
    config_override(TOOLS_ALLOW_PRIVATE_URLS="true")


async def owner(client: AsyncClient) -> dict[str, str]:
    return auth_header((await signup(client))["tokens"])


async def make_agent(client: AsyncClient, auth: dict[str, str]) -> dict[str, Any]:
    created = await client.post(
        "/agents",
        json={"name": f"Agent {uuid.uuid4().hex[:6]}", "persona": "Helpful."},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    agent: dict[str, Any] = created.json()["value"]
    return agent


async def add_tool(
    client: AsyncClient, auth: dict[str, str], agent_id: str, **overrides: Any
) -> Any:
    return await client.post(f"/agents/{agent_id}/tools", json={**TOOL, **overrides}, headers=auth)


# -- defining a tool --------------------------------------------------------------------


async def test_a_tool_can_be_defined_and_read_back(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)

    created = await add_tool(client, auth, agent["id"])

    assert created.status_code == 201, created.text
    tool = created.json()["value"]
    assert tool["name"] == "check_order_status"
    assert tool["status"] == "enabled"
    assert tool["hasCredential"] is True


async def test_the_credential_is_never_returned(client: AsyncClient) -> None:
    """It goes in and stays in — the whole reason the platform holds it (§5.2.1)."""
    auth = await owner(client)
    agent = await make_agent(client, auth)
    created = await add_tool(client, auth, agent["id"])
    tool_id = created.json()["value"]["id"]

    read = await client.get(f"/tools/{tool_id}", headers=auth)
    listed = await client.get(f"/agents/{agent['id']}/tools", headers=auth)

    assert "sk_live_never_returned" not in created.text
    assert "sk_live_never_returned" not in read.text
    assert "sk_live_never_returned" not in listed.text
    assert read.json()["value"]["hasCredential"] is True


async def test_the_first_tool_seeds_the_allowlist_with_its_own_host(
    client: AsyncClient,
) -> None:
    """A tenant who just defined one endpoint plainly means to allow it."""
    auth = await owner(client)
    agent = await make_agent(client, auth)
    await add_tool(client, auth, agent["id"])

    policy = await client.get(f"/agents/{agent['id']}/tools/policy", headers=auth)

    assert policy.json()["value"]["allowedHosts"] == ["api.example.com"]


async def test_a_description_too_short_to_be_prompt_text_is_refused(
    client: AsyncClient,
) -> None:
    """The description is what the model reads to choose the tool. "gets orders" is not enough."""
    auth = await owner(client)
    agent = await make_agent(client, auth)

    refused = await add_tool(client, auth, agent["id"], description="orders")

    assert refused.status_code == 422


async def test_an_endpoint_placeholder_must_be_declared_in_the_schema(
    client: AsyncClient,
) -> None:
    """The model can only fill in a blank it has been told about."""
    auth = await owner(client)
    agent = await make_agent(client, auth)

    refused = await add_tool(
        client,
        auth,
        agent["id"],
        endpointUrl="https://api.example.com/orders/{orderId}/{customerId}",
    )

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "TOOL_PLACEHOLDER_UNDECLARED"


@pytest.mark.parametrize(
    "endpoint",
    ["ftp://api.example.com/orders", "not-a-url", "file:///etc/passwd"],
)
async def test_an_endpoint_that_is_not_http_is_refused(client: AsyncClient, endpoint: str) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)

    refused = await add_tool(client, auth, agent["id"], endpointUrl=endpoint)

    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "TOOL_ENDPOINT_INVALID"


@pytest.mark.parametrize("name", ["9lives", "has spaces", "has.dots", ""])
async def test_a_name_no_provider_would_accept_is_refused(client: AsyncClient, name: str) -> None:
    """A name the provider rejects fails the whole turn, not just the tool."""
    auth = await owner(client)
    agent = await make_agent(client, auth)

    refused = await add_tool(client, auth, agent["id"], name=name)

    assert refused.status_code == 422


async def test_two_tools_on_one_agent_cannot_share_a_name(client: AsyncClient) -> None:
    """The model addresses a tool by name, so a duplicate is ambiguous where it costs most."""
    auth = await owner(client)
    agent = await make_agent(client, auth)
    await add_tool(client, auth, agent["id"])

    duplicate = await add_tool(client, auth, agent["id"])

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "TOOL_NAME_TAKEN"


# -- changing one -----------------------------------------------------------------------


async def test_a_tool_can_be_disabled_without_losing_its_configuration(
    client: AsyncClient,
) -> None:
    """What a tenant needs when an integration starts misbehaving mid-afternoon."""
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    disabled = await client.patch(f"/tools/{tool_id}", json={"status": "disabled"}, headers=auth)

    assert disabled.json()["value"]["status"] == "disabled"
    assert disabled.json()["value"]["hasCredential"] is True
    assert disabled.json()["value"]["requestSchema"]["required"] == ["orderId"]


async def test_an_omitted_field_is_left_alone_on_update(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    updated = await client.patch(f"/tools/{tool_id}", json={"cacheTtlSeconds": 30}, headers=auth)

    assert updated.json()["value"]["cacheTtlSeconds"] == 30
    assert updated.json()["value"]["name"] == "check_order_status"
    assert updated.json()["value"]["hasCredential"] is True


async def test_a_deleted_tool_is_gone(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    await client.delete(f"/tools/{tool_id}", headers=auth)
    gone = await client.get(f"/tools/{tool_id}", headers=auth)

    assert gone.status_code == 404


# -- the allowlist ----------------------------------------------------------------------


async def test_the_allowlist_can_be_set_and_normalises_what_is_pasted(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    await add_tool(client, auth, agent["id"])

    updated = await client.put(
        f"/agents/{agent['id']}/tools/policy",
        json={"allowedHosts": ["https://API.Example.com/orders", ".partner.example"]},
        headers=auth,
    )

    assert updated.json()["value"]["allowedHosts"] == [".partner.example", "api.example.com"]


async def test_a_zero_call_budget_is_refused(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    await add_tool(client, auth, agent["id"])

    refused = await client.put(
        f"/agents/{agent['id']}/tools/policy", json={"maxCallsPerTurn": 0}, headers=auth
    )

    assert refused.status_code == 422


# -- the test run -----------------------------------------------------------------------


async def test_a_failing_test_run_returns_200_with_the_note_the_model_would_see(
    client: AsyncClient,
) -> None:
    """The point of the endpoint is to show what the model gets, so a failure must not be an error.

    `api.example.com` has no server behind it in a test, so this exercises the real failure path —
    and what comes back is the sentence the agent would compose an apology from.
    """
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    ran = await client.post(
        f"/tools/{tool_id}/try", json={"arguments": {"orderId": "A-1"}}, headers=auth
    )

    assert ran.status_code == 200, ran.text
    assert ran.json()["value"]["outcome"] in ("failed", "timedOut", "refused")
    assert ran.json()["value"]["resultText"]


async def test_a_test_run_with_bad_arguments_is_refused_before_any_request(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    ran = await client.post(f"/tools/{tool_id}/try", json={"arguments": {}}, headers=auth)

    assert ran.json()["value"]["outcome"] == "refused"
    assert "orderId" in ran.json()["value"]["resultText"]


async def test_a_test_run_appears_in_the_call_log_with_no_conversation(
    client: AsyncClient,
) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)
    tool_id = (await add_tool(client, auth, agent["id"])).json()["value"]["id"]

    await client.post(f"/tools/{tool_id}/try", json={"arguments": {"orderId": "A-1"}}, headers=auth)
    log = await client.get(f"/tools/{tool_id}/calls", headers=auth)

    assert log.json()["value"][0]["arguments"] == {"orderId": "A-1"}
    assert log.json()["value"][0].get("conversationId") is None


# -- tenant isolation -------------------------------------------------------------------


async def test_one_tenant_cannot_read_another_tenants_tool(client: AsyncClient) -> None:
    auth_a = await owner(client)
    agent_a = await make_agent(client, auth_a)
    tool_id = (await add_tool(client, auth_a, agent_a["id"])).json()["value"]["id"]

    auth_b = await owner(client)
    stolen = await client.get(f"/tools/{tool_id}", headers=auth_b)

    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "TOOL_NOT_FOUND"


async def test_one_tenant_cannot_run_another_tenants_tool(client: AsyncClient) -> None:
    """A test run executes a call with someone else's credential. It must not be reachable."""
    auth_a = await owner(client)
    agent_a = await make_agent(client, auth_a)
    tool_id = (await add_tool(client, auth_a, agent_a["id"])).json()["value"]["id"]

    auth_b = await owner(client)
    stolen = await client.post(
        f"/tools/{tool_id}/try", json={"arguments": {"orderId": "A-1"}}, headers=auth_b
    )

    assert stolen.status_code == 404


async def test_one_tenant_cannot_widen_another_tenants_allowlist(
    client: AsyncClient,
) -> None:
    auth_a = await owner(client)
    agent_a = await make_agent(client, auth_a)
    await add_tool(client, auth_a, agent_a["id"])

    auth_b = await owner(client)
    stolen = await client.put(
        f"/agents/{agent_a['id']}/tools/policy",
        json={"allowedHosts": ["attacker.test"]},
        headers=auth_b,
    )

    assert stolen.status_code == 404
    still = await client.get(f"/agents/{agent_a['id']}/tools/policy", headers=auth_a)
    assert still.json()["value"]["allowedHosts"] == ["api.example.com"]


async def test_one_tenant_cannot_add_a_tool_to_another_tenants_agent(
    client: AsyncClient,
) -> None:
    auth_a = await owner(client)
    agent_a = await make_agent(client, auth_a)

    auth_b = await owner(client)
    stolen = await add_tool(client, auth_b, agent_a["id"])

    assert stolen.status_code == 404
    assert stolen.json()["error"]["code"] == "AGENT_NOT_FOUND"


async def test_one_tenant_cannot_read_another_tenants_call_log(client: AsyncClient) -> None:
    """The log holds arguments and results — someone else's customer data."""
    auth_a = await owner(client)
    agent_a = await make_agent(client, auth_a)
    tool_id = (await add_tool(client, auth_a, agent_a["id"])).json()["value"]["id"]
    await client.post(
        f"/tools/{tool_id}/try", json={"arguments": {"orderId": "A-1"}}, headers=auth_a
    )

    auth_b = await owner(client)
    stolen = await client.get(f"/tools/{tool_id}/calls", headers=auth_b)

    assert stolen.status_code == 404


async def test_the_tools_routes_need_a_token(client: AsyncClient) -> None:
    auth = await owner(client)
    agent = await make_agent(client, auth)

    anonymous = await client.get(f"/agents/{agent['id']}/tools")

    assert anonymous.status_code == 401
