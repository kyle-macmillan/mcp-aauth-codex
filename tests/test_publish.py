"""Live tests for explicit derived eDoc publish on a per-agent resource server."""

from __future__ import annotations

import json
import socket

import pytest
import requests
from aauth_edocs import SigningKey, issue_agent_token
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.ps import create_ps
from mcp import Client
from mcp_types import ElicitResult

from mcp_aauth_codex.config import ProxyConfig
from mcp_aauth_codex.demo import (
    BOB_RECIPIENT_AGENT,
    CAROL_RECIPIENT_AGENT,
    DESTINATION_AGENT,
    ASGIService,
    DemoStack,
    DemoUrls,
    FlaskService,
)
from mcp_aauth_codex.proxy import build_server
from mcp_edocs_agent.new_agent import (
    _build_agent_resource_server,
    _register_binding,
    append_provider,
    write_agent_credentials,
)


def _free_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def _demo_urls() -> DemoUrls:
    return DemoUrls(
        ap=_free_url(),
        ps=_free_url(),
        sentinel=_free_url(),
        alice_as=_free_url(),
        alice_resource=_free_url(),
        bob_as=_free_url(),
        bob_resource=_free_url(),
        carol_as=_free_url(),
        carol_resource=_free_url(),
        control=_free_url(),
    )


def _payload(result):
    return json.loads(result.content[0].text)


def _start_agent_with_resource(
    state_dir,
    urls: DemoUrls,
    *,
    role: str,
    agent_id: str,
    person: str,
    display_name: str,
) -> tuple[FlaskService, ASGIService, str]:
    ap_key = SigningKey.from_private_jwk(
        json.loads((state_dir / "keys" / "ap.jwk").read_text())
    )
    ps_url = _free_url()
    resource_url = _free_url()
    agent_key = SigningKey.generate(f"{role}-agent")
    ps_key = SigningKey.generate("ps")
    resource_key = SigningKey.generate(f"{role}-resource")
    transport = RequestsTransport()
    ps = create_ps(
        ps_url,
        key=ps_key,
        person=person,
        policy=lambda _agent, _resource: "pending",
        transport=transport,
    )
    agent_token = issue_agent_token(
        issuer=urls.ap,
        agent=agent_id,
        agent_jwk=agent_key.public_jwk,
        ps=ps_url,
        key=ap_key,
    )
    write_agent_credentials(
        state_dir=state_dir,
        urls=urls,
        provider_path=state_dir / "providers.json",
        role=role,
        agent_id=agent_id,
        person=person,
        agent_key=agent_key,
        agent_token=agent_token,
        resource_url=resource_url,
        sentinel_url=urls.sentinel,
    )
    resource_app = _build_agent_resource_server(
        provider_id=role,
        display_name=display_name,
        resource_url=resource_url,
        sentinel_url=urls.sentinel,
        source_agent=agent_id,
        resource_key=resource_key,
    )
    ps_service = FlaskService(ps, int(ps_url.rsplit(":", 1)[-1]))
    resource_service = ASGIService(resource_app, int(resource_url.rsplit(":", 1)[-1]))
    ps_service.start()
    resource_service.start()
    for _ in range(100):
        try:
            if (
                requests.get(f"{ps_url}/.well-known/aauth-person.json", timeout=0.25).status_code
                == 200
                and requests.get(f"{resource_url}/admin/documents", timeout=0.25).status_code
                == 200
            ):
                break
        except requests.RequestException:
            pass
    else:
        raise RuntimeError(f"agent {role} failed readiness")
    _register_binding(
        urls.sentinel,
        source_agent=agent_id,
        source_ps=ps_url,
        resource_issuer=resource_url,
        resource_jkt=resource_key.thumbprint,
    )
    append_provider(
        state_dir / "providers.json",
        provider_id=role,
        display_name=display_name,
        description=f"{display_name}'s published derived eDocs",
        mcp_url=f"{resource_url}/mcp",
    )
    return ps_service, resource_service, resource_url


@pytest.mark.asyncio
async def test_explicit_publish_allows_carol_denies_bob(monkeypatch, tmp_path):
    state_dir = tmp_path / "publish-state"
    stack = DemoStack(state_dir, _demo_urls())
    services: list = []
    try:
        stack.start()
        producer_ps, producer_rs, producer_resource = _start_agent_with_resource(
            state_dir,
            stack.urls,
            role="producer",
            agent_id=DESTINATION_AGENT,
            person="alice",
            display_name="Producer",
        )
        services.extend([producer_ps, producer_rs])
        carol_ps, carol_rs, _carol_resource = _start_agent_with_resource(
            state_dir,
            stack.urls,
            role="carol",
            agent_id=CAROL_RECIPIENT_AGENT,
            person="carol",
            display_name="Carol",
        )
        services.extend([carol_ps, carol_rs])
        bob_ps, bob_rs, _bob_resource = _start_agent_with_resource(
            state_dir,
            stack.urls,
            role="bob",
            agent_id=BOB_RECIPIENT_AGENT,
            person="bob",
            display_name="Bob",
        )
        services.extend([bob_ps, bob_rs])

        async def approve(_context, _params):
            return ElicitResult(action="accept", content={"approve": True})

        for line in (state_dir / "agents" / "producer.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        producer_proxy = build_server(ProxyConfig.from_env())

        async with Client(
            producer_proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            providers = await client.call_tool("list_providers", {})
            provider_refs = {
                item["provider_id"]: item["provider_ref"]
                for item in _payload(providers)["providers"]
            }
            assert "producer" in provider_refs
            alice_resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["alice"]},
            )
            alice_ref = _payload(alice_resources)["resources"][0]["resource_ref"]
            producer_resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["producer"]},
            )
            assert _payload(producer_resources)["resources"] == []
            invoked = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": alice_ref,
                    "function_id": "query_table@1",
                    "arguments": {
                        "statement": (
                            "SELECT name, department FROM document "
                            "WHERE department = ? ORDER BY name"
                        ),
                        "parameters": ["engineering"],
                    },
                },
            )
            assert invoked.is_error is False
            invoke_payload = _payload(invoked)["structured_content"]
            derived_edoc_id = invoke_payload["derived_edoc_id"]
            assert derived_edoc_id.startswith("derived_")

            unpublished = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["producer"]},
            )
            assert _payload(unpublished)["resources"] == []

            published = await client.call_tool(
                "publish_derived_edoc",
                {"derived_edoc_id": derived_edoc_id},
            )
            assert published.is_error is False
            publish_payload = _payload(published)
            assert publish_payload["derived_edoc_id"] == derived_edoc_id
            assert publish_payload["provider_id"] == "producer"
            assert publish_payload["controllers"] == [stack.urls.alice_as]

            listed = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["producer"]},
            )
            resources = _payload(listed)["resources"]
            assert len(resources) == 1
            assert resources[0]["uri"] == f"edoc://producer/{derived_edoc_id}"

        assert stack.registry.controllers[
            (producer_resource, derived_edoc_id)
        ] == (stack.urls.alice_as,)

        for line in (state_dir / "agents" / "carol.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        carol_proxy = build_server(ProxyConfig.from_env())
        async with Client(
            carol_proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            providers = await client.call_tool("list_providers", {})
            provider_refs = {
                item["provider_id"]: item["provider_ref"]
                for item in _payload(providers)["providers"]
            }
            resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["producer"]},
            )
            resource_ref = _payload(resources)["resources"][0]["resource_ref"]
            allowed = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_ref,
                    "function_id": "identity@1",
                    "arguments": {},
                },
            )
            assert allowed.is_error is False
            shared = _payload(allowed)["structured_content"]
            assert shared["rows"] == [
                {"name": "Avery", "department": "engineering"},
                {"name": "Casey", "department": "engineering"},
            ]

        for line in (state_dir / "agents" / "bob.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        bob_proxy = build_server(ProxyConfig.from_env())
        async with Client(
            bob_proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            providers = await client.call_tool("list_providers", {})
            provider_refs = {
                item["provider_id"]: item["provider_ref"]
                for item in _payload(providers)["providers"]
            }
            resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["producer"]},
            )
            resource_ref = _payload(resources)["resources"][0]["resource_ref"]
            denied = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_ref,
                    "function_id": "identity@1",
                    "arguments": {},
                },
            )
            assert denied.is_error is True
    finally:
        for service in reversed(services):
            service.stop()
        stack.stop()


@pytest.mark.asyncio
async def test_publish_rejects_non_possessor(monkeypatch, tmp_path):
    state_dir = tmp_path / "publish-deny-state"
    stack = DemoStack(state_dir, _demo_urls())
    services: list = []
    try:
        stack.start()
        producer_ps, producer_rs, _ = _start_agent_with_resource(
            state_dir,
            stack.urls,
            role="producer",
            agent_id=DESTINATION_AGENT,
            person="alice",
            display_name="Producer",
        )
        services.extend([producer_ps, producer_rs])
        carol_ps, carol_rs, _ = _start_agent_with_resource(
            state_dir,
            stack.urls,
            role="carol",
            agent_id=CAROL_RECIPIENT_AGENT,
            person="carol",
            display_name="Carol",
        )
        services.extend([carol_ps, carol_rs])

        materialize = requests.post(
            f"{stack.urls.sentinel}/registry/materializations",
            json={
                "dataflow": {
                    "source": "aauth:source@alice.demo.local",
                    "function": "query_table@1",
                    "document": "doc_01JDEMO7F3A",
                    "destination": DESTINATION_AGENT,
                    "function_args": {
                        "statement": "SELECT 1",
                        "parameters": [],
                    },
                },
                "output": {"columns": ["n"], "rows": [{"n": 1}], "truncated": False},
                "controllers": [stack.urls.alice_as],
            },
            timeout=2,
        )
        assert materialize.status_code == 201
        derived_edoc_id = materialize.json()["derived_edoc_id"]

        for line in (state_dir / "agents" / "carol.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        carol_proxy = build_server(ProxyConfig.from_env())
        async with Client(carol_proxy, mode="legacy") as client:
            denied = await client.call_tool(
                "publish_derived_edoc",
                {"derived_edoc_id": derived_edoc_id},
            )
            assert denied.is_error is True
            assert "possessor" in denied.content[0].text.lower()
    finally:
        for service in reversed(services):
            service.stop()
        stack.stop()
