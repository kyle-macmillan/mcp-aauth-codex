"""Environment-backed configuration for the local eDocs agent bridge."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from aauth_edocs import SigningKey

from .providers import ProviderEndpoint


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
class AgentRuntimeConfig:
    """Runtime values kept outside the plugin manifest and MCP tool output."""

    agent_token: str
    signing_key: SigningKey
    person: str | None = None
    providers: tuple[ProviderEndpoint, ...] = ()
    provider_file: Path | None = None
    remote_mcp_url: str | None = None
    function_registry_url: str | None = None
    control_url: str | None = None
    agent_resource_url: str | None = None
    agent_id: str | None = None

    def provider_directory(self) -> tuple[ProviderEndpoint, ...]:
        if self.provider_file is not None and self.provider_file.exists():
            try:
                provider_values = json.loads(
                    self.provider_file.read_text().strip() or "[]"
                )
                return tuple(
                    ProviderEndpoint(**value) for value in provider_values
                )
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                pass
        if self.providers:
            return self.providers
        if not self.remote_mcp_url:
            raise RuntimeError("proxy has no configured providers")
        return (
            ProviderEndpoint(
                provider_id="alice",
                display_name="Alice",
                description="Alice's governed eDocs",
                mcp_url=self.remote_mcp_url,
            ),
        )

    @classmethod
    def from_env(cls) -> "AgentRuntimeConfig":
        try:
            key_data = json.loads(_read_secret("EDOCS_AGENT_KEY_FILE"))
        except (json.JSONDecodeError, TypeError) as error:
            raise RuntimeError("EDOCS_AGENT_KEY_FILE must contain a private JWK") from error
        if not isinstance(key_data, dict) or "d" not in key_data:
            raise RuntimeError("EDOCS_AGENT_KEY_FILE must contain a private JWK")
        provider_path = Path(_required("EDOCS_PROVIDER_FILE")).expanduser()
        try:
            provider_values = json.loads(provider_path.read_text().strip() or "[]")
            providers = tuple(
                ProviderEndpoint(**value) for value in provider_values
            )
        except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
            raise RuntimeError(
                "EDOCS_PROVIDER_FILE must contain a provider array"
            ) from error
        if not providers:
            raise RuntimeError("EDOCS_PROVIDER_FILE must contain at least one provider")
        function_registry_url = os.environ.get("EDOCS_FUNCTION_REGISTRY_URL")
        control_url = os.environ.get("EDOCS_CONTROL_URL")
        if control_url is None and function_registry_url:
            control_url = function_registry_url.removesuffix(
                "/api/sentinel/functions"
            )
        return cls(
            agent_token=_read_secret("EDOCS_AGENT_TOKEN_FILE"),
            signing_key=SigningKey.from_private_jwk(key_data),
            person=os.environ.get("EDOCS_PERSON"),
            providers=providers,
            provider_file=provider_path,
            function_registry_url=function_registry_url,
            control_url=control_url,
            agent_resource_url=os.environ.get("EDOCS_AGENT_RESOURCE_URL"),
            agent_id=os.environ.get("EDOCS_DEMO_AGENT_ID"),
        )
