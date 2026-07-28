"""Runnable localhost composition for the coding-agent eDocs demo."""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import requests
import uvicorn
from aauth_edocs import (
    AAuthError,
    Dataflow,
    ExactRule,
    MutableControllerPolicy,
    OutputOf,
    ResourceBinding,
    SentinelRegistry,
    SigningKey,
    create_sentinel,
    issue_agent_token,
    register_materialization,
)
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.asrv import create_as
from aauth_edocs.errors import INVALID_TOKEN
from aauth_edocs.httpsig import KeyResolver
from aauth_edocs.ps import create_ps
from flask import Flask
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_edocs_provider import (
    CatalogEntry as ProviderCatalogEntry,
    LoadedFunction,
    ProviderApplication,
    ProviderCatalog,
    ProviderResource,
    ProviderServerConfig,
    MutableFunctionRegistry,
    build_provider_server,
)
from starlette.types import ASGIApp
from werkzeug.serving import BaseWSGIServer, WSGIRequestHandler, make_server

from .demo_database import (
    CatalogEntry as DemoCatalogEntry,
    DEMO_EDOC_ID,
    load_demo_catalog,
    setup_demo_database,
)
from .control_panel import DemoProviderAdmin, create_control_panel
from .functions import (
    DEMO_FUNCTION_SQL,
    IDENTITY_FUNCTION_ID,
    local_demo_function_registrations,
    sql_function_registration,
)

QUERY_FUNCTION_ID = "query_table@1"
PERSON = "alice"
ALICE_SOURCE_AGENT = "aauth:source@alice.demo.local"
BOB_SOURCE_AGENT = "aauth:source@bob.demo.local"
CAROL_SOURCE_AGENT = "aauth:source@carol.demo.local"
PRODUCER_AGENT = "aauth:producer@demo.local"
# Compatibility name retained for existing integrations and tests.
DESTINATION_AGENT = PRODUCER_AGENT
CAROL_RECIPIENT_AGENT = "aauth:carol@demo.local"
BOB_RECIPIENT_AGENT = "aauth:bob@demo.local"
DEMO_AGENTS = {
    "producer": PRODUCER_AGENT,
    "carol": CAROL_RECIPIENT_AGENT,
    "bob": BOB_RECIPIENT_AGENT,
}


@dataclass(frozen=True)
class DemoProviderSpec:
    provider_id: str
    display_name: str
    description: str
    resource_url: str
    access_server_url: str
    source_agent: str


@dataclass(frozen=True)
class DemoUrls:
    ap: str = "http://127.0.0.1:8711"
    ps: str = "http://127.0.0.1:8712"
    sentinel: str = "http://127.0.0.1:8713"
    alice_as: str = "http://127.0.0.1:8714"
    alice_resource: str = "http://127.0.0.1:8716"
    bob_as: str = "http://127.0.0.1:8717"
    bob_resource: str = "http://127.0.0.1:8718"
    carol_as: str = "http://127.0.0.1:8719"
    carol_resource: str = "http://127.0.0.1:8720"
    control: str = "http://127.0.0.1:8721"

    @property
    def alice_mcp(self) -> str:
        return f"{self.alice_resource}/mcp"

    @property
    def bob_mcp(self) -> str:
        return f"{self.bob_resource}/mcp"

    @property
    def carol_mcp(self) -> str:
        return f"{self.carol_resource}/mcp"

    def provider_specs(self) -> tuple[DemoProviderSpec, ...]:
        return (
            DemoProviderSpec(
                "alice",
                "Alice",
                "Alice's governed eDocs",
                self.alice_resource,
                self.alice_as,
                ALICE_SOURCE_AGENT,
            ),
            DemoProviderSpec(
                "bob",
                "Bob",
                "Bob's governed eDocs",
                self.bob_resource,
                self.bob_as,
                BOB_SOURCE_AGENT,
            ),
            DemoProviderSpec(
                "carol",
                "Carol",
                "Carol's governed eDocs",
                self.carol_resource,
                self.carol_as,
                CAROL_SOURCE_AGENT,
            ),
        )


@dataclass(frozen=True)
class ProviderDeployment:
    provider_id: str
    display_name: str
    description: str
    resource_url: str
    access_server_url: str
    source_agent: str
    resource_key: SigningKey
    access_server_key: SigningKey
    catalog: ProviderCatalog

    @property
    def mcp_url(self) -> str:
        return f"{self.resource_url}/mcp"


class QuietRequestHandler(WSGIRequestHandler):
    """Suppress routine localhost access lines without hiding server errors."""

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        pass


class FlaskService:
    def __init__(self, app: Flask, port: int) -> None:
        self.server: BaseWSGIServer = make_server(
            "127.0.0.1",
            port,
            app,
            threaded=True,
            request_handler=QuietRequestHandler,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)


class ASGIService:
    def __init__(self, app: ASGIApp, port: int) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()),
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


class DemoStack:
    def __init__(self, state_dir: Path, urls: DemoUrls = DemoUrls()) -> None:
        self.state_dir = state_dir
        self.urls = urls
        self.services: list[FlaskService | ASGIService] = []
        self.registry: SentinelRegistry | None = None
        self.policies: dict[str, MutableControllerPolicy] = {}
        self.catalogs: dict[str, ProviderCatalog] = {}
        self.function_registry = MutableFunctionRegistry()
        self._build()

    def _build(self) -> None:
        urls = self.urls
        provider_specs = urls.provider_specs()
        keys = {
            name: SigningKey.generate(name)
            for name in (
                "ap",
                "ps",
                "sentinel",
                *(f"{role}-agent" for role in DEMO_AGENTS),
                *(
                    key_name
                    for spec in provider_specs
                    for key_name in (
                        f"{spec.provider_id}-as",
                        f"{spec.provider_id}-resource",
                    )
                ),
            )
        }
        seeded_functions = local_demo_function_registrations()
        self.function_registry = MutableFunctionRegistry()
        for function_id, registration in seeded_functions.items():
            self.function_registry.register(
                registration,
                artifact={
                    "runtime": "sql",
                    "source": (
                        DEMO_FUNCTION_SQL[function_id]
                        if function_id in DEMO_FUNCTION_SQL
                        else (
                            "identity(input)"
                            if function_id == IDENTITY_FUNCTION_ID
                            else "Provided by the caller in function arguments"
                        )
                    ),
                },
            )
        deployments = tuple(
            self._deployment(
                provider_id=spec.provider_id,
                display_name=spec.display_name,
                description=spec.description,
                resource_url=spec.resource_url,
                access_server_url=spec.access_server_url,
                source_agent=spec.source_agent,
                resource_key=keys[f"{spec.provider_id}-resource"],
                access_server_key=keys[f"{spec.provider_id}-as"],
            )
            for spec in provider_specs
        )
        query_arguments = {
            "statement": (
                "SELECT name, department FROM document "
                "WHERE department = ? ORDER BY name"
            ),
            "parameters": ["engineering"],
        }
        self.registry = SentinelRegistry(
            resource_bindings={
                deployment.source_agent: ResourceBinding(
                    source_ps=urls.ps,
                    resource_issuer=deployment.resource_url,
                    resource_jkt=deployment.resource_key.thumbprint,
                )
                for deployment in deployments
            },
            controllers={
                (deployment.resource_url, DEMO_EDOC_ID): (
                    deployment.access_server_url,
                )
                for deployment in deployments
            },
            functions={
                function_id: registration.descriptor
                for function_id, registration in self.function_registry.items()
            },
        )
        transport = RequestsTransport()
        ap = _metadata_app(urls.ap, "aauth-agent.json", keys["ap"])
        query_flows = {
            deployment.provider_id: Dataflow.from_arguments(
                deployment.source_agent,
                QUERY_FUNCTION_ID,
                DEMO_EDOC_ID,
                PRODUCER_AGENT,
                query_arguments,
            )
            for deployment in deployments
        }
        self.policies = {
            deployment.provider_id: MutableControllerPolicy(
                (ExactRule(query_flows[deployment.provider_id]),),
                derived_resolver=self.registry.derived_documents.get,
            )
            for deployment in deployments
        }
        self.policies["alice"].create_rule(
            Dataflow.from_arguments(
                PRODUCER_AGENT,
                IDENTITY_FUNCTION_ID,
                OutputOf(query_flows["alice"]),
                CAROL_RECIPIENT_AGENT,
                {},
            )
        )
        self.catalogs = {
            deployment.provider_id: deployment.catalog
            for deployment in deployments
        }
        access_servers = [
            create_as(
                deployment.access_server_url,
                key=deployment.access_server_key,
                transport=transport,
                sentinel=urls.sentinel,
                controller_policy=self.policies[deployment.provider_id],
            )
            for deployment in deployments
        ]
        sentinel = create_sentinel(
            issuer=urls.sentinel,
            registry=self.registry,
            key=keys["sentinel"],
            transport=transport,
        )
        ps = create_ps(
            urls.ps,
            key=keys["ps"],
            person=PERSON,
            policy=lambda _agent, _resource: "pending",
            transport=transport,
        )
        agent_tokens = {
            role: issue_agent_token(
                issuer=urls.ap,
                agent=agent,
                agent_jwk=keys[f"{role}-agent"].public_jwk,
                ps=urls.ps,
                key=keys["ap"],
            )
            for role, agent in DEMO_AGENTS.items()
        }
        resolver = _static_and_remote_resolver(
            {
                urls.ap: keys["ap"].public_jwk,
                urls.sentinel: keys["sentinel"].public_jwk,
                **{
                    deployment.access_server_url: (
                        deployment.access_server_key.public_jwk
                    )
                    for deployment in deployments
                },
            }
        )
        resource_apps = [
            self._provider_application(
                deployment,
                resolver=resolver,
                function_registry=self.function_registry,
            )
            for deployment in deployments
        ]
        control_panel = create_control_panel(
            {
                deployment.provider_id: DemoProviderAdmin(
                    provider_id=deployment.provider_id,
                    display_name=deployment.display_name,
                    catalog=deployment.catalog,
                    policy=self.policies[deployment.provider_id],
                    add_document=self._document_adder(deployment),
                    source_agent=deployment.source_agent,
                    destination_agent=PRODUCER_AGENT,
                )
                for deployment in deployments
            },
            sentinel=self.registry,
            function_registry=self.function_registry,
            register_function=self._function_registrar(),
            agents=DEMO_AGENTS,
        )
        self.services = [
            FlaskService(ap, _port(urls.ap)),
            FlaskService(ps, _port(urls.ps)),
            FlaskService(sentinel, _port(urls.sentinel)),
            *[
                FlaskService(access_server, _port(deployment.access_server_url))
                for access_server, deployment in zip(
                    access_servers,
                    deployments,
                    strict=True,
                )
            ],
            *[
                ASGIService(resource_app, _port(deployment.resource_url))
                for resource_app, deployment in zip(
                    resource_apps,
                    deployments,
                    strict=True,
                )
            ],
            FlaskService(control_panel, _port(urls.control)),
        ]
        self._write_state(
            {
                role: keys[f"{role}-agent"]
                for role in DEMO_AGENTS
            },
            agent_tokens,
            deployments,
        )

    def _deployment(
        self,
        *,
        provider_id: str,
        display_name: str,
        description: str,
        resource_url: str,
        access_server_url: str,
        source_agent: str,
        resource_key: SigningKey,
        access_server_key: SigningKey,
    ) -> ProviderDeployment:
        state_dir = self.state_dir / provider_id
        documents = (
            load_demo_catalog(state_dir)
            if (state_dir / "catalog.json").exists()
            else setup_demo_database(state_dir, provider_id=provider_id)
        )
        catalog = ProviderCatalog(
            tuple(
                ProviderCatalogEntry(
                    edoc_id=document.edoc_id,
                    resource_uri=document.resource_uri,
                    title=document.title,
                    description=document.description,
                    enabled=True,
                    storage=document,
                )
                for document in documents.values()
            )
        )
        return ProviderDeployment(
            provider_id=provider_id,
            display_name=display_name,
            description=description,
            resource_url=resource_url,
            access_server_url=access_server_url,
            source_agent=source_agent,
            resource_key=resource_key,
            access_server_key=access_server_key,
            catalog=catalog,
        )

    def _provider_application(
        self,
        deployment: ProviderDeployment,
        *,
        resolver: KeyResolver,
        function_registry: MutableFunctionRegistry,
    ) -> ProviderApplication:
        def register_tools(
            mcp: MCPServer,
            resource: ProviderResource,
        ) -> None:
            @mcp.tool()
            def query_table(
                provider_id: str,
                edoc_id: str,
                statement: str,
                parameters: list[Any],
                ctx: Context,
            ) -> dict[str, Any]:
                return resource.execute(
                    ctx.request_context.request.scope["aauth"],
                    provider_id=provider_id,
                    edoc_id=edoc_id,
                    function_id=QUERY_FUNCTION_ID,
                    function_args={
                        "statement": statement,
                        "parameters": parameters,
                    },
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

            @mcp.tool()
            def department_counts(
                provider_id: str,
                edoc_id: str,
                ctx: Context,
            ) -> dict[str, Any]:
                return resource.execute(
                    ctx.request_context.request.scope["aauth"],
                    provider_id=provider_id,
                    edoc_id=edoc_id,
                    function_id="department_counts@1",
                    function_args={},
                )

            @mcp.tool()
            def average_salary_by_department(
                provider_id: str,
                edoc_id: str,
                ctx: Context,
            ) -> dict[str, Any]:
                return resource.execute(
                    ctx.request_context.request.scope["aauth"],
                    provider_id=provider_id,
                    edoc_id=edoc_id,
                    function_id="average_salary_by_department@1",
                    function_args={},
                )

            @mcp.tool()
            def employee_count(
                provider_id: str,
                edoc_id: str,
                ctx: Context,
            ) -> dict[str, Any]:
                return resource.execute(
                    ctx.request_context.request.scope["aauth"],
                    provider_id=provider_id,
                    edoc_id=edoc_id,
                    function_id="employee_count@1",
                    function_args={},
                )

        return build_provider_server(
            ProviderServerConfig(
                provider_id=deployment.provider_id,
                display_name=deployment.display_name,
                resource_issuer=deployment.resource_url,
                sentinel_url=self.urls.sentinel,
                source_agent=deployment.source_agent,
                signing_key=deployment.resource_key,
                authoritative_controllers=(deployment.access_server_url,),
            ),
            catalog=deployment.catalog,
            functions=function_registry,
            loader=function_registry,
            key_resolver=resolver,
            register_tools=register_tools,
            on_materialized=self._record_materialization,
        )

    def _record_materialization(
        self,
        producer: Dataflow,
        output: dict[str, Any],
        controllers: tuple[str, ...],
    ) -> None:
        assert self.registry is not None
        register_materialization(
            self.registry,
            producer=producer,
            output=output,
            controllers=controllers,
        )

    def _function_registrar(self):
        def register(body: dict[str, Any]) -> LoadedFunction:
            if set(body) != {
                "function_id",
                "description",
                "input_schema",
                "implementation",
            }:
                raise ValueError(
                    "function requires function_id, description, "
                    "input_schema, and implementation"
                )
            implementation = body["implementation"]
            if (
                not isinstance(implementation, dict)
                or set(implementation) != {"runtime", "source"}
                or implementation["runtime"] != "sql"
            ):
                raise ValueError(
                    "the demo supports implementation runtime 'sql'"
                )
            registration = sql_function_registration(
                function_id=body["function_id"],
                description=body["description"],
                sql=implementation["source"],
                input_schema=body["input_schema"],
            )
            self.function_registry.register(
                registration,
                artifact=implementation,
            )
            assert self.registry is not None
            self.registry.functions[
                registration.descriptor.id
            ] = registration.descriptor
            return registration

        return register

    def _document_adder(
        self,
        deployment: ProviderDeployment,
    ):
        def add_document(body: dict[str, Any]) -> ProviderCatalogEntry:
            if set(body) != {"title", "description", "csv"}:
                raise ValueError(
                    "file requires title, description, and csv fields"
                )
            title = body["title"]
            description = body["description"]
            csv_text = body["csv"]
            if not isinstance(title, str) or not title.strip():
                raise ValueError("title must be a non-empty string")
            if not isinstance(description, str):
                raise ValueError("description must be a string")
            if not isinstance(csv_text, str) or not csv_text.strip():
                raise ValueError("csv must be a non-empty string")
            reader = csv.reader(io.StringIO(csv_text))
            try:
                header = next(reader)
            except StopIteration as error:
                raise ValueError("CSV must contain a header") from error
            if not header or any(not column.strip() for column in header):
                raise ValueError("CSV header names must be non-empty")
            if len(set(header)) != len(header):
                raise ValueError("CSV header names must be unique")
            if not any(True for _ in reader):
                raise ValueError("CSV must contain at least one data row")

            edoc_id = f"doc_{uuid4().hex}"
            resources_dir = self.state_dir / deployment.provider_id / "resources"
            resources_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            csv_path = resources_dir / f"{edoc_id}.csv"
            database_path = resources_dir / f"{edoc_id}.duckdb"
            csv_path.write_text(csv_text)
            connection = duckdb.connect(str(database_path))
            try:
                connection.execute(
                    "CREATE TABLE document AS SELECT * FROM read_csv_auto(?)",
                    [str(csv_path)],
                )
            except Exception:
                database_path.unlink(missing_ok=True)
                raise
            finally:
                connection.close()
            storage = DemoCatalogEntry(
                edoc_id=edoc_id,
                resource_uri=f"edoc://{deployment.provider_id}/{edoc_id}",
                title=title.strip(),
                description=description,
                original_filename=f"{edoc_id}.csv",
                database_path=str(database_path),
            )
            return deployment.catalog.add(
                ProviderCatalogEntry(
                    edoc_id=storage.edoc_id,
                    resource_uri=storage.resource_uri,
                    title=storage.title,
                    description=storage.description,
                    enabled=True,
                    storage=storage,
                )
            )

        return add_document

    def _write_state(
        self,
        agent_keys: dict[str, SigningKey],
        agent_tokens: dict[str, str],
        deployments: tuple[ProviderDeployment, ...],
    ) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        agents_dir = self.state_dir / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        provider_path = self.state_dir / "providers.json"
        provider_path.write_text(
            json.dumps(
                [
                    {
                        "provider_id": deployment.provider_id,
                        "display_name": deployment.display_name,
                        "description": deployment.description,
                        "mcp_url": deployment.mcp_url,
                    }
                    for deployment in deployments
                ],
                indent=2,
            )
        )
        bridge_launcher = (
            Path(__file__).resolve().parents[2] / "scripts" / "run_proxy.sh"
        )
        agent_paths: list[Path] = []
        for role, agent_id in DEMO_AGENTS.items():
            key_path = agents_dir / f"{role}.jwk"
            token_path = agents_dir / f"{role}.token"
            env_path = agents_dir / f"{role}.env"
            claude_mcp_path = agents_dir / f"{role}.claude-mcp.json"
            key_path.write_text(
                json.dumps(agent_keys[role].private_jwk())
            )
            token_path.write_text(agent_tokens[role])
            claude_mcp_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "edocs-aauth": {
                                "type": "stdio",
                                "command": str(bridge_launcher),
                                "args": [],
                                "env": {
                                    "EDOCS_PROVIDER_FILE": str(provider_path),
                                    "EDOCS_AGENT_KEY_FILE": str(key_path),
                                    "EDOCS_AGENT_TOKEN_FILE": str(token_path),
                                    "EDOCS_PERSON": PERSON,
                                    "EDOCS_FUNCTION_REGISTRY_URL": (
                                        f"{self.urls.control}"
                                        "/api/sentinel/functions"
                                    ),
                                },
                            }
                        }
                    },
                    indent=2,
                )
            )
            env_path.write_text(
                f"EDOCS_PROVIDER_FILE={provider_path}\n"
                f"EDOCS_AGENT_KEY_FILE={key_path}\n"
                f"EDOCS_AGENT_TOKEN_FILE={token_path}\n"
                f"EDOCS_PERSON={PERSON}\n"
                f"EDOCS_DEMO_AGENT_ID={agent_id}\n"
                f"EDOCS_DEMO_AGENT_ROLE={role}\n"
                f"EDOCS_CLAUDE_MCP_CONFIG={claude_mcp_path}\n"
                "EDOCS_FUNCTION_REGISTRY_URL="
                f"{self.urls.control}/api/sentinel/functions\n"
            )
            agent_paths.extend(
                (key_path, token_path, env_path, claude_mcp_path)
            )

        producer_key = self.state_dir / "agent.jwk"
        producer_token = self.state_dir / "agent.token"
        legacy_env = self.state_dir / "demo.env"
        producer_key.write_text(
            json.dumps(agent_keys["producer"].private_jwk())
        )
        producer_token.write_text(agent_tokens["producer"])
        legacy_env.write_text(
            (agents_dir / "producer.env").read_text()
            .replace(str(agents_dir / "producer.jwk"), str(producer_key))
            .replace(
                str(agents_dir / "producer.token"),
                str(producer_token),
            )
        )
        for path in (
            producer_key,
            producer_token,
            provider_path,
            legacy_env,
            *agent_paths,
        ):
            os.chmod(path, 0o600)

    def start(self) -> None:
        started = []
        try:
            for service in self.services:
                service.start()
                started.append(service)
            self.wait_ready()
            (self.state_dir / "ready").write_text("ready\n")
        except BaseException:
            for service in reversed(started):
                service.stop()
            raise

    def wait_ready(self, timeout: float = 10) -> None:
        urls = [
            f"{self.urls.ap}/.well-known/aauth-agent.json",
            f"{self.urls.ps}/.well-known/aauth-person.json",
            f"{self.urls.sentinel}/.well-known/aauth-access.json",
            f"{self.urls.alice_as}/.well-known/aauth-access.json",
            f"{self.urls.bob_as}/.well-known/aauth-access.json",
            f"{self.urls.carol_as}/.well-known/aauth-access.json",
            f"{self.urls.alice_resource}/admin/documents",
            f"{self.urls.bob_resource}/admin/documents",
            f"{self.urls.carol_resource}/admin/documents",
            f"{self.urls.control}/api/providers",
        ]
        deadline = time.monotonic() + timeout
        pending = set(urls)
        while pending and time.monotonic() < deadline:
            for url in tuple(pending):
                try:
                    if requests.get(url, timeout=0.25).status_code == 200:
                        pending.remove(url)
                except requests.RequestException:
                    pass
            if pending:
                time.sleep(0.05)
        if pending:
            raise RuntimeError(
                "demo services failed readiness: " + ", ".join(sorted(pending))
            )

    def stop(self) -> None:
        (self.state_dir / "ready").unlink(missing_ok=True)
        for service in reversed(self.services):
            service.stop()


def _metadata_app(issuer: str, dwk: str, key: SigningKey) -> Flask:
    from aauth_edocs import build_metadata

    app = Flask(issuer)

    @app.get(f"/.well-known/{dwk}")
    def metadata():
        return dict(build_metadata(issuer, jwks_uri=f"{issuer}/jwks.json"))

    @app.get("/jwks.json")
    def jwks():
        return {"keys": [key.public_jwk]}

    return app


def _static_and_remote_resolver(keys: dict[str, dict]) -> KeyResolver:
    def resolve(issuer: str, _dwk: str, kid: str):
        key = keys.get(issuer)
        if key is None or key.get("kid") != kid:
            raise AAuthError(INVALID_TOKEN, 401, "unknown signing key")
        return key

    return resolve


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[-1])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".demo-state"),
    )
    args = parser.parse_args()
    stack = DemoStack(args.state_dir.resolve())
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    stack.start()
    print(
        "eDocs demo ready: "
        f"Alice {stack.urls.alice_mcp}, Bob {stack.urls.bob_mcp}; "
        f"controls {stack.urls.control}/demo",
        flush=True,
    )
    try:
        stopped.wait()
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
