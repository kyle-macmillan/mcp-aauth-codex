import pytest

from aauth_edocs import (
    EMPTY_FUNCTION_ARGS_HASH,
    EdocsApprovalRequest,
    SigningKey,
    issue_agent_token,
)
from mcp import Client
from mcp_aauth_codex.config import ProxyConfig
from mcp.server.elicitation import render_elicitation_schema
from mcp_aauth_codex.proxy import (
    ConsentDecision,
    _approval_message,
    _resource_edoc_id,
    _remote_tool,
    build_server,
)


def test_remote_tool_is_derived_from_versioned_function():
    assert _remote_tool("identity@1") == "identity"
    assert _remote_tool("summarize_edoc@2026-07") == "summarize_edoc"


def test_codex_consent_schema_omits_pydantic_root_title():
    schema = render_elicitation_schema(ConsentDecision)

    assert set(schema) == {"type", "properties", "required"}


@pytest.mark.parametrize(
    "function_id",
    ["identity", "@1", "identity@", "../identity@1", "identity space@1"],
)
def test_remote_tool_rejects_unversioned_or_unsafe_names(function_id):
    with pytest.raises(ValueError, match="versioned identifier"):
        _remote_tool(function_id)


def test_prompt_contains_ps_verified_edocs_facts():
    review = EdocsApprovalRequest(
        source_agent="aauth:source@example",
        function_id="identity@1",
        edoc_id="doc-123",
        destination_agent="aauth:assistant@example",
        controllers=("https://as-a.example", "https://as-b.example"),
        resource="https://resource.example",
        authorization_audience="https://sentinel.example",
        approval_url="https://ps.example/consent/pending",
        function_args={},
        function_args_hash=EMPTY_FUNCTION_ARGS_HASH,
    )

    message = _approval_message(review)

    for value in (
        review.source_agent,
        review.function_id,
        review.edoc_id,
        review.destination_agent,
        *review.controllers,
        review.resource,
        review.authorization_audience,
    ):
        assert value in message
    assert "Arguments:\n{}" in message


@pytest.mark.asyncio
async def test_server_exposes_only_constrained_edocs_tool():
    provider_key = SigningKey.generate("provider")
    agent_key = SigningKey.generate("agent")
    token = issue_agent_token(
        issuer="https://ap.example",
        agent="aauth:assistant@example",
        agent_jwk=agent_key.public_jwk,
        ps="https://ps.example",
        key=provider_key,
    )
    config = ProxyConfig(
        remote_mcp_url="https://resource.example/mcp",
        agent_token=token,
        signing_key=agent_key,
    )
    server = build_server(
        config,
        agent_transport=object(),
        consent_transport=object(),
    )

    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == ["invoke_edocs_function"]
    schema = tools.tools[0].input_schema
    assert schema["required"] == ["resource_uri", "function_id", "arguments"]
    assert set(schema["properties"]) == {
        "resource_uri",
        "function_id",
        "arguments",
    }


def test_resource_uri_resolves_only_opaque_demo_edocs():
    assert _resource_edoc_id("edoc://demo/doc_01JDEMO7F3A") == "doc_01JDEMO7F3A"
    for value in ("employees.csv", "edoc://alice/doc-1", "edoc://demo/a/b"):
        with pytest.raises(ValueError, match="resource_uri"):
            _resource_edoc_id(value)
