import importlib.util
import json
import sys
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ElicitRequestParams, ElicitResult, Resource as MCPResource

from mcp_aauth import aauth_authorization
from mcp_edocs_agent.config import AgentRuntimeConfig
from mcp_edocs_agent.mcp_adapter import build_server


def _edocs_test_support():
    """Load the established cross-repository integration world."""
    path = (
        Path(__file__).resolve().parents[2]
        / "mcp-aauth"
        / "tests"
        / "test_edocs_end_to_end.py"
    )
    spec = importlib.util.spec_from_file_location("edocs_end_to_end_support", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_bridge_completes_full_aauth_flow_after_mcp_elicitation():
    support = _edocs_test_support()
    (
        transport,
        keys,
        registry,
        proposal,
        resource,
        resolver,
        agent_token,
    ) = support.world()
    remote = MCPServer("edocs-remote")

    @remote.tool()
    def identity(provider_id: str, edoc_id: str, ctx: Context) -> str:
        assert provider_id == "alice"
        authorization = ctx.request_context.request.scope["aauth"]
        return resource.identity(authorization, edoc_id=edoc_id)["message"]

    async def list_resources() -> list[MCPResource]:
        return [
            MCPResource(
                uri=f"edoc://alice/{support.EDOC_ID}",
                name=support.EDOC_ID,
            )
        ]

    remote.list_resources = list_resources
    remote_app = remote.streamable_http_app(host="resource.local")
    authorized_app = aauth_authorization(
        key_resolver=resolver,
        issuer=support.SENTINEL,
        audience=support.RESOURCE,
    )(
        remote_app
    )
    challenge_router = support.DemoApplication(
        resource,
        remote_app,
        key_resolver=resolver,
        challenge_mcp=True,
    )

    async def challenged_app(scope, receive, send):
        token_type = support._presented_token_type(scope)
        if (
            scope["type"] == "http"
            and scope.get("path") == "/mcp"
            and token_type is not None
            and token_type != support.AGENT_TYP
        ):
            await authorized_app(scope, receive, send)
            return
        await challenge_router(scope, receive, send)
    assert transport.request(
        "POST",
        f"{support.PS}/login",
        json={"person": "alice"},
    ).status_code == 200
    bridge = build_server(
        AgentRuntimeConfig(
            remote_mcp_url=f"{support.RESOURCE}/mcp",
            agent_token=agent_token,
            signing_key=keys["agent"],
        ),
        agent_transport=transport,
        consent_transport=transport,
        http_transport=httpx2.ASGITransport(app=challenged_app),
    )
    prompts: list[ElicitRequestParams] = []

    async def approve(_context, params: ElicitRequestParams) -> ElicitResult:
        prompts.append(params)
        return ElicitResult(action="accept", content={"approve": True})

    async with remote.session_manager.run():
        async with Client(
            bridge,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            providers = await client.call_tool("list_providers", {})
            provider_ref = json.loads(providers.content[0].text)["providers"][0][
                "provider_ref"
            ]
            resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_ref},
            )
            resource_ref = json.loads(resources.content[0].text)["resources"][0][
                "resource_ref"
            ]
            result = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_ref,
                    "function_id": support.FUNCTION,
                    "arguments": {},
                },
            )

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["is_error"] is False
    assert payload["content"][0]["text"] == "hello"
    assert len(prompts) == 1
    prompt = prompts[0]
    assert support.FUNCTION in prompt.message
    assert support.EDOC_ID in prompt.message
    assert support.SOURCE in prompt.message
    assert support.AGENT in prompt.message
    assert support.AS_A in prompt.message
    assert support.AS_B in prompt.message
    assert proposal not in registry.materialized
