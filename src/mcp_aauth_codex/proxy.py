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
from .providers import ProviderEndpoint
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


def _resource_target(
    resource_uri: str,
    providers: dict[str, ProviderEndpoint],
) -> tuple[ProviderEndpoint, str]:
    parts = urlsplit(resource_uri)
    edoc_id = parts.path.lstrip("/")
    if (
        parts.scheme != "edoc"
        or parts.netloc not in providers
        or not edoc_id
        or "/" in edoc_id
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "resource_uri must identify a configured provider and opaque eDoc ID"
        )
    return providers[parts.netloc], edoc_id


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
    providers = {
        provider.provider_id: provider
        for provider in config.provider_directory()
    }
    if len(providers) != len(config.provider_directory()):
        raise ValueError("provider IDs must be unique")

    @server.tool(description="List the configured eDocs providers.")
    def list_providers() -> dict[str, list[dict[str, str]]]:
        return {
            "providers": [
                provider.public_dict() for provider in providers.values()
            ]
        }

    @server.tool(
        description=(
            "Fetch the current enabled eDoc metadata directly from one provider's "
            "public MCP catalog."
        )
    )
    async def list_resources(provider_id: str) -> dict[str, list[dict[str, Any]]]:
        provider = providers.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider: {provider_id}")
        client_options = {"follow_redirects": False}
        if http_transport is not None:
            client_options["transport"] = http_transport
        try:
            async with (
                httpx2.AsyncClient(**client_options) as http_client,
                Client(
                    streamable_http_client(
                        provider.mcp_url,
                        http_client=http_client,
                    ),
                    mode="legacy",
                ) as client,
            ):
                result = await client.list_resources()
        except Exception as error:
            _raise_remote_error(error)
        resources = []
        for resource in result.resources:
            value = resource.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            uri = str(value.get("uri", ""))
            if urlsplit(uri).netloc != provider_id:
                raise RuntimeError(
                    f"provider {provider_id} returned an unqualified resource URI"
                )
            resources.append(value)
        return {"resources": resources}

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
        provider, edoc_id = _resource_target(resource_uri, providers)
        reserved = {"edoc_id", "provider_id"}.intersection(arguments)
        if reserved:
            raise ValueError(
                "arguments must not override reserved routing fields: "
                + ", ".join(sorted(reserved))
            )
        origin = _resource_origin(provider.mcp_url)
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
                headers={"Edocs-Provider": provider.provider_id},
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
            resource_url=provider.mcp_url,
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
                        provider.mcp_url,
                        http_client=http_client,
                    ),
                    mode="legacy",
                ) as client,
            ):
                if config.function_registry_url:
                    remote_tool = "execute_registered_function"
                    remote_arguments = {
                        "provider_id": provider.provider_id,
                        "edoc_id": edoc_id,
                        "function_id": function_id,
                        "arguments": arguments,
                    }
                else:
                    remote_tool = _remote_tool(function_id)
                    remote_arguments = {
                        "provider_id": provider.provider_id,
                        "edoc_id": edoc_id,
                        **arguments,
                    }
                result = await client.call_tool(
                    remote_tool,
                    remote_arguments,
                )
        except Exception as error:
            _raise_remote_error(error)
        return _result_payload(result)

    if config.function_registry_url:
        @server.tool(
            description=(
                "Upload a schema-conforming function to the shared eDocs "
                "registry. Registration does not authorize its invocation."
            )
        )
        async def register_edocs_function(
            function_id: str,
            description: str,
            input_schema: dict[str, Any],
            implementation: dict[str, Any],
        ) -> dict[str, Any]:
            options = {"follow_redirects": False}
            if http_transport is not None:
                options["transport"] = http_transport
            async with httpx2.AsyncClient(**options) as client:
                response = await client.post(
                    config.function_registry_url,
                    json={
                        "function_id": function_id,
                        "description": description,
                        "input_schema": input_schema,
                        "implementation": implementation,
                    },
                )
            body = response.json()
            if response.status_code != 201:
                detail = (
                    body.get("detail")
                    if isinstance(body, dict)
                    else "function registration failed"
                )
                raise ValueError(detail or "function registration failed")
            return body

    return server
