"""Standard MCP adapter for the host-independent eDocs gateway."""

from __future__ import annotations

import json
import secrets
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
    "returned provider_ref. Call list_edocs_functions to discover registered "
    "function IDs and input schemas. Invoke only with a resource_ref returned "
    "by list_resources and the exact function and arguments the user requested. "
    "Successful invocations may return derived_edoc_id; call "
    "publish_derived_edoc with that id to expose the result on this agent's "
    "resource server for peers. References are opaque and session-scoped; "
    "never invent or alter them. Invocation may pause for a separate eDocs "
    "consent decision."
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
    provider_refs: dict[str, str] = {}
    provider_refs_by_id: dict[str, str] = {}
    resource_refs: dict[str, str] = {}

    def opaque_ref(prefix: str) -> str:
        return f"{prefix}_{secrets.token_urlsafe(18)}"

    @server.tool(
        description="List the configured eDocs providers.",
        annotations=READ_ONLY,
    )
    def list_providers() -> dict[str, list[dict[str, str]]]:
        providers = gateway.list_providers()["providers"]
        discovered = []
        for provider in providers:
            provider_id = provider["provider_id"]
            provider_ref = provider_refs_by_id.get(provider_id)
            if provider_ref is None:
                provider_ref = opaque_ref("provider")
                provider_refs[provider_ref] = provider_id
                provider_refs_by_id[provider_id] = provider_ref
            discovered.append({**provider, "provider_ref": provider_ref})
        return {"providers": discovered}

    @server.tool(
        description=(
            "Fetch current enabled eDoc metadata directly from one provider's "
            "public MCP catalog."
        ),
        annotations=READ_ONLY,
    )
    async def list_resources(
        provider_ref: str,
    ) -> dict[str, list[dict[str, Any]]]:
        provider_id = provider_refs.get(provider_ref)
        if provider_id is None:
            raise ValueError(
                "unknown provider_ref; call list_providers and use an exact "
                "returned provider_ref"
            )
        resources = (await gateway.list_resources(provider_id))["resources"]
        discovered = []
        for resource in resources:
            resource_ref = opaque_ref("resource")
            resource_refs[resource_ref] = str(resource["uri"])
            discovered.append({**resource, "resource_ref": resource_ref})
        return {"resources": discovered}

    @server.tool(
        description=(
            "Invoke a registered, versioned function on an eDoc through the "
            "AAuth-protected remote MCP server. A successful invocation records "
            "a derived materialization."
        ),
        annotations=ADDITIVE,
    )
    async def invoke_edocs_function(
        resource_ref: str,
        function_id: str,
        arguments: dict[str, Any],
        ctx: Context,
    ) -> dict[str, Any]:
        resource_uri = resource_refs.get(resource_ref)
        if resource_uri is None:
            raise ValueError(
                "unknown resource_ref; call list_resources and use an exact "
                "returned resource_ref"
            )
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

    if config.agent_resource_url and config.sentinel_url:

        @server.tool(
            description=(
                "Publish a derived eDoc this agent obtained from a prior "
                "successful invoke_edocs_function. Pass the derived_edoc_id "
                "from that result. Publishing makes the resource discoverable "
                "and invocable by peers through Sentinel; it does not create "
                "policy."
            ),
            annotations=ADDITIVE,
        )
        async def publish_derived_edoc(
            derived_edoc_id: str,
            title: str | None = None,
            description: str | None = None,
        ) -> dict[str, Any]:
            return await gateway.publish_derived_edoc(
                derived_edoc_id,
                title=title,
                description=description,
            )

    if config.function_registry_url:

        @server.tool(
            description=(
                "List registered eDocs function descriptors and input schemas. "
                "Registration does not imply that invocation is authorized."
            ),
            annotations=READ_ONLY,
        )
        async def list_edocs_functions() -> dict[
            str,
            list[dict[str, Any]],
        ]:
            return await gateway.list_edocs_functions()

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
