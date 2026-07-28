"""Mint one agent identity and run its Person Server.

Complements ``demo.py``'s infra-only ``DemoStack``: this module is the one
place that mints an agent identity (a fixed demo role or an arbitrary new
party) against the Agent Provider key that infra already persisted, and runs
that agent's own Person Server for the consent flow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import threading
import time
from pathlib import Path

import requests
from aauth_edocs import SigningKey, issue_agent_token
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.ps import create_ps

from .demo import DemoUrls, FlaskService

ROLE_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


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
                            "EDOCS_PERSON": person,
                            "EDOCS_FUNCTION_REGISTRY_URL": (
                                f"{urls.control}/api/sentinel/functions"
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
        f"EDOCS_PERSON={person}\n"
        f"EDOCS_DEMO_AGENT_ID={agent_id}\n"
        f"EDOCS_DEMO_AGENT_ROLE={role}\n"
        f"EDOCS_CLAUDE_MCP_CONFIG={claude_mcp_path}\n"
        f"EDOCS_FUNCTION_REGISTRY_URL={urls.control}/api/sentinel/functions\n"
    )
    for path in (key_path, token_path, env_path, claude_mcp_path):
        os.chmod(path, 0o600)


def _wait_ready(url: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if requests.get(url, timeout=0.25).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"agent's Person Server failed readiness: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".demo-state"))
    parser.add_argument("--role", required=True)
    parser.add_argument("--agent-id", default=None)
    parser.add_argument("--person", default=None)
    args = parser.parse_args()

    if not ROLE_PATTERN.fullmatch(args.role):
        raise SystemExit(f"Invalid agent role: {args.role}")
    role = args.role
    agent_id = args.agent_id or f"aauth:{role}@newparty.local"
    person = args.person or role

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

    ap_key = SigningKey.from_private_jwk(json.loads(ap_key_path.read_text()))
    agent_key = SigningKey.generate(f"{role}-agent")
    ps_key = SigningKey.generate("ps")

    transport = RequestsTransport()
    ps = create_ps(
        urls.ps,
        key=ps_key,
        person=person,
        policy=lambda _agent, _resource: "pending",
        transport=transport,
    )
    agent_token = issue_agent_token(
        issuer=urls.ap,
        agent=agent_id,
        agent_jwk=agent_key.public_jwk,
        ps=urls.ps,
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
    )

    service = FlaskService(ps, int(urls.ps.rsplit(":", 1)[-1]))
    stopped = threading.Event()

    def request_stop(_signum, _frame):
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    service.start()
    try:
        _wait_ready(f"{urls.ps}/.well-known/aauth-person.json")
        ready_path = state_dir / "agents" / f"{role}.ready"
        ready_path.write_text("ready\n")
        print(f"{role}: {agent_id} (person: {person})", flush=True)
        print(f"Control panel: {urls.control}/demo", flush=True)
        stopped.wait()
    finally:
        ready_path = state_dir / "agents" / f"{role}.ready"
        ready_path.unlink(missing_ok=True)
        service.stop()


if __name__ == "__main__":
    main()
