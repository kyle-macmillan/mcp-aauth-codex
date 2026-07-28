import json
from pathlib import Path

import anyio
import pytest
from aauth_edocs import SigningKey, issue_agent_token
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client


@pytest.mark.asyncio
async def test_launcher_exposes_tool_over_real_stdio(tmp_path):
    plugin_root = Path(__file__).resolve().parents[1]
    provider_key = SigningKey.generate("provider")
    agent_key = SigningKey.generate("agent")
    token = issue_agent_token(
        issuer="https://ap.example",
        agent="aauth:assistant@example",
        agent_jwk=agent_key.public_jwk,
        ps="https://ps.example",
        key=provider_key,
    )
    key_file = tmp_path / "agent.jwk"
    token_file = tmp_path / "agent.token"
    key_file.write_text(json.dumps(agent_key.private_jwk()))
    token_file.write_text(token)
    params = StdioServerParameters(
        command=str(plugin_root / "scripts" / "run_proxy.sh"),
        env={
            "EDOCS_MCP_URL": "https://resource.example/mcp",
            "EDOCS_AGENT_KEY_FILE": str(key_file),
            "EDOCS_AGENT_TOKEN_FILE": str(token_file),
        },
    )

    errlog_path = tmp_path / "server.stderr"
    with errlog_path.open("w") as errlog:
        with anyio.fail_after(10):
            async with Client(
                stdio_client(params, errlog=errlog),
                mode="legacy",
            ) as client:
                tools = await client.list_tools()

    assert [tool.name for tool in tools.tools] == ["invoke_edocs_function"]
    assert set(tools.tools[0].input_schema["properties"]) == {
        "resource_uri",
        "function_id",
        "arguments",
    }
