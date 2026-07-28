"""Compatibility exports for the host-neutral MCP adapter."""

from mcp_edocs_agent.mcp_adapter import (
    ConsentDecision,
    _approval_message,
    _leaf_exception,
    _raise_remote_error,
    _remote_tool,
    _resource_origin,
    _resource_target,
    _result_payload,
    build_server,
)

__all__ = ["ConsentDecision", "build_server"]
