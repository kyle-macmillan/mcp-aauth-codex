import json

import pytest
from aauth_edocs import SigningKey

from mcp_aauth_codex.config import ProxyConfig


def test_loads_secrets_from_files(monkeypatch, tmp_path):
    key = SigningKey.generate("agent-key")
    key_path = tmp_path / "agent.jwk"
    token_path = tmp_path / "agent.token"
    provider_path = tmp_path / "providers.json"
    key_path.write_text(json.dumps(key.private_jwk()))
    token_path.write_text("signed-agent-token\n")
    provider_path.write_text(
        json.dumps(
            [
                {
                    "provider_id": "alice",
                    "display_name": "Alice",
                    "description": "Alice data",
                    "mcp_url": "https://alice.example/mcp",
                },
                {
                    "provider_id": "bob",
                    "display_name": "Bob",
                    "description": "Bob data",
                    "mcp_url": "https://bob.example/mcp",
                },
            ]
        )
    )
    monkeypatch.setenv("EDOCS_PROVIDER_FILE", str(provider_path))
    monkeypatch.setenv("EDOCS_AGENT_KEY_FILE", str(key_path))
    monkeypatch.setenv("EDOCS_AGENT_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("EDOCS_PERSON", "alice")
    monkeypatch.setenv(
        "EDOCS_FUNCTION_REGISTRY_URL",
        "http://127.0.0.1:8721/api/sentinel/functions",
    )

    config = ProxyConfig.from_env()

    assert [provider.provider_id for provider in config.providers] == [
        "alice",
        "bob",
    ]
    assert config.providers[1].mcp_url == "https://bob.example/mcp"
    assert config.agent_token == "signed-agent-token"
    assert config.signing_key.public_jwk == key.public_jwk
    assert config.person == "alice"
    assert config.function_registry_url == (
        "http://127.0.0.1:8721/api/sentinel/functions"
    )


def test_requires_private_key_file(monkeypatch, tmp_path):
    key_path = tmp_path / "agent.jwk"
    token_path = tmp_path / "agent.token"
    provider_path = tmp_path / "providers.json"
    key_path.write_text("{}")
    token_path.write_text("token")
    provider_path.write_text("[]")
    monkeypatch.setenv("EDOCS_PROVIDER_FILE", str(provider_path))
    monkeypatch.setenv("EDOCS_AGENT_KEY_FILE", str(key_path))
    monkeypatch.setenv("EDOCS_AGENT_TOKEN_FILE", str(token_path))

    with pytest.raises(RuntimeError, match="private JWK"):
        ProxyConfig.from_env()
