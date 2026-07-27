"""Codex-facing MCP proxy for AAuth-protected eDocs resources."""

from .config import ProxyConfig
from .proxy import build_server

__all__ = ["ProxyConfig", "build_server"]
