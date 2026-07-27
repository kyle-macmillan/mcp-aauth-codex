import json

import pytest
from aauth_edocs import SigningKey

from mcp_aauth_codex.config import ProxyConfig


def test_loads_secrets_from_files(monkeypatch, tmp_path):
    key = SigningKey.generate("agent-key")
    key_path = tmp_path / "agent.jwk"
    token_path = tmp_path / "agent.token"
    key_path.write_text(json.dumps(key.private_jwk()))
    token_path.write_text("signed-agent-token\n")
    monkeypatch.setenv("EDOCS_MCP_URL", "https://resource.example/mcp")
    monkeypatch.setenv("EDOCS_AGENT_KEY_FILE", str(key_path))
    monkeypatch.setenv("EDOCS_AGENT_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("EDOCS_PERSON", "alice")

    config = ProxyConfig.from_env()

    assert config.remote_mcp_url == "https://resource.example/mcp"
    assert config.agent_token == "signed-agent-token"
    assert config.signing_key.public_jwk == key.public_jwk
    assert config.person == "alice"


def test_requires_private_key_file(monkeypatch, tmp_path):
    key_path = tmp_path / "agent.jwk"
    token_path = tmp_path / "agent.token"
    key_path.write_text("{}")
    token_path.write_text("token")
    monkeypatch.setenv("EDOCS_MCP_URL", "https://resource.example/mcp")
    monkeypatch.setenv("EDOCS_AGENT_KEY_FILE", str(key_path))
    monkeypatch.setenv("EDOCS_AGENT_TOKEN_FILE", str(token_path))

    with pytest.raises(RuntimeError, match="private JWK"):
        ProxyConfig.from_env()
