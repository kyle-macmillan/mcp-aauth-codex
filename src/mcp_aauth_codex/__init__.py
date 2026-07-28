"""Compatibility package for the host-neutral eDocs agent bridge."""

from mcp_edocs_agent import AgentRuntimeConfig, build_server

ProxyConfig = AgentRuntimeConfig

__all__ = ["AgentRuntimeConfig", "ProxyConfig", "build_server"]
