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
        self.functions = [
            {
                "function_id": "identity@1",
                "description": "Return a document value",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "digest": "sha256:identity",
            }
        ]

    def list_providers(self):
        return {
            "providers": [
                {
                    "provider_id": "alice",
                    "display_name": "Alice",
                    "description": "Alice's governed eDocs",
                }
            ]
        }

    async def list_resources(self, provider_id):
        return {"resources": [{"uri": f"edoc://{provider_id}/doc-1"}]}

    async def list_edocs_functions(self):
        return {"functions": list(self.functions)}

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


def _registry_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        agent_token="unused",
        signing_key=SigningKey.generate("agent"),
        remote_mcp_url="https://resource.example/mcp",
        function_registry_url="https://registry.example/functions",
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
async def test_discovery_references_cannot_be_guessed_or_skipped():
    gateway = FakeGateway()
    server = build_server(_config(), gateway=gateway)

    async with Client(server, mode="legacy") as client:
        resources = await client.call_tool(
            "list_resources",
            {"provider_ref": "alice"},
        )
        invocation = await client.call_tool(
            "invoke_edocs_function",
            {
                "resource_ref": "edoc://alice/doc-1",
                "function_id": "identity@1",
                "arguments": {},
            },
        )

    assert resources.is_error is True
    assert "call list_providers" in resources.content[0].text
    assert invocation.is_error is True
    assert "call list_resources" in invocation.content[0].text
    assert gateway.approved_by == []


@pytest.mark.asyncio
async def test_standard_instructions_and_tool_annotations():
    server = build_server(_config(), gateway=FakeGateway())

    async with Client(server, mode="legacy") as client:
        instructions = client.instructions
        tools = await client.list_tools()

    assert instructions == SERVER_INSTRUCTIONS
    by_name = {tool.name: tool for tool in tools.tools}
    assert by_name["list_resources"].input_schema["required"] == [
        "provider_ref"
    ]
    assert by_name["invoke_edocs_function"].input_schema["required"] == [
        "resource_ref",
        "function_id",
        "arguments",
    ]
    assert all(tool.output_schema is not None for tool in by_name.values())
    for name in ("list_providers", "list_resources"):
        assert by_name[name].annotations.read_only_hint is True
        assert by_name[name].annotations.destructive_hint is False
        assert by_name[name].annotations.idempotent_hint is True
    invoke = by_name["invoke_edocs_function"].annotations
    assert invoke.read_only_hint is False
    assert invoke.destructive_hint is False
    assert invoke.idempotent_hint is False


@pytest.mark.asyncio
async def test_function_discovery_is_read_only_and_live():
    gateway = FakeGateway()
    server = build_server(_registry_config(), gateway=gateway)

    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()
        before = await client.call_tool("list_edocs_functions", {})
        gateway.functions.append(
            {
                "function_id": "summarize@1",
                "description": "Summarize a document",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "digest": "sha256:summarize",
            }
        )
        after = await client.call_tool("list_edocs_functions", {})

    by_name = {tool.name: tool for tool in tools.tools}
    discovery = by_name["list_edocs_functions"].annotations
    assert discovery.read_only_hint is True
    assert discovery.destructive_hint is False
    assert discovery.idempotent_hint is True
    assert [
        item["function_id"]
        for item in json.loads(before.content[0].text)["functions"]
    ] == ["identity@1"]
    assert [
        item["function_id"]
        for item in json.loads(after.content[0].text)["functions"]
    ] == ["identity@1", "summarize@1"]
