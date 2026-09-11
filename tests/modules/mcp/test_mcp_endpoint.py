"""The /mcp endpoint, driven the way a coding agent drives it.

Grouped by the guarantee under test: who gets in, what a token's scope allows, that a tool call sees
only its own tenant, that secrets never cross, and that the tools do what the console does.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.tenants.domain.models import TenantStatus
from src.modules.tenants.domain.services import TenantService
from src.shared.database.pagination import MAX_PAGE_SIZE
from tests.modules.auth.test_auth_flow import PASSWORD
from tests.modules.mcp.helpers import (
    PUBLISHABLE,
    account,
    call_tool,
    error_code,
    issue_token,
    rpc,
    serving,
    value_of,
)

WRITE = ("mcp:read", "mcp:write")


# -- getting in --------------------------------------------------------------------------


async def test_a_request_without_a_token_is_refused(client: AsyncClient) -> None:
    response = await rpc(client, None, "tools/list")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_a_bad_token_is_refused_with_one_code(client: AsyncClient) -> None:
    """Unknown, malformed and wrong-scheme all read alike from outside."""
    for header in ("Bearer nsp_local_not-a-real-token", "Bearer ", "Basic dXNlcjpwYXNz"):
        response = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": header, "Accept": "application/json"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] in {"INVALID_PERSONAL_TOKEN", "UNAUTHORIZED"}


async def test_a_revoked_token_stops_on_its_next_request(app: FastAPI, client: AsyncClient) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)
    token_id = (await client.get("/mcp-tokens", headers=auth)).json()["value"][0]["id"]

    async with serving(app):
        assert (await rpc(client, token, "tools/list")).status_code == 200
        await client.post(f"/mcp-tokens/{token_id}/revoke", headers=auth)
        refused = await rpc(client, token, "tools/list")

    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "INVALID_PERSONAL_TOKEN"


async def test_signing_in_again_does_not_revoke_a_personal_token(
    app: FastAPI, client: AsyncClient
) -> None:
    """The reason these tokens have a table of their own: a sign-in revokes every session token."""
    auth, value = await account(client)
    token = await issue_token(client, auth)

    login = await client.post(
        "/auth/login", json={"email": value["user"]["email"], "password": PASSWORD}
    )
    assert login.status_code == 200

    async with serving(app):
        assert (await rpc(client, token, "tools/list")).status_code == 200


async def test_a_disabled_account_is_refused(
    app: FastAPI, client: AsyncClient, session: AsyncSession
) -> None:
    auth, value = await account(client)
    token = await issue_token(client, auth)

    await TenantService(session).set_status(
        uuid.UUID(value["user"]["tenantId"]), TenantStatus.DISABLED
    )
    response = await rpc(client, token, "tools/list")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


async def test_the_rate_limit_is_counted_per_token(
    app: FastAPI, client: AsyncClient, config_override: Callable[..., None]
) -> None:
    config_override(MCP_RATE_LIMIT_PER_MINUTE=2)
    auth, _ = await account(client)
    token = await issue_token(client, auth)
    other = await issue_token(client, auth)

    async with serving(app):
        allowed = [await rpc(client, token, "tools/list") for _ in range(2)]
        refused = await rpc(client, token, "tools/list")
        unaffected = await rpc(client, other, "tools/list")

    assert [response.status_code for response in allowed] == [200, 200]
    assert allowed[0].headers["x-ratelimit-limit"] == "2"
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert "retry-after" in refused.headers
    assert unaffected.status_code == 200


async def test_the_endpoint_is_not_part_of_the_rest_schema(client: AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]

    assert "/mcp" not in paths
    assert "/mcp-tokens" in paths


# -- what a token may do -------------------------------------------------------------------


async def test_the_tools_are_listed_with_their_annotations(
    app: FastAPI, client: AsyncClient
) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        response = await rpc(client, token, "tools/list")

    tools = {tool["name"]: tool for tool in response.json()["result"]["tools"]}
    assert {"whoami", "create_agent", "get_integration_guide", "send_preview_message"} <= set(tools)
    assert tools["list_agents"]["annotations"]["readOnlyHint"] is True
    assert tools["create_agent"]["annotations"]["readOnlyHint"] is False
    assert tools["try_agent_tool"]["annotations"]["openWorldHint"] is True


async def test_whoami_reports_the_owner_and_scopes(app: FastAPI, client: AsyncClient) -> None:
    auth, value = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        me = await value_of(client, token, "whoami")

    assert me["user"]["email"] == value["user"]["email"]
    assert me["organisation"]["id"] == value["user"]["tenantId"]
    assert me["scopes"] == ["mcp:read"]
    assert me["canWrite"] is False


async def test_a_read_only_token_cannot_change_anything(app: FastAPI, client: AsyncClient) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        result = await call_tool(client, token, "create_agent", {"name": "Not allowed"})

    assert error_code(result) == "INSUFFICIENT_SCOPE"
    assert (await client.get("/agents", headers=auth)).json()["meta"]["totalItems"] == 0


async def test_errors_lead_with_the_platforms_error_code(app: FastAPI, client: AsyncClient) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        result = await call_tool(client, token, "get_agent", {"agent_id": str(uuid.uuid4())})

    assert error_code(result) == "AGENT_NOT_FOUND"


async def test_arguments_are_validated_before_anything_runs(
    app: FastAPI, client: AsyncClient
) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        result = await call_tool(client, token, "list_agents", {"page_size": MAX_PAGE_SIZE + 1})

    assert result.get("isError") is True


# -- isolation and secrets -----------------------------------------------------------------


async def test_another_tenants_agent_is_not_found(app: FastAPI, client: AsyncClient) -> None:
    owner, _ = await account(client)
    created = await client.post("/agents", json={"name": "Private agent"}, headers=owner)
    agent_id = created.json()["value"]["id"]

    stranger, _ = await account(client)
    token = await issue_token(client, stranger, WRITE)

    async with serving(app):
        read = await call_tool(client, token, "get_agent", {"agent_id": agent_id})
        write = await call_tool(
            client, token, "update_agent", {"agent_id": agent_id, "name": "Hijacked"}
        )
        listed = await value_of(client, token, "list_agents")

    assert error_code(read) == "AGENT_NOT_FOUND"
    assert error_code(write) == "AGENT_NOT_FOUND"
    assert listed["totalItems"] == 0
    owners_view = await client.get(f"/agents/{agent_id}", headers=owner)
    assert owners_view.json()["value"]["name"] == "Private agent"


async def test_an_admin_acting_as_a_tenant_still_gets_a_token_for_their_own(
    app: FastAPI, client: AsyncClient, session: AsyncSession
) -> None:
    """A standing credential is never issued inside somebody else's organisation."""
    admin_auth, admin = await account(client)
    user = await TenantService(session).find_by_email(admin["user"]["email"])
    assert user is not None
    user.is_platform_admin = True
    await session.flush()

    _, other = await account(client)
    token = await issue_token(client, {**admin_auth, "X-Tenant-Id": other["user"]["tenantId"]})

    async with serving(app):
        me = await value_of(client, token, "whoami")

    assert me["organisation"]["id"] == admin["user"]["tenantId"]


async def test_secrets_never_reach_a_coding_agent(app: FastAPI, client: AsyncClient) -> None:
    auth, _ = await account(client)
    provider_key = "sk-provider-secret-value-0001"
    tool_key = "tool-credential-secret-0002"

    created = await client.post(
        "/agents",
        json={"name": "Keyed agent", **PUBLISHABLE, "modelApiKey": provider_key},
        headers=auth,
    )
    agent_id = created.json()["value"]["id"]
    tool = await client.post(
        f"/agents/{agent_id}/tools",
        json={
            "name": "check_order",
            "description": "Look up an order's delivery status by its order number.",
            "endpointUrl": "https://api.example.com/orders/{orderId}",
            "authType": "api_key_header",
            "authConfig": {"headerName": "X-API-Key", "value": tool_key},
            "requestSchema": {
                "type": "object",
                "properties": {"orderId": {"type": "string"}},
                "required": ["orderId"],
            },
        },
        headers=auth,
    )
    assert tool.status_code == 201, tool.text
    token = await issue_token(client, auth)

    async with serving(app):
        agent = await call_tool(client, token, "get_agent", {"agent_id": agent_id})
        tools = await call_tool(client, token, "list_agent_tools", {"agent_id": agent_id})

    seen = json.dumps([agent, tools])
    assert provider_key not in seen
    assert tool_key not in seen
    assert agent["structuredContent"]["hasModelApiKey"] is True
    assert tools["structuredContent"]["items"][0]["hasCredential"] is True


# -- doing the work ------------------------------------------------------------------------


async def test_a_coding_agent_can_build_an_agent_end_to_end(
    app: FastAPI, client: AsyncClient
) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth, WRITE)

    async with serving(app):
        agent = await value_of(
            client,
            token,
            "create_agent",
            {
                "name": "Sales assistant",
                "persona": PUBLISHABLE["persona"],
                "model_provider": "gemini",
                "model_settings": {"model": "gemini-2.0-flash"},
            },
        )
        kb = await value_of(client, token, "create_knowledge_base", {"name": "Delivery"})
        source = await value_of(
            client,
            token,
            "add_text_source",
            {
                "kb_id": kb["id"],
                "title": "Delivery times",
                "body": "Orders placed before 2pm are delivered in Harare the next working day.",
            },
        )
        await value_of(
            client, token, "attach_knowledge_base", {"kb_id": kb["id"], "agent_id": agent["id"]}
        )
        retrieval = await value_of(
            client,
            token,
            "explain_retrieval",
            {"query": "How long does delivery to Harare take?", "agent_id": agent["id"]},
        )
        text = await value_of(
            client, token, "read_knowledge_source", {"kb_id": kb["id"], "source_id": source["id"]}
        )
        updated = await value_of(
            client,
            token,
            "update_agent",
            {"agent_id": agent["id"], "persona": "You answer delivery questions.", "note": "focus"},
        )
        versions = await value_of(client, token, "list_agent_versions", {"agent_id": agent["id"]})
        published = await value_of(
            client, token, "set_agent_status", {"agent_id": agent["id"], "action": "publish"}
        )
        guide = await value_of(client, token, "get_integration_guide", {"agent_id": agent["id"]})

    assert agent["status"] == "draft"
    assert source["status"] == "ready"
    assert retrieval["hasContext"] is True
    assert "Harare" in text["text"] and text["nextOffset"] is None
    assert updated["version"] == 2
    assert versions["items"][0]["note"] == "focus"
    assert published["status"] == "published"
    assert "Sales assistant" in guide["markdown"]
    assert guide["baseUrl"] == "http://test"

    # The same rows the console reads — MCP is another door, not another store.
    console = (await client.get(f"/agents/{agent['id']}", headers=auth)).json()["value"]
    assert console["status"] == "published"
    assert console["persona"] == "You answer delivery questions."


async def test_a_preview_message_runs_a_turn_in_its_own_thread(
    app: FastAPI, client: AsyncClient
) -> None:
    """A restricted topic is declined before any provider is called, so no model is needed."""
    auth, value = await account(client)
    created = await client.post(
        "/agents",
        json={
            "name": "Guarded agent",
            **PUBLISHABLE,
            "guardrails": {"restrictedTopics": ["refunds"]},
        },
        headers=auth,
    )
    agent_id = created.json()["value"]["id"]
    token = await issue_token(client, auth, WRITE)

    async with serving(app):
        turn = await value_of(
            client,
            token,
            "send_preview_message",
            {"agent_id": agent_id, "message": "Can I get refunds on paint?"},
        )
        conversations = await value_of(
            client, token, "list_conversations", {"agent_id": agent_id, "channel": "preview"}
        )

    assert turn["reply"]
    assert turn["conversationId"] == conversations["items"][0]["id"]
    assert conversations["items"][0]["externalUserId"].startswith("mcp:")


async def test_reports_are_served(app: FastAPI, client: AsyncClient) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth)

    async with serving(app):
        usage = await value_of(client, token, "get_usage_report", {"days": 7})
        failures = await value_of(client, token, "get_failure_report")

    assert usage["conversations"]["started"] == 0
    assert "window" in usage
    assert failures["total"] == 0
    assert {item["kind"] for item in failures["classes"]} >= {"ingestion", "tool"}


async def test_allowed_origins_are_set_without_losing_other_settings(
    app: FastAPI, client: AsyncClient
) -> None:
    auth, _ = await account(client)
    agent_id = (await client.post("/agents", json={"name": "Web agent"}, headers=auth)).json()[
        "value"
    ]["id"]
    token = await issue_token(client, auth, WRITE)

    async with serving(app):
        configured = await value_of(
            client,
            token,
            "set_web_allowed_origins",
            {"agent_id": agent_id, "allowed_origins": ["https://Shop.Example.com/"]},
        )
        refused = await call_tool(
            client,
            token,
            "set_web_allowed_origins",
            {"agent_id": agent_id, "allowed_origins": ["https://example.com/path"]},
        )

    assert configured["settings"]["allowedOrigins"] == ["https://shop.example.com"]
    assert error_code(refused) == "INVALID_ORIGIN"


async def test_a_source_can_be_deleted_and_the_knowledge_base_survives(
    app: FastAPI, client: AsyncClient
) -> None:
    """Text sources cannot be edited in place, so correcting knowledge that has gone stale means
    deleting it and adding the replacement. Without a delete over MCP the wrong source stays,
    competes for retrieval, and keeps being answered from."""
    auth, _ = await account(client)
    token = await issue_token(client, auth, WRITE)

    async with serving(app):
        kb = await value_of(client, token, "create_knowledge_base", {"name": "Branches"})
        stale = await value_of(
            client,
            token,
            "add_text_source",
            {"kb_id": kb["id"], "title": "Rusape", "body": "There is no branch in Rusape."},
        )
        await value_of(
            client,
            token,
            "delete_knowledge_source",
            {"kb_id": kb["id"], "source_id": stale["id"]},
        )

        listed = await value_of(client, token, "list_knowledge_sources", {"kb_id": kb["id"]})
        assert listed["totalItems"] == 0
        assert (await value_of(client, token, "get_knowledge_base", {"kb_id": kb["id"]}))["id"]


async def test_a_knowledge_base_can_be_deleted_with_everything_in_it(
    app: FastAPI, client: AsyncClient
) -> None:
    auth, _ = await account(client)
    token = await issue_token(client, auth, WRITE)

    async with serving(app):
        kb = await value_of(client, token, "create_knowledge_base", {"name": "Superseded"})
        await value_of(
            client,
            token,
            "add_text_source",
            {"kb_id": kb["id"], "title": "Old prices", "body": "PVA 20L costs ten dollars."},
        )
        await value_of(client, token, "delete_knowledge_base", {"kb_id": kb["id"]})

        remaining = await value_of(client, token, "list_knowledge_bases", {})
        assert kb["id"] not in {row["id"] for row in remaining["items"]}
