"""Host-independent eDocs discovery, authorization, and invocation."""

from __future__ import annotations

import sys
import traceback
from collections.abc import Awaitable, Callable
from typing import Any, Literal
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
from mcp_aauth import AAuthAgentHTTPAuth

from .config import AgentRuntimeConfig
from .providers import ProviderEndpoint
from .transport import AsyncRequestsTransport, person_transport

ApprovalDecision = Literal["grant", "deny"]
ApprovalCallback = Callable[
    [EdocsApprovalRequest],
    Awaitable[ApprovalDecision],
]


class EdocsGateway:
    """Perform eDocs operations without depending on a coding-agent host."""

    def __init__(
        self,
        config: AgentRuntimeConfig,
        *,
        agent_transport=None,
        consent_transport=None,
        http_transport=None,
    ) -> None:
        self.config = config
        self.http_transport = http_transport
        agent_transport = agent_transport or AsyncRequestsTransport()
        self.coordinator = AuthorizationCoordinator(
            key=config.signing_key,
            agent_token=config.agent_token,
            transport=agent_transport,
        )
        if not self.coordinator.ps_url:
            raise RuntimeError("agent token has no Person Server URL")
        consent_transport = consent_transport or person_transport(
            config.person,
            self.coordinator.ps_url,
        )
        self.consent_client = EdocsConsentClient(consent_transport)
        self._refresh_providers()

    def _refresh_providers(self) -> None:
        directory = self.config.provider_directory()
        self.providers = {
            provider.provider_id: provider for provider in directory
        }
        if len(self.providers) != len(directory):
            raise ValueError("provider IDs must be unique")

    def list_providers(self) -> dict[str, list[dict[str, str]]]:
        self._refresh_providers()
        return {
            "providers": [
                provider.public_dict() for provider in self.providers.values()
            ]
        }

    async def list_resources(
        self,
        provider_id: str,
    ) -> dict[str, list[dict[str, Any]]]:
        self._refresh_providers()
        provider = self.providers.get(provider_id)
        if provider is None:
            raise ValueError(f"unknown provider: {provider_id}")
        client_options = {"follow_redirects": False}
        if self.http_transport is not None:
            client_options["transport"] = self.http_transport
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

    async def invoke_edocs_function(
        self,
        resource_uri: str,
        function_id: str,
        arguments: dict[str, Any],
        approve: ApprovalCallback,
    ) -> dict[str, Any]:
        approval_handler = EdocsApprovalHandler(
            consent_client=self.consent_client,
            prompt=approve,
        )
        self._refresh_providers()
        provider, edoc_id = _resource_target(
            resource_uri,
            self.providers,
        )
        reserved = {"edoc_id", "provider_id"}.intersection(arguments)
        if reserved:
            raise ValueError(
                "arguments must not override reserved routing fields: "
                + ", ".join(sorted(reserved))
            )
        origin = _resource_origin(provider.mcp_url)
        signing_auth = AAuthAgentHTTPAuth(
            key=self.config.signing_key,
            token=self.config.agent_token,
        )
        authorize_options = {
            "auth": signing_auth,
            "follow_redirects": False,
        }
        if self.http_transport is not None:
            authorize_options["transport"] = self.http_transport
        async with httpx2.AsyncClient(
            **authorize_options
        ) as authorize_client:
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
            raise AAuthError.from_response(
                response.status_code,
                response.json(),
            )
        resource_token = response.json().get("resource_token")
        if not isinstance(resource_token, str):
            raise RuntimeError("resource returned no resource token")
        authorization = await self.coordinator.begin_async(
            resource_token,
            resource_url=provider.mcp_url,
        )
        if isinstance(authorization, ApprovalRequired):
            await approval_handler(authorization)
            await self.coordinator.complete_async(authorization)

        auth = AAuthAgentHTTPAuth(
            key=self.config.signing_key,
            token=self.config.agent_token,
            coordinator=self.coordinator,
            on_approval_required=approval_handler,
        )
        client_options = {
            "auth": auth,
            "follow_redirects": False,
        }
        if self.http_transport is not None:
            client_options["transport"] = self.http_transport
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
                if self.config.function_registry_url:
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

    async def register_edocs_function(
        self,
        function_id: str,
        description: str,
        input_schema: dict[str, Any],
        implementation: dict[str, Any],
    ) -> dict[str, Any]:
        if not self.config.function_registry_url:
            raise RuntimeError("function registry is not configured")
        options = {"follow_redirects": False}
        if self.http_transport is not None:
            options["transport"] = self.http_transport
        async with httpx2.AsyncClient(**options) as client:
            response = await client.post(
                self.config.function_registry_url,
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

    async def list_edocs_functions(
        self,
    ) -> dict[str, list[dict[str, Any]]]:
        if not self.config.function_registry_url:
            raise RuntimeError("function registry is not configured")
        options = {"follow_redirects": False}
        if self.http_transport is not None:
            options["transport"] = self.http_transport
        try:
            async with httpx2.AsyncClient(**options) as client:
                response = await client.get(
                    self.config.function_registry_url,
                )
        except httpx2.HTTPError as error:
            raise RuntimeError("function registry is unavailable") from error
        try:
            body = response.json()
        except ValueError as error:
            raise RuntimeError(
                "function registry returned an invalid response"
            ) from error
        if response.status_code != 200:
            detail = (
                body.get("detail")
                if isinstance(body, dict)
                else "function discovery failed"
            )
            raise ValueError(detail or "function discovery failed")
        if (
            not isinstance(body, dict)
            or set(body) != {"functions"}
            or not isinstance(body["functions"], list)
        ):
            raise RuntimeError("function registry returned an invalid response")
        for function in body["functions"]:
            if (
                not isinstance(function, dict)
                or set(function)
                != {
                    "function_id",
                    "description",
                    "input_schema",
                    "digest",
                }
                or not isinstance(function["function_id"], str)
                or not isinstance(function["description"], str)
                or not isinstance(function["input_schema"], dict)
                or not isinstance(function["digest"], str)
            ):
                raise RuntimeError(
                    "function registry returned an invalid response"
                )
        return body

    async def publish_derived_edoc(
        self,
        derived_edoc_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Expose one possessor-owned derived eDoc on this agent's resource server."""
        if not self.config.sentinel_url:
            raise RuntimeError("sentinel URL is not configured")
        if not self.config.agent_resource_url:
            raise RuntimeError("agent resource URL is not configured")
        if not self.config.agent_id:
            raise RuntimeError("agent id is not configured")
        sentinel = self.config.sentinel_url.rstrip("/")
        options = {"follow_redirects": False}
        if self.http_transport is not None:
            options["transport"] = self.http_transport
        async with httpx2.AsyncClient(**options) as client:
            derived_response = await client.get(
                f"{sentinel}/registry/derived/{derived_edoc_id}"
            )
            if derived_response.status_code == 404:
                raise LookupError(f"unknown derived eDoc: {derived_edoc_id}")
            derived_body = derived_response.json()
            if derived_response.status_code != 200:
                detail = (
                    derived_body.get("detail")
                    if isinstance(derived_body, dict)
                    else "derived eDoc lookup failed"
                )
                raise ValueError(detail or "derived eDoc lookup failed")
            if derived_body.get("possessor") != self.config.agent_id:
                raise PermissionError(
                    "only the possessor agent may publish this derived eDoc"
                )
            if derived_body.get("published"):
                raise ValueError("derived eDoc is already published")
            controllers = derived_body["controllers"]
            output = derived_body["output"]
            if not isinstance(output, dict):
                raise RuntimeError("derived eDoc payload is invalid")
            publish_title = title or f"Published {derived_edoc_id}"
            publish_description = description or (
                "Derived eDoc published by its possessor agent"
            )
            catalog_response = await client.post(
                f"{self.config.agent_resource_url}/admin/documents",
                json={
                    "edoc_id": derived_edoc_id,
                    "title": publish_title,
                    "description": publish_description,
                    "storage": output,
                    "controllers": controllers,
                },
            )
            catalog_body = catalog_response.json()
            if catalog_response.status_code not in {200, 201}:
                detail = (
                    catalog_body.get("detail")
                    if isinstance(catalog_body, dict)
                    else "failed to publish derived eDoc"
                )
                raise ValueError(detail or "failed to publish derived eDoc")
            controller_response = await client.post(
                f"{sentinel}/registry/controllers",
                json={
                    "resource_issuer": self.config.agent_resource_url,
                    "edoc_id": derived_edoc_id,
                    "controllers": controllers,
                },
            )
            controller_body = controller_response.json()
            if controller_response.status_code not in {200, 201}:
                detail = (
                    controller_body.get("detail")
                    if isinstance(controller_body, dict)
                    else "failed to register derived controllers"
                )
                raise ValueError(detail or "failed to register derived controllers")
        self._refresh_providers()
        provider_id = None
        for item in self.providers.values():
            origin = item.mcp_url.rstrip("/").removesuffix("/mcp")
            if origin == self.config.agent_resource_url.rstrip("/"):
                provider_id = item.provider_id
                break
        resource_uri = (
            catalog_body.get("document", {}).get("resource_uri")
            if isinstance(catalog_body, dict)
            else None
        )
        return {
            "derived_edoc_id": derived_edoc_id,
            "provider_id": provider_id,
            "resource_uri": resource_uri,
            "controllers": controllers,
        }


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
        raise ValueError(
            "function_id must be a versioned identifier such as identity@1"
        )
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


def _result_payload(result) -> dict[str, Any]:
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


__all__ = [
    "ApprovalCallback",
    "ApprovalDecision",
    "EdocsGateway",
]
