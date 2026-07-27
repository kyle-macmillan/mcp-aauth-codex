"""Runnable localhost composition for the Codex eDocs demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import uvicorn
from aauth_edocs import (
    AAuthError,
    ControllerPolicy,
    Dataflow,
    ExactRule,
    FunctionDescriptor,
    ResourceBinding,
    SentinelRegistry,
    SigningKey,
    VerifiedRequest,
    AGENT_TYP,
    build_metadata,
    build_requirement,
    create_sentinel,
    issue_agent_token,
    issue_resource_token,
    peek_jwt,
)
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.asrv import create_as
from aauth_edocs.errors import DENIED, INVALID_TOKEN
from aauth_edocs.headers import AUTH_TOKEN
from aauth_edocs.httpsig import KeyResolver
from aauth_edocs.keys import jwk_thumbprint
from aauth_edocs.ps import create_ps
from flask import Flask
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp_aauth import aauth_agent_authentication, aauth_authorization
from starlette.types import ASGIApp, Receive, Scope, Send
from werkzeug.serving import BaseWSGIServer, make_server

FUNCTION_ID = "identity@1"
EDOC_ID = "doc-123"
PERSON = "alice"
SOURCE_AGENT = "aauth:source@demo.local"
DESTINATION_AGENT = "aauth:codex@demo.local"


@dataclass(frozen=True)
class DemoUrls:
    ap: str = "http://127.0.0.1:8711"
    ps: str = "http://127.0.0.1:8712"
    sentinel: str = "http://127.0.0.1:8713"
    controller_a: str = "http://127.0.0.1:8714"
    controller_b: str = "http://127.0.0.1:8715"
    resource: str = "http://127.0.0.1:8716"

    @property
    def mcp(self) -> str:
        return f"{self.resource}/mcp"


@dataclass
class DemoResource:
    issuer: str
    sentinel: str
    source_agent: str
    destination_agent: str
    key: SigningKey
    controllers: tuple[str, ...]
    documents: dict[str, Any]

    def challenge(self, verified_agent: VerifiedRequest) -> str:
        agent = verified_agent.claims.get("sub")
        agent_jwk = (verified_agent.claims.get("cnf") or {}).get("jwk")
        if not isinstance(agent, str) or not isinstance(agent_jwk, dict):
            raise AAuthError(INVALID_TOKEN, 401, "agent identity is incomplete")
        return issue_resource_token(
            issuer=self.issuer,
            aud=self.sentinel,
            agent=agent,
            agent_jkt=jwk_thumbprint(agent_jwk),
            scope=FUNCTION_ID,
            source_agent=self.source_agent,
            edoc_id=EDOC_ID,
            controllers=self.controllers,
            key=self.key,
        )

    def identity(self, authorization: VerifiedRequest, edoc_id: str) -> str:
        expected = {
            "iss": self.sentinel,
            "aud": self.issuer,
            "source_agent": self.source_agent,
            "scope": FUNCTION_ID,
            "edoc_id": edoc_id,
            "agent": self.destination_agent,
            "controllers": list(self.controllers),
        }
        for name, value in expected.items():
            if authorization.claims.get(name) != value:
                raise AAuthError(
                    INVALID_TOKEN,
                    401,
                    f"authorization {name} does not match the invocation",
                )
        try:
            return self.documents[edoc_id]["message"]
        except KeyError as error:
            raise AAuthError(DENIED, 403, "eDoc does not exist") from error


class ChallengeApplication:
    """Issue a resource challenge before forwarding to authorized MCP."""

    def __init__(
        self,
        resource: DemoResource,
        downstream: ASGIApp,
        *,
        key_resolver: KeyResolver,
    ) -> None:
        self.resource = resource
        self.downstream = downstream
        self.challenge_app = aauth_agent_authentication(
            key_resolver=key_resolver
        )(self._challenge)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") in {
            "/.well-known/aauth-resource.json",
            "/jwks.json",
        }:
            value = (
                dict(
                    build_metadata(
                        self.resource.issuer,
                        jwks_uri=f"{self.resource.issuer}/jwks.json",
                    )
                )
                if scope["path"] == "/.well-known/aauth-resource.json"
                else {"keys": [self.resource.key.public_jwk]}
            )
            await _send_json(send, 200, value)
            return
        if (
            scope["type"] == "http"
            and scope.get("path") == "/mcp"
            and _presented_token_type(scope) == AGENT_TYP
        ):
            await self.challenge_app(scope, receive, send)
            return
        await self.downstream(scope, receive, send)

    async def _challenge(
        self,
        scope: Scope,
        _receive: Receive,
        send: Send,
    ) -> None:
        token = self.resource.challenge(scope["aauth"])
        body = json.dumps(
            {
                "error": INVALID_TOKEN,
                "error_description": "authorization token required",
            }
        ).encode()
        requirement = build_requirement(AUTH_TOKEN, resource_token=token)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"aauth-requirement", requirement.encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


async def _send_json(send: Send, status: int, value: dict) -> None:
    body = json.dumps(value, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _presented_token_type(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() != b"signature-key":
            continue
        signature_key = value.decode("latin-1")
        marker = 'jwt="'
        if marker not in signature_key:
            return None
        token = signature_key.split(marker, 1)[1].split('"', 1)[0]
        try:
            return peek_jwt(token)[0].get("typ")
        except AAuthError:
            return None
    return None


class FlaskService:
    def __init__(self, app: Flask, port: int) -> None:
        self.server: BaseWSGIServer = make_server(
            "127.0.0.1",
            port,
            app,
            threaded=True,
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
        self._build()

    def _build(self) -> None:
        urls = self.urls
        keys = {
            name: SigningKey.generate(name)
            for name in (
                "ap",
                "ps",
                "sentinel",
                "controller-a",
                "controller-b",
                "resource",
                "agent",
            )
        }
        descriptor = FunctionDescriptor(
            id=FUNCTION_ID,
            description="Return the eDoc unchanged",
            implementation_uri="memory://identity",
            digest="sha256:identity",
        )
        proposal = Dataflow(
            SOURCE_AGENT,
            FUNCTION_ID,
            EDOC_ID,
            DESTINATION_AGENT,
        )
        self.registry = SentinelRegistry(
            resource_bindings={
                SOURCE_AGENT: ResourceBinding(
                    source_ps=urls.ps,
                    resource_issuer=urls.resource,
                    resource_jkt=keys["resource"].thumbprint,
                )
            },
            controllers={
                (urls.resource, EDOC_ID): (
                    urls.controller_a,
                    urls.controller_b,
                )
            },
            functions={FUNCTION_ID: descriptor},
        )
        transport = RequestsTransport()
        controller_policy = ControllerPolicy((ExactRule(proposal),))
        ap = _metadata_app(urls.ap, "aauth-agent.json", keys["ap"])
        controller_a = create_as(
            urls.controller_a,
            key=keys["controller-a"],
            transport=transport,
            sentinel=urls.sentinel,
            controller_policy=controller_policy,
        )
        controller_b = create_as(
            urls.controller_b,
            key=keys["controller-b"],
            transport=transport,
            sentinel=urls.sentinel,
            controller_policy=controller_policy,
        )
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
        agent_token = issue_agent_token(
            issuer=urls.ap,
            agent=DESTINATION_AGENT,
            agent_jwk=keys["agent"].public_jwk,
            ps=urls.ps,
            key=keys["ap"],
        )
        resolver = _static_and_remote_resolver(
            {
                urls.ap: keys["ap"].public_jwk,
                urls.sentinel: keys["sentinel"].public_jwk,
                urls.controller_a: keys["controller-a"].public_jwk,
                urls.controller_b: keys["controller-b"].public_jwk,
            }
        )
        resource = DemoResource(
            issuer=urls.resource,
            sentinel=urls.sentinel,
            source_agent=SOURCE_AGENT,
            destination_agent=DESTINATION_AGENT,
            key=keys["resource"],
            controllers=(urls.controller_a, urls.controller_b),
            documents={EDOC_ID: {"message": "hello"}},
        )
        mcp = MCPServer("eDocs demo resource")

        @mcp.tool()
        def identity(edoc_id: str, ctx: Context) -> str:
            return resource.identity(
                ctx.request_context.request.scope["aauth"],
                edoc_id,
            )

        mcp_app = mcp.streamable_http_app(
            host=f"127.0.0.1:{urls.resource.rsplit(':', 1)[-1]}",
            authentication_middleware_factory=aauth_authorization(
                key_resolver=resolver,
                issuer=urls.sentinel,
                audience=urls.resource,
            ),
        )
        challenged = ChallengeApplication(
            resource,
            mcp_app,
            key_resolver=resolver,
        )
        ports = [
            int(url.rsplit(":", 1)[-1])
            for url in (
                urls.ap,
                urls.ps,
                urls.sentinel,
                urls.controller_a,
                urls.controller_b,
            )
        ]
        self.services = [
            FlaskService(ap, ports[0]),
            FlaskService(ps, ports[1]),
            FlaskService(sentinel, ports[2]),
            FlaskService(controller_a, ports[3]),
            FlaskService(controller_b, ports[4]),
            ASGIService(challenged, int(urls.resource.rsplit(":", 1)[-1])),
        ]
        self._write_state(keys["agent"], agent_token)

    def _write_state(self, agent_key: SigningKey, agent_token: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        key_path = self.state_dir / "agent.jwk"
        token_path = self.state_dir / "agent.token"
        env_path = self.state_dir / "demo.env"
        key_path.write_text(json.dumps(agent_key.private_jwk()))
        token_path.write_text(agent_token)
        env_path.write_text(
            f"EDOCS_MCP_URL={self.urls.mcp}\n"
            f"EDOCS_AGENT_KEY_FILE={key_path}\n"
            f"EDOCS_AGENT_TOKEN_FILE={token_path}\n"
            f"EDOCS_PERSON={PERSON}\n"
        )
        for path in (key_path, token_path, env_path):
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
            f"{self.urls.controller_a}/.well-known/aauth-access.json",
            f"{self.urls.controller_b}/.well-known/aauth-access.json",
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
    print(f"eDocs demo ready: {stack.urls.mcp}", flush=True)
    try:
        stopped.wait()
    finally:
        stack.stop()


if __name__ == "__main__":
    main()
