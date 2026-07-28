"""Compatibility aliases for the host-neutral runtime configuration."""

from mcp_edocs_agent.config import AgentRuntimeConfig

ProxyConfig = AgentRuntimeConfig

__all__ = ["AgentRuntimeConfig", "ProxyConfig"]
