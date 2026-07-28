"""Standard MCP adapter for the host-independent eDocs gateway."""

from __future__ import annotations

import json
from typing import Any

from aauth_edocs import AAuthError, EdocsApprovalRequest
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import AgentRuntimeConfig
from .gateway import (
    EdocsGateway,
    _leaf_exception,
    _raise_remote_error,
    _remote_tool,
    _resource_origin,
    _resource_target,
    _result_payload,
)

SERVER_INSTRUCTIONS = (
    "Discover an eDoc by calling list_providers, then list_resources with the "
    "selected provider ID. Never invent or alter an edoc:// URI. Invoke only "
    "the function and exact arguments the user requested. Invocation may pause "
    "for a separate eDocs consent decision."
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
ADDITIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


class ConsentDecision(BaseModel):
    approve: bool = Field(description="Approve this exact eDocs operation")

    @classmethod
    def model_json_schema(cls, *args, **kwargs):
        """Render the strict root shape required by MCP form elicitation."""
        schema = super().model_json_schema(*args, **kwargs)
        schema.pop("title", None)
        return schema


def _approval_message(review: EdocsApprovalRequest) -> str:
    arguments = json.dumps(
        review.function_args,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"Approve {review.function_id} on {review.edoc_id}; "
        f"{review.source_agent} -> {review.destination_agent}; "
        f"controllers={','.join(review.controllers)}; "
        f"arguments={arguments}"
    )


def _supports_form_elicitation(ctx: Context) -> bool:
    capabilities = ctx.client_capabilities
    if capabilities is None or capabilities.elicitation is None:
        return False
    elicitation = capabilities.elicitation
    return elicitation.form is not None or elicitation.url is None


def build_server(
    config: AgentRuntimeConfig,
    *,
    agent_transport=None,
    consent_transport=None,
    http_transport=None,
    gateway: EdocsGateway | None = None,
) -> MCPServer:
    """Expose the generic gateway to any MCP client supporting elicitation."""
    gateway = gateway or EdocsGateway(
        config,
        agent_transport=agent_transport,
        consent_transport=consent_transport,
        http_transport=http_transport,
    )
    server = MCPServer(
        "eDocs AAuth agent bridge",
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.tool(
        description="List the configured eDocs providers.",
        annotations=READ_ONLY,
    )
    def list_providers() -> dict[str, list[dict[str, str]]]:
        return gateway.list_providers()

    @server.tool(
        description=(
            "Fetch current enabled eDoc metadata directly from one provider's "
            "public MCP catalog."
        ),
        annotations=READ_ONLY,
    )
    async def list_resources(
        provider_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        return await gateway.list_resources(provider_id)

    @server.tool(
        description=(
            "Invoke a registered, versioned function on an eDoc through the "
            "AAuth-protected remote MCP server. A successful invocation records "
            "a derived materialization."
        ),
        annotations=ADDITIVE,
    )
    async def invoke_edocs_function(
        resource_uri: str,
        function_id: str,
        arguments: dict[str, Any],
        ctx: Context,
    ) -> dict[str, Any]:
        if not _supports_form_elicitation(ctx):
            raise RuntimeError(
                "this eDocs operation requires an MCP client with form "
                "elicitation support"
            )

        async def prompt(review: EdocsApprovalRequest) -> str:
            elicitation = await ctx.elicit(
                _approval_message(review),
                ConsentDecision,
            )
            if elicitation.action == "cancel":
                raise AAuthError(
                    "request_cancelled",
                    499,
                    "eDocs approval was cancelled",
                )
            if elicitation.action == "decline":
                return "deny"
            return "grant" if elicitation.data.approve else "deny"

        return await gateway.invoke_edocs_function(
            resource_uri,
            function_id,
            arguments,
            prompt,
        )

    if config.function_registry_url:

        @server.tool(
            description=(
                "Upload a schema-conforming function to the shared eDocs "
                "registry. Registration does not authorize its invocation."
            ),
            annotations=ADDITIVE,
        )
        async def register_edocs_function(
            function_id: str,
            description: str,
            input_schema: dict[str, Any],
            implementation: dict[str, Any],
        ) -> dict[str, Any]:
            return await gateway.register_edocs_function(
                function_id,
                description,
                input_schema,
                implementation,
            )

    return server


__all__ = [
    "ConsentDecision",
    "SERVER_INSTRUCTIONS",
    "build_server",
]
