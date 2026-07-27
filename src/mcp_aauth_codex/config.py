"""Environment-backed configuration for the local proxy."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from aauth_edocs import SigningKey


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _read_secret(name: str) -> str:
    path = Path(_required(name)).expanduser()
    try:
        return path.read_text().strip()
    except OSError as error:
        raise RuntimeError(f"cannot read {name}: {path}") from error


@dataclass(frozen=True)
class ProxyConfig:
    """Runtime values kept outside the plugin manifest and MCP tool output."""

    remote_mcp_url: str
    agent_token: str
    signing_key: SigningKey
    person: str | None = None

    @classmethod
    def from_env(cls) -> "ProxyConfig":
        try:
            key_data = json.loads(_read_secret("EDOCS_AGENT_KEY_FILE"))
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError("EDOCS_AGENT_KEY_FILE must contain a private JWK") from error
        if not isinstance(key_data, dict) or "d" not in key_data:
            raise RuntimeError("EDOCS_AGENT_KEY_FILE must contain a private JWK")
        return cls(
            remote_mcp_url=_required("EDOCS_MCP_URL"),
            agent_token=_read_secret("EDOCS_AGENT_TOKEN_FILE"),
            signing_key=SigningKey.from_private_jwk(key_data),
            person=os.environ.get("EDOCS_PERSON"),
        )
