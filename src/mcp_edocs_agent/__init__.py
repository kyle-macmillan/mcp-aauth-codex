"""Host-neutral MCP bridge for AAuth-protected eDocs resources."""

from .config import AgentRuntimeConfig
from .mcp_adapter import build_server

__all__ = ["AgentRuntimeConfig", "build_server"]
