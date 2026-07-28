import json

import pytest
from aauth_edocs import EdocsApprovalRequest, SigningKey
from mcp import Client
from mcp_types import ElicitResult, Implementation

from mcp_edocs_agent.config import AgentRuntimeConfig
from mcp_edocs_agent.mcp_adapter import SERVER_INSTRUCTIONS, build_server


class FakeGateway:
    def __init__(self) -> None:
        self.approved_by: list[str] = []

    def list_providers(self):
        return {"providers": []}

    async def list_resources(self, provider_id):
        return {"resources": [{"uri": f"edoc://{provider_id}/doc-1"}]}

    async def invoke_edocs_function(
        self,
        resource_uri,
        function_id,
        arguments,
        approve,
    ):
        decision = await approve(
            EdocsApprovalRequest(
                source_agent="aauth:source@example",
                function_id=function_id,
                edoc_id="doc-1",
                destination_agent="aauth:producer@example",
                controllers=("https://as.example",),
                resource="https://resource.example",
                authorization_audience="https://sentinel.example",
                approval_url="https://ps.example/approval",
                function_args=arguments,
                function_args_hash="sha256:test",
            )
        )
        self.approved_by.append(decision)
        return {"resource_uri": resource_uri, "decision": decision}


def _config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_token="unused",
        signing_key=SigningKey.generate("agent"),
        remote_mcp_url="https://resource.example/mcp",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("client_name", ["codex", "claude-code"])
async def test_standard_mcp_adapter_is_host_independent(client_name):
    gateway = FakeGateway()
    server = build_server(_config(), gateway=gateway)

    async def approve(_context, _params):
        return ElicitResult(action="accept", content={"approve": True})

    async with Client(
        server,
        mode="legacy",
        client_info=Implementation(name=client_name, version="test"),
        elicitation_callback=approve,
    ) as client:
        result = await client.call_tool(
            "invoke_edocs_function",
            {
                "resource_uri": "edoc://alice/doc-1",
                "function_id": "identity@1",
                "arguments": {},
            },
        )

    assert result.is_error is False
    assert json.loads(result.content[0].text)["decision"] == "grant"
    assert gateway.approved_by == ["grant"]


@pytest.mark.asyncio
async def test_invocation_requires_form_elicitation_capability():
    gateway = FakeGateway()
    server = build_server(_config(), gateway=gateway)

    async with Client(server, mode="legacy") as client:
        result = await client.call_tool(
            "invoke_edocs_function",
            {
                "resource_uri": "edoc://alice/doc-1",
                "function_id": "identity@1",
                "arguments": {},
            },
        )

    assert result.is_error is True
    assert "requires an MCP client with form elicitation support" in (
        result.content[0].text
    )
    assert gateway.approved_by == []


@pytest.mark.asyncio
async def test_standard_instructions_and_tool_annotations():
    server = build_server(_config(), gateway=FakeGateway())

    async with Client(server, mode="legacy") as client:
        instructions = client.instructions
        tools = await client.list_tools()

    assert instructions == SERVER_INSTRUCTIONS
    by_name = {tool.name: tool for tool in tools.tools}
    assert all(tool.output_schema is not None for tool in by_name.values())
    for name in ("list_providers", "list_resources"):
        assert by_name[name].annotations.read_only_hint is True
        assert by_name[name].annotations.destructive_hint is False
        assert by_name[name].annotations.idempotent_hint is True
    invoke = by_name["invoke_edocs_function"].annotations
    assert invoke.read_only_hint is False
    assert invoke.destructive_hint is False
    assert invoke.idempotent_hint is False
