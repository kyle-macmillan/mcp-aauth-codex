"""Codex stdio MCP server backed by an AAuth-aware HTTP MCP client."""

from __future__ import annotations

import sys
import traceback
import json
from typing import Any
from urllib.parse import urlsplit

import httpx2
from aauth_edocs import (
    AAuthError,
    ApprovalRequired,
    AuthorizationCoordinator,
    EdocsApprovalHandler,
    EdocsApprovalRequest,
    EdocsConsentClient,
)
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_aauth import AAuthAgentHTTPAuth
from pydantic import BaseModel, Field

from .config import ProxyConfig
from .transport import AsyncRequestsTransport, person_transport


class ConsentDecision(BaseModel):
    approve: bool = Field(description="Approve this exact eDocs operation")

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        """Render the strict form shape accepted by the Codex MCP host."""
        schema = super().model_json_schema(*args, **kwargs)
        schema.pop("title", None)
        return schema


def _approval_message(review: EdocsApprovalRequest) -> str:
    controllers = ", ".join(review.controllers)
    return (
        "Approve this eDocs operation?\n"
        f"Function: {review.function_id}\n"
        f"eDoc: {review.edoc_id}\n"
        f"Source agent: {review.source_agent}\n"
        f"Destination agent: {review.destination_agent}\n"
        f"Resource: {review.resource}\n"
        f"Authorization service: {review.authorization_audience}\n"
        f"Controllers: {controllers}\n"
        "Arguments:\n"
        f"{json.dumps(review.function_args, indent=2, sort_keys=True)}"
    )


def _remote_tool(function_id: str) -> str:
    name, separator, version = function_id.partition("@")
    if (
        not separator
        or not name
        or not version
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in name
        )
    ):
        raise ValueError("function_id must be a versioned identifier such as identity@1")
    return name


def _resource_edoc_id(resource_uri: str) -> str:
    parts = urlsplit(resource_uri)
    edoc_id = parts.path.lstrip("/")
    if (
        parts.scheme != "edoc"
        or parts.netloc != "demo"
        or not edoc_id
        or "/" in edoc_id
        or parts.query
        or parts.fragment
    ):
        raise ValueError("resource_uri must be an edoc://demo/<opaque-id> URI")
    return edoc_id


def _resource_origin(mcp_url: str) -> str:
    parts = urlsplit(mcp_url)
    return f"{parts.scheme}://{parts.netloc}"


def _result_payload(result) -> dict:
    content = []
    for item in result.content:
        if hasattr(item, "model_dump"):
            content.append(item.model_dump(mode="json"))
        else:
            content.append({"text": str(item)})
    payload = {"content": content, "is_error": bool(result.is_error)}
    if result.structured_content is not None:
        payload["structured_content"] = result.structured_content
    return payload


def _leaf_exception(error: BaseException) -> BaseException:
    """Return the first concrete error hidden by an async task group."""
    nested = getattr(error, "exceptions", None)
    if nested:
        return _leaf_exception(nested[0])
    return error


def _raise_remote_error(error: Exception) -> None:
    """Preserve diagnostics while returning a useful MCP tool error."""
    traceback.print_exception(error, file=sys.stderr)
    leaf = _leaf_exception(error)
    raise RuntimeError(
        "remote eDocs MCP call failed: "
        f"{type(leaf).__name__}: {leaf}"
    ) from leaf


def build_server(
    config: ProxyConfig,
    *,
    agent_transport=None,
    consent_transport=None,
    http_transport=None,
) -> MCPServer:
    """Build the local server; injectable transports keep the flow testable."""
    agent_transport = agent_transport or AsyncRequestsTransport()
    coordinator = AuthorizationCoordinator(
        key=config.signing_key,
        agent_token=config.agent_token,
        transport=agent_transport,
    )
    if not coordinator.ps_url:
        raise RuntimeError("agent token has no Person Server URL")
    consent_transport = consent_transport or person_transport(
        config.person,
        coordinator.ps_url,
    )
    consent_client = EdocsConsentClient(consent_transport)
    server = MCPServer("eDocs AAuth proxy")

    @server.tool(
        description=(
            "Invoke a registered, versioned function on an eDoc through the "
            "AAuth-protected remote MCP server."
        )
    )
    async def invoke_edocs_function(
        resource_uri: str,
        function_id: str,
        arguments: dict[str, Any],
        ctx: Context,
    ) -> dict:
        async def prompt(review: EdocsApprovalRequest) -> str:
            elicitation = await ctx.elicit(_approval_message(review), ConsentDecision)
            if elicitation.action == "cancel":
                raise AAuthError(
                    "request_cancelled",
                    499,
                    "eDocs approval was cancelled",
                )
            if elicitation.action == "decline":
                return "deny"
            return "grant" if elicitation.data.approve else "deny"

        approval_handler = EdocsApprovalHandler(
            consent_client=consent_client,
            prompt=prompt,
        )
        edoc_id = _resource_edoc_id(resource_uri)
        if "edoc_id" in arguments:
            raise ValueError("arguments must not override the reserved edoc_id field")
        origin = _resource_origin(config.remote_mcp_url)
        signing_auth = AAuthAgentHTTPAuth(
            key=config.signing_key,
            token=config.agent_token,
        )
        authorize_options = {
            "auth": signing_auth,
            "follow_redirects": False,
        }
        if http_transport is not None:
            authorize_options["transport"] = http_transport
        async with httpx2.AsyncClient(**authorize_options) as authorize_client:
            response = await authorize_client.post(
                f"{origin}/authorize",
                json={
                    "edoc_id": edoc_id,
                    "function_id": function_id,
                    "function_args": arguments,
                },
            )
        if response.status_code != 200:
            raise AAuthError.from_response(response.status_code, response.json())
        resource_token = response.json().get("resource_token")
        if not isinstance(resource_token, str):
            raise RuntimeError("resource returned no resource token")
        authorization = await coordinator.begin_async(
            resource_token,
            resource_url=config.remote_mcp_url,
        )
        if isinstance(authorization, ApprovalRequired):
            await approval_handler(authorization)
            await coordinator.complete_async(authorization)

        auth = AAuthAgentHTTPAuth(
            key=config.signing_key,
            token=config.agent_token,
            coordinator=coordinator,
            on_approval_required=approval_handler,
        )
        client_options = {
            "auth": auth,
            "follow_redirects": False,
        }
        if http_transport is not None:
            client_options["transport"] = http_transport
        try:
            async with (
                httpx2.AsyncClient(**client_options) as http_client,
                Client(
                    streamable_http_client(
                        config.remote_mcp_url,
                        http_client=http_client,
                    ),
                    mode="legacy",
                ) as client,
            ):
                result = await client.call_tool(
                    _remote_tool(function_id),
                    {"edoc_id": edoc_id, **arguments},
                )
        except Exception as error:
            _raise_remote_error(error)
        return _result_payload(result)

    return server
