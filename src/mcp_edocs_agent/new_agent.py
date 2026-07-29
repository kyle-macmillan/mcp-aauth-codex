"""Mint one agent identity and run its Person Server plus resource server.

Complements ``demo.py``'s infra-only ``DemoStack``: this module is the one
place that mints an agent identity (a fixed demo role or an arbitrary new
party) against the Agent Provider key that infra already persisted, runs that
agent's Person Server for consent, and hosts that agent's resource server for
publishing derived eDocs.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any

import requests
from aauth_edocs import (
    Dataflow,
    JwksResolver,
    SigningKey,
    issue_agent_token,
)
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.ps import create_ps
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_edocs_provider import (
    MutableFunctionRegistry,
    ProviderCatalog,
    ProviderResource,
    ProviderServerConfig,
    build_provider_server,
)

from .demo import ASGIService, DemoUrls, FlaskService
from .functions import IDENTITY_FUNCTION_ID, local_demo_function_registrations

ROLE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
KNOWN_ROLE_PORTS = {
    "producer": (8730, 8731),
    "carol": (8732, 8733),
    "bob": (8734, 8735),
}


def agent_service_ports(role: str) -> tuple[int, int]:
    """Return ``(ps_port, resource_port)`` for one agent role."""
    if role in KNOWN_ROLE_PORTS:
        return KNOWN_ROLE_PORTS[role]
    digest = hashlib.sha256(role.encode()).hexdigest()
    base = 8740 + (int(digest[:4], 16) % 80) * 2
    return base, base + 1


def write_agent_credentials(
    *,
    state_dir: Path,
    urls: DemoUrls,
    provider_path: Path,
    role: str,
    agent_id: str,
    person: str,
    agent_key: SigningKey,
    agent_token: str,
    resource_url: str,
    sentinel_url: str,
) -> None:
    agents_dir = state_dir / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = agents_dir / f"{role}.jwk"
    token_path = agents_dir / f"{role}.token"
    env_path = agents_dir / f"{role}.env"
    claude_mcp_path = agents_dir / f"{role}.claude-mcp.json"
    bridge_launcher = Path(__file__).resolve().parents[2] / "scripts" / "run_proxy.sh"

    key_path.write_text(json.dumps(agent_key.private_jwk()))
    token_path.write_text(agent_token)
    env_values = {
        "EDOCS_PROVIDER_FILE": str(provider_path),
        "EDOCS_AGENT_KEY_FILE": str(key_path),
        "EDOCS_AGENT_TOKEN_FILE": str(token_path),
        "EDOCS_PERSON": person,
        "EDOCS_DEMO_AGENT_ID": agent_id,
        "EDOCS_DEMO_AGENT_ROLE": role,
        "EDOCS_CLAUDE_MCP_CONFIG": str(claude_mcp_path),
        "EDOCS_FUNCTION_REGISTRY_URL": f"{sentinel_url}/registry/functions",
        "EDOCS_SENTINEL_URL": sentinel_url,
        "EDOCS_AGENT_RESOURCE_URL": resource_url,
    }
    claude_mcp_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "edocs-aauth": {
                        "type": "stdio",
                        "command": str(bridge_launcher),
                        "args": [],
                        "env": {
                            key: value
                            for key, value in env_values.items()
                            if key
                            in {
                                "EDOCS_PROVIDER_FILE",
                                "EDOCS_AGENT_KEY_FILE",
                                "EDOCS_AGENT_TOKEN_FILE",
                                "EDOCS_PERSON",
                                "EDOCS_FUNCTION_REGISTRY_URL",
                                "EDOCS_SENTINEL_URL",
                                "EDOCS_AGENT_RESOURCE_URL",
                                "EDOCS_DEMO_AGENT_ID",
                            }
                        },
                    }
                }
            },
            indent=2,
        )
    )
    env_path.write_text(
        "".join(f"{key}={value}\n" for key, value in env_values.items())
    )
    for path in (key_path, token_path, env_path, claude_mcp_path):
        os.chmod(path, 0o600)


def append_provider(
    provider_path: Path,
    *,
    provider_id: str,
    display_name: str,
    description: str,
    mcp_url: str,
) -> None:
    provider_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(provider_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read().strip()
        providers: list[dict[str, str]] = json.loads(raw) if raw else []
        if not isinstance(providers, list):
            raise RuntimeError("providers.json must contain a provider array")
        entry = {
            "provider_id": provider_id,
            "display_name": display_name,
            "description": description,
            "mcp_url": mcp_url,
        }
        providers = [
            item
            for item in providers
            if not (
                isinstance(item, dict) and item.get("provider_id") == provider_id
            )
        ]
        providers.append(entry)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(providers, indent=2))
        handle.write("\n")
        os.chmod(provider_path, 0o600)


def _wait_ready(url: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=0.25).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"agent service failed readiness: {url}")


def _register_binding(
    sentinel_url: str,
    *,
    source_agent: str,
    source_ps: str,
    resource_issuer: str,
    resource_jkt: str,
) -> None:
    response = requests.post(
        f"{sentinel_url}/registry/bindings",
        json={
            "source_agent": source_agent,
            "source_ps": source_ps,
            "resource_issuer": resource_issuer,
            "resource_jkt": resource_jkt,
        },
        timeout=5,
    )
    if response.status_code not in {200, 201}:
        detail = response.text
        raise RuntimeError(f"failed to register resource binding: {detail}")


def _build_agent_resource_server(
    *,
    provider_id: str,
    display_name: str,
    resource_url: str,
    sentinel_url: str,
    source_agent: str,
    resource_key: SigningKey,
) -> Any:
    catalog = ProviderCatalog()
    function_registry = MutableFunctionRegistry()
    for function_id, registration in local_demo_function_registrations().items():
        if function_id == IDENTITY_FUNCTION_ID:
            function_registry.register(registration)

    transport = RequestsTransport()
    resolver = JwksResolver(transport)

    def register_tools(mcp: MCPServer, resource: ProviderResource) -> None:
        @mcp.tool()
        def identity(
            provider_id: str,
            edoc_id: str,
            ctx: Context,
        ) -> dict[str, Any]:
            return resource.execute(
                ctx.request_context.request.scope["aauth"],
                provider_id=provider_id,
                edoc_id=edoc_id,
                function_id=IDENTITY_FUNCTION_ID,
                function_args={},
            )

        @mcp.tool()
        def execute_registered_function(
            provider_id: str,
            edoc_id: str,
            function_id: str,
            arguments: dict[str, Any],
            ctx: Context,
        ) -> dict[str, Any]:
            return resource.execute(
                ctx.request_context.request.scope["aauth"],
                provider_id=provider_id,
                edoc_id=edoc_id,
                function_id=function_id,
                function_args=arguments,
            )

    def on_materialized(
        producer: Dataflow,
        output: dict[str, Any],
        controllers: tuple[str, ...],
    ):
        from aauth_edocs import serialize_dataflow

        response = requests.post(
            f"{sentinel_url}/registry/materializations",
            json={
                "producer": serialize_dataflow(producer),
                "output": output,
                "controllers": list(controllers),
            },
            timeout=5,
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"failed to record materialization: {response.text}"
            )
        body = response.json()
        return type(
            "DerivedRef",
            (),
            {"edoc_id": body["derived_edoc_id"]},
        )()

    return build_provider_server(
        ProviderServerConfig(
            provider_id=provider_id,
            display_name=display_name,
            resource_issuer=resource_url,
            sentinel_url=sentinel_url,
            source_agent=source_agent,
            signing_key=resource_key,
            authoritative_controllers=(),
        ),
        catalog=catalog,
        functions=function_registry,
        loader=function_registry,
        key_resolver=resolver,
        register_tools=register_tools,
        on_materialized=on_materialized,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".demo-state"))
    parser.add_argument("--role", required=True)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--person", default=None)
    parser.add_argument("--display-name", default=None)
    args = parser.parse_args()

    if not ROLE_PATTERN.fullmatch(args.role):
        raise SystemExit(f"Invalid agent role: {args.role}")
    role = args.role
    agent_id = args.agent_id or f"aauth:{role}@newparty.local"
    person = args.person or role
    display_name = args.display_name or role.title()

    state_dir = args.state_dir.resolve()
    urls = DemoUrls()
    ap_key_path = state_dir / "keys" / "ap.jwk"
    provider_path = state_dir / "providers.json"
    if not (state_dir / "ready").exists() or not ap_key_path.exists():
        raise SystemExit(
            "Infra isn't running: expected "
            f"{state_dir / 'ready'} and {ap_key_path}. "
            "Start it with scripts/run_infra.sh first."
        )

    ps_port, resource_port = agent_service_ports(role)
    ps_url = f"http://127.0.0.1:{ps_port}"
    resource_url = f"http://127.0.0.1:{resource_port}"

    ap_key = SigningKey.from_private_jwk(json.loads(ap_key_path.read_text()))
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
        provider_path=provider_path,
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

    ps_service = FlaskService(ps, ps_port)
    resource_service = ASGIService(resource_app, resource_port)
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    ps_service.start()
    resource_service.start()
    ready_path = state_dir / "agents" / f"{role}.ready"
    try:
        _wait_ready(f"{ps_url}/.well-known/aauth-person.json")
        _wait_ready(f"{resource_url}/admin/documents")
        _register_binding(
            urls.sentinel,
            source_agent=agent_id,
            source_ps=ps_url,
            resource_issuer=resource_url,
            resource_jkt=resource_key.thumbprint,
        )
        append_provider(
            provider_path,
            provider_id=role,
            display_name=display_name,
            description=f"{display_name}'s published derived eDocs",
            mcp_url=f"{resource_url}/mcp",
        )
        ready_path.write_text("ready\n")
        print(f"{role}: {agent_id} (person: {person})", flush=True)
        print(f"Resource: {resource_url}", flush=True)
        print(f"Control panel: {urls.control}/demo", flush=True)
        stopped.wait()
    finally:
        ready_path.unlink(missing_ok=True)
        resource_service.stop()
        ps_service.stop()


if __name__ == "__main__":
    main()
