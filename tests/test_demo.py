import json
import socket
import stat

import pytest
from mcp import Client
from mcp_types import ElicitResult

from mcp_aauth_codex.config import ProxyConfig
from mcp_aauth_codex.demo import (
    DESTINATION_AGENT,
    EDOC_ID,
    FUNCTION_ID,
    DemoStack,
    DemoUrls,
)
from mcp_aauth_codex.proxy import build_server


def _free_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


@pytest.mark.asyncio
async def test_live_demo_stack_runs_complete_flow(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    stack = DemoStack(
        state_dir,
        DemoUrls(
            ap=_free_url(),
            ps=_free_url(),
            sentinel=_free_url(),
            controller_a=_free_url(),
            controller_b=_free_url(),
            resource=_free_url(),
        ),
    )
    try:
        stack.start()
        env = {}
        for line in (state_dir / "demo.env").read_text().splitlines():
            name, value = line.split("=", 1)
            env[name] = value
            monkeypatch.setenv(name, value)

        proxy = build_server(ProxyConfig.from_env())
        prompts = []

        async def approve(_context, params):
            prompts.append(params)
            return ElicitResult(action="accept", content={"approve": True})

        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            result = await client.call_tool(
                "invoke_edocs_function",
                {"edoc_id": EDOC_ID, "function_id": FUNCTION_ID},
            )

        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert payload["content"][0]["text"] == "hello"
        assert len(prompts) == 1
        assert DESTINATION_AGENT in prompts[0].message
        assert stack.registry is not None
        assert len(stack.registry.materialized) == 1
        assert env["EDOCS_MCP_URL"] == stack.urls.mcp
        for name in ("agent.jwk", "agent.token", "demo.env"):
            mode = stat.S_IMODE((state_dir / name).stat().st_mode)
            assert mode == 0o600
    finally:
        stack.stop()

    assert not (state_dir / "ready").exists()
