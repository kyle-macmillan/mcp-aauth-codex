import json
import socket
import stat

import pytest
import requests
from aauth_edocs import Dataflow, SigningKey, issue_agent_token, peek_jwt
from aauth_edocs.agent import RequestsTransport
from aauth_edocs.ps import create_ps
from mcp import Client
from mcp_types import ElicitResult

from mcp_aauth_codex.config import ProxyConfig
from mcp_aauth_codex.demo import (
    ALICE_SOURCE_AGENT,
    BOB_SOURCE_AGENT,
    BOB_RECIPIENT_AGENT,
    CAROL_SOURCE_AGENT,
    CAROL_RECIPIENT_AGENT,
    DESTINATION_AGENT,
    QUERY_FUNCTION_ID,
    DemoStack,
    DemoUrls,
    FlaskService,
)
from mcp_aauth_codex.proxy import build_server
from mcp_aauth_codex.demo_database import DEMO_EDOC_ID, DEMO_RESOURCE_URI
from mcp_aauth_codex.providers import ProviderEndpoint
from mcp_edocs_agent.new_agent import write_agent_credentials


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


def _mint_agent(
    state_dir,
    urls: DemoUrls,
    *,
    role: str,
    agent_id: str,
    person: str,
) -> FlaskService:
    """Mirror new_agent.py: mint one agent identity and run its own PS."""
    ap_key = SigningKey.from_private_jwk(
        json.loads((state_dir / "keys" / "ap.jwk").read_text())
    )
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
        provider_path=state_dir / "providers.json",
        role=role,
        agent_id=agent_id,
        person=person,
        agent_key=agent_key,
        agent_token=agent_token,
        resource_url=_free_url(),
        sentinel_url=urls.sentinel,
    )
    service = FlaskService(ps, int(urls.ps.rsplit(":", 1)[-1]))
    service.start()
    return service


def _payload(result):
    return json.loads(result.content[0].text)


def _provider_refs(result) -> dict[str, str]:
    return {
        provider["provider_id"]: provider["provider_ref"]
        for provider in _payload(result)["providers"]
    }


def _resource_ref(result) -> str:
    return _payload(result)["resources"][0]["resource_ref"]


async def _discover_resource_refs(client) -> dict[str, str]:
    providers = await client.call_tool("list_providers", {})
    resources_by_uri = {}
    for provider_ref in _provider_refs(providers).values():
        resources = await client.call_tool(
            "list_resources",
            {"provider_ref": provider_ref},
        )
        for resource in _payload(resources)["resources"]:
            resources_by_uri[resource["uri"]] = resource["resource_ref"]
    return resources_by_uri


@pytest.mark.asyncio
async def test_live_demo_stack_runs_complete_flow(monkeypatch, tmp_path):
    state_dir = tmp_path / "state"
    stack = DemoStack(
        state_dir,
        _demo_urls(),
    )
    ps_service = None
    try:
        stack.start()
        ps_service = _mint_agent(
            state_dir,
            stack.urls,
            role="producer",
            agent_id=DESTINATION_AGENT,
            person="alice",
        )
        env = {}
        for line in (state_dir / "agents" / "producer.env").read_text().splitlines():
            name, value = line.split("=", 1)
            env[name] = value
            monkeypatch.setenv(name, value)

        config = ProxyConfig.from_env()
        proxy = build_server(config)
        prompts = []

        async def approve(_context, params):
            prompts.append(params)
            return ElicitResult(action="accept", content={"approve": True})

        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            providers = await client.call_tool("list_providers", {})
            provider_refs = _provider_refs(providers)
            resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["alice"]},
            )
            bob_resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["bob"]},
            )
            carol_resources = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["carol"]},
            )
            alice_ref = _resource_ref(resources)
            bob_ref = _resource_ref(bob_resources)
            carol_ref = _resource_ref(carol_resources)
            assert prompts == []
            result = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": alice_ref,
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": {
                        "statement": (
                            "SELECT name, department FROM document "
                            "WHERE department = ? ORDER BY name"
                        ),
                        "parameters": ["engineering"],
                    },
                },
            )
            bob_result = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": bob_ref,
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": {
                        "statement": (
                            "SELECT name, department FROM document "
                            "WHERE department = ? ORDER BY name"
                        ),
                        "parameters": ["engineering"],
                    },
                },
            )
            carol_result = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": carol_ref,
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": {
                        "statement": (
                            "SELECT name, department FROM document "
                            "WHERE department = ? ORDER BY name"
                        ),
                        "parameters": ["engineering"],
                    },
                },
            )
            denied = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": alice_ref,
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": {
                        "statement": (
                            "SELECT name, department FROM document "
                            "WHERE department = ? ORDER BY name"
                        ),
                        "parameters": ["finance"],
                    },
                },
            )
            unpolicied_function = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": alice_ref,
                    "function_id": "employee_count@1",
                    "arguments": {},
                },
            )

        materialized_before_misroute = set(stack.registry.materialized)
        prompt_count_before_misroute = len(prompts)
        misrouted_proxy = build_server(
            ProxyConfig(
                agent_token=config.agent_token,
                signing_key=config.signing_key,
                person=config.person,
                providers=(
                    ProviderEndpoint(
                        provider_id="alice",
                        display_name="Alice",
                        description="Deliberately misrouted test entry",
                        mcp_url=stack.urls.bob_mcp,
                    ),
                ),
            )
        )
        async with Client(
            misrouted_proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            misrouted_providers = await client.call_tool("list_providers", {})
            misrouted = await client.call_tool(
                "list_resources",
                {
                    "provider_ref": _provider_refs(misrouted_providers)[
                        "alice"
                    ]
                },
            )

        assert misrouted.is_error is True
        assert "provider alice returned an unqualified resource URI" in (
            misrouted.content[0].text
        )
        assert len(prompts) == prompt_count_before_misroute
        assert set(stack.registry.materialized) == materialized_before_misroute

        assert requests.patch(
            f"{stack.urls.alice_resource}/admin/documents/doc_01JDEMO7F3A",
            json={"title": "Alice renamed data"},
            timeout=2,
        ).status_code == 200
        assert requests.put(
            (
                f"{stack.urls.alice_resource}/admin/documents/"
                "doc_01JDEMO7F3A/enabled"
            ),
            json={"enabled": False},
            timeout=2,
        ).status_code == 200
        prompt_count_before_disabled = len(prompts)
        materialized_before_disabled = set(stack.registry.materialized)
        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            disabled_alice = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["alice"]},
            )
            unchanged_bob = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["bob"]},
            )
            disabled_invocation = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": alice_ref,
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": {
                        "statement": "SELECT count(*) AS count FROM document",
                        "parameters": [],
                    },
                },
            )
        assert json.loads(disabled_alice.content[0].text)["resources"] == []
        assert len(json.loads(unchanged_bob.content[0].text)["resources"]) == 1
        assert disabled_invocation.is_error is True
        assert len(prompts) == prompt_count_before_disabled
        assert set(stack.registry.materialized) == materialized_before_disabled

        assert requests.put(
            (
                f"{stack.urls.alice_resource}/admin/documents/"
                "doc_01JDEMO7F3A/enabled"
            ),
            json={"enabled": True},
            timeout=2,
        ).status_code == 200
        async with Client(proxy, mode="legacy") as client:
            restored_alice = await client.call_tool(
                "list_resources",
                {"provider_ref": provider_refs["alice"]},
            )
        restored_payload = json.loads(restored_alice.content[0].text)
        assert restored_payload["resources"][0]["title"] == "Alice renamed data"
        assert "database_path" not in str(restored_payload)
        assert "original_filename" not in str(restored_payload)

        provider_payload = json.loads(providers.content[0].text)
        assert [
            {
                key: value
                for key, value in provider.items()
                if key != "provider_ref"
            }
            for provider in provider_payload["providers"]
        ] == [
            {
                "provider_id": "alice",
                "display_name": "Alice",
                "description": "Alice's governed eDocs",
            },
            {
                "provider_id": "bob",
                "display_name": "Bob",
                "description": "Bob's governed eDocs",
            },
            {
                "provider_id": "carol",
                "display_name": "Carol",
                "description": "Carol's governed eDocs",
            },
        ]
        assert all(
            provider["provider_ref"].startswith("provider_")
            for provider in provider_payload["providers"]
        )
        resource_payload = json.loads(resources.content[0].text)
        assert [resource["uri"] for resource in resource_payload["resources"]] == [
            DEMO_RESOURCE_URI
        ]
        assert resource_payload["resources"][0]["title"] == "Employee directory"
        assert "mimeType" not in resource_payload["resources"][0]
        assert "_meta" not in resource_payload["resources"][0]
        bob_resource_payload = json.loads(bob_resources.content[0].text)
        assert [
            resource["uri"]
            for resource in bob_resource_payload["resources"]
        ] == [DEMO_RESOURCE_URI.replace("edoc://alice/", "edoc://bob/")]
        assert "mimeType" not in bob_resource_payload["resources"][0]
        carol_resource_payload = json.loads(carol_resources.content[0].text)
        assert [
            resource["uri"]
            for resource in carol_resource_payload["resources"]
        ] == [DEMO_RESOURCE_URI.replace("edoc://alice/", "edoc://carol/")]
        assert "mimeType" not in carol_resource_payload["resources"][0]
        assert result.is_error is False
        payload = json.loads(result.content[0].text)
        assert payload["structured_content"]["rows"] == [
            {"name": "Avery", "department": "engineering"},
            {"name": "Casey", "department": "engineering"},
        ]
        assert payload["structured_content"]["derived_edoc_id"].startswith(
            "derived_"
        )
        assert bob_result.is_error is False
        bob_payload = json.loads(bob_result.content[0].text)
        assert bob_payload["structured_content"]["rows"] == [
            {"name": "Morgan", "department": "engineering"},
            {"name": "Taylor", "department": "engineering"},
        ]
        assert carol_result.is_error is False
        carol_payload = json.loads(carol_result.content[0].text)
        assert carol_payload["structured_content"]["rows"] == [
            {"name": "Emerson", "department": "engineering"},
            {"name": "Harper", "department": "engineering"},
        ]
        assert len(prompts) == 5
        assert '"engineering"' in prompts[0].message
        assert BOB_SOURCE_AGENT in prompts[1].message
        assert CAROL_SOURCE_AGENT in prompts[2].message
        assert '"finance"' in prompts[3].message
        assert "employee_count@1" in prompts[4].message
        assert denied.is_error is True
        assert unpolicied_function.is_error is True
        assert all(
            flow.function != "employee_count@1"
            for flow in stack.registry.materialized
        )
        assert DESTINATION_AGENT in prompts[0].message
        assert stack.registry is not None
        assert {
            flow.source for flow in stack.registry.materialized
        } == {
            ALICE_SOURCE_AGENT,
            BOB_SOURCE_AGENT,
            CAROL_SOURCE_AGENT,
        }
        assert len(stack.registry.derived_documents) == 3
        assert {
            derived.dataflow.source
            for derived in stack.registry.derived_documents.values()
        } == {
            ALICE_SOURCE_AGENT,
            BOB_SOURCE_AGENT,
            CAROL_SOURCE_AGENT,
        }
        assert all(
            derived.possessor == DESTINATION_AGENT
            and derived.output_digest.startswith("sha256:")
            for derived in stack.registry.derived_documents.values()
        )
        assert all(
            (stack.derived_store.root / f"{derived.edoc_id}.json").exists()
            for derived in stack.registry.derived_documents.values()
        )
        alice_derived = next(
            derived
            for derived in stack.registry.derived_documents.values()
            if derived.dataflow.source == ALICE_SOURCE_AGENT
        )
        share_with_carol = Dataflow.from_arguments(
            DESTINATION_AGENT,
            "identity@1",
            alice_derived.edoc_id,
            CAROL_RECIPIENT_AGENT,
            {},
        )
        share_with_bob = Dataflow.from_arguments(
            DESTINATION_AGENT,
            "identity@1",
            alice_derived.edoc_id,
            BOB_RECIPIENT_AGENT,
            {},
        )
        assert stack.policies["alice"].evaluate(share_with_carol) is not None
        assert stack.policies["alice"].evaluate(share_with_bob) is None
        assert stack.registry.controllers[
            (stack.urls.alice_resource, "doc_01JDEMO7F3A")
        ] == (stack.urls.alice_as,)
        assert stack.registry.controllers[
            (stack.urls.bob_resource, "doc_01JDEMO7F3A")
        ] == (stack.urls.bob_as,)
        assert stack.registry.controllers[
            (stack.urls.carol_resource, "doc_01JDEMO7F3A")
        ] == (stack.urls.carol_as,)
        provider_file = state_dir / "providers.json"
        assert env["EDOCS_PROVIDER_FILE"] == str(provider_file)
        provider_values = json.loads(provider_file.read_text())
        assert provider_values[1]["mcp_url"] == stack.urls.bob_mcp
        assert provider_values[2]["mcp_url"] == stack.urls.carol_mcp
        assert stat.S_IMODE(provider_file.stat().st_mode) == 0o600
        role, agent_id = "producer", DESTINATION_AGENT
        role_env = {}
        env_path = state_dir / "agents" / f"{role}.env"
        for line in env_path.read_text().splitlines():
            name, value = line.split("=", 1)
            role_env[name] = value
        assert role_env["EDOCS_DEMO_AGENT_ID"] == agent_id
        assert role_env["EDOCS_SENTINEL_URL"] == stack.urls.sentinel
        assert role_env["EDOCS_FUNCTION_REGISTRY_URL"] == (
            f"{stack.urls.sentinel}/registry/functions"
        )
        assert "EDOCS_AGENT_RESOURCE_URL" in role_env
        claude_config = state_dir / "agents" / f"{role}.claude-mcp.json"
        assert role_env["EDOCS_CLAUDE_MCP_CONFIG"] == str(claude_config)
        claude_server = json.loads(claude_config.read_text())["mcpServers"][
            "edocs-aauth"
        ]
        assert claude_server["type"] == "stdio"
        assert claude_server["env"]["EDOCS_AGENT_KEY_FILE"] == (
            role_env["EDOCS_AGENT_KEY_FILE"]
        )
        assert claude_server["env"]["EDOCS_AGENT_TOKEN_FILE"] == (
            role_env["EDOCS_AGENT_TOKEN_FILE"]
        )
        assert role_env["EDOCS_DEMO_AGENT_ROLE"] == role
        assert peek_jwt(
            (state_dir / "agents" / f"{role}.token").read_text()
        )[1]["sub"] == agent_id
        for suffix in ("jwk", "token", "env"):
            assert stat.S_IMODE(
                (state_dir / "agents" / f"{role}.{suffix}").stat().st_mode
            ) == 0o600
        assert stat.S_IMODE(claude_config.stat().st_mode) == 0o600
    finally:
        if ps_service is not None:
            ps_service.stop()
        stack.stop()

    assert not (state_dir / "ready").exists()


@pytest.mark.asyncio
async def test_live_policy_mutation_is_isolated_and_restartable(
    monkeypatch,
    tmp_path,
):
    state_dir = tmp_path / "policy-state"
    stack = DemoStack(state_dir, _demo_urls())
    ps_service = None
    try:
        stack.start()
        ps_service = _mint_agent(
            state_dir,
            stack.urls,
            role="producer",
            agent_id=DESTINATION_AGENT,
            person="alice",
        )
        for line in (state_dir / "agents" / "producer.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        proxy = build_server(ProxyConfig.from_env())
        prompts = []

        async def approve(_context, params):
            prompts.append(params)
            return ElicitResult(action="accept", content={"approve": True})

        alice_policy = stack.policies["alice"]
        alice_rule = alice_policy.list_rules()[0]
        catalog_before = requests.get(
            f"{stack.urls.alice_resource}/admin/documents",
            timeout=2,
        ).json()
        materialized_before = set(stack.registry.materialized)

        assert requests.delete(
            (
                f"{stack.urls.control}/api/providers/alice/policies/"
                f"{alice_rule.rule_id}"
            ),
            timeout=2,
        ).status_code == 204

        assert stack.registry.materialized == materialized_before
        assert requests.get(
            f"{stack.urls.alice_resource}/admin/documents",
            timeout=2,
        ).json() == catalog_before
        assert stack.policies["bob"].evaluate(alice_rule.target) is None
        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            resource_refs = await _discover_resource_refs(client)
            denied_alice = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_refs[DEMO_RESOURCE_URI],
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": alice_rule.target.function_args,
                },
            )
            allowed_bob = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_refs[
                        DEMO_RESOURCE_URI.replace(
                            "edoc://alice/",
                            "edoc://bob/",
                        )
                    ],
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": stack.policies[
                        "bob"
                    ].list_rules()[0].target.function_args,
                },
            )

        assert denied_alice.is_error is True
        assert allowed_bob.is_error is False
        assert all(
            flow.source != ALICE_SOURCE_AGENT
            for flow in stack.registry.materialized
        )

        restored_response = requests.post(
            f"{stack.urls.control}/api/providers/alice/policies",
            json={
                "target": {
                    "source": alice_rule.target.source,
                    "function": alice_rule.target.function,
                    "document": alice_rule.target.document,
                    "destination": alice_rule.target.destination,
                    "function_args": alice_rule.target.function_args,
                },
                "prerequisite": None,
            },
            timeout=2,
        )
        assert restored_response.status_code == 201
        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            resource_refs = await _discover_resource_refs(client)
            restored_alice = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_refs[DEMO_RESOURCE_URI],
                    "function_id": QUERY_FUNCTION_ID,
                    "arguments": alice_rule.target.function_args,
                },
            )

        assert restored_alice.is_error is False
        assert len(alice_policy.list_rules()) == 3
    finally:
        if ps_service is not None:
            ps_service.stop()
        stack.stop()

    restarted = DemoStack(state_dir, _demo_urls())
    assert len(restarted.policies["alice"].list_rules()) == 3


def test_demo_control_panel_manages_isolated_provider_state(tmp_path):
    stack = DemoStack(tmp_path / "control-state", _demo_urls())
    try:
        stack.start()
        root = stack.urls.control
        page = requests.get(f"{root}/demo", timeout=2)
        assert page.status_code == 200
        assert "Available functions" in page.text
        assert "Controlled derived eDocs" in page.text
        assert "No policy — invocation denied" in page.text
        assert "openPolicyEditor" in page.text
        assert "<summary>Edit policy</summary>" not in page.text
        providers = requests.get(f"{root}/api/providers", timeout=2).json()
        assert [
            item["provider_id"] for item in providers["providers"]
        ] == ["alice", "bob", "carol"]
        public_functions = requests.get(
            f"{root}/api/sentinel/functions",
            timeout=2,
        )
        assert public_functions.status_code == 200
        public_function_body = public_functions.json()
        assert [
            function["function_id"]
            for function in public_function_body["functions"]
        ] == sorted(
            function["function_id"]
            for function in public_function_body["functions"]
        )
        public_employee_count = next(
            function
            for function in public_function_body["functions"]
            if function["function_id"] == "employee_count@1"
        )
        assert public_employee_count == {
            "function_id": "employee_count@1",
            "description": "Count all employees in the document",
            "digest": public_employee_count["digest"],
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        }
        assert "implementation" not in str(public_function_body)
        sentinel = requests.get(f"{root}/api/sentinel", timeout=2)
        assert sentinel.status_code == 200
        sentinel_body = sentinel.json()
        assert len(sentinel_body["resource_bindings"]) == 3
        assert len(sentinel_body["controllers"]) == 3
        assert sentinel_body["materialized"] == []
        employee_count = next(
            function
            for function in sentinel_body["functions"]
            if function["function_id"] == "employee_count@1"
        )
        assert employee_count == {
            "function_id": "employee_count@1",
            "description": "Count all employees in the document",
            "implementation_uri": "demo-sql://employee_count@1",
            "digest": employee_count["digest"],
            "input_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "implementation": {
                "runtime": "sql",
                "source": (
                    "SELECT count(*) AS employee_count FROM document"
                ),
            },
        }
        registered = requests.post(
            f"{root}/api/sentinel/functions",
            json={
                "function_id": "summarize@1",
                "description": "Summarize a document",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                "implementation": {
                    "runtime": "sql",
                    "source": "SELECT count(*) AS count FROM document",
                },
            },
            timeout=2,
        )
        assert registered.status_code == 201
        assert registered.json()["function"]["function_id"] == "summarize@1"
        assert "summarize@1" in stack.registry.functions
        refreshed_functions = requests.get(
            f"{root}/api/sentinel/functions",
            timeout=2,
        ).json()["functions"]
        assert "summarize@1" in {
            function["function_id"] for function in refreshed_functions
        }
        assert all(
            set(function)
            == {
                "function_id",
                "description",
                "input_schema",
                "digest",
            }
            for function in refreshed_functions
        )
        assert requests.post(
            f"{root}/api/sentinel/functions",
            json={
                "function_id": "summarize@1",
                "description": "Duplicate",
                "input_schema": {},
                "implementation": {
                    "runtime": "sql",
                    "source": "SELECT count(*) AS count FROM document",
                },
            },
            timeout=2,
        ).status_code == 400
        stack.registry.materialized.add(
            stack.policies["alice"].list_rules()[0].target
        )
        assert len(
            requests.get(f"{root}/api/sentinel", timeout=2).json()[
                "materialized"
            ]
        ) == 1
        sharing_rule = stack.policies["alice"].list_rules()[1]
        assert sharing_rule.target.source == DESTINATION_AGENT
        assert sharing_rule.target.function == "identity@1"
        assert sharing_rule.target.document.dataflow == (
            stack.policies["alice"].list_rules()[0].target
        )
        assert sharing_rule.target.destination == CAROL_RECIPIENT_AGENT
        assert sharing_rule.target.function_args == {}
        assert stack.policies["bob"].evaluate(sharing_rule.target) is None

        materialize = requests.post(
            f"{stack.urls.sentinel}/registry/materializations",
            json={
                "dataflow": {
                    "source": ALICE_SOURCE_AGENT,
                    "function": "query_table@1",
                    "document": "doc_01JDEMO7F3A",
                    "destination": DESTINATION_AGENT,
                    "function_args": {
                        "statement": "SELECT 1",
                        "parameters": [],
                    },
                },
                "output": {
                    "columns": ["n"],
                    "rows": [{"n": 1}],
                    "truncated": False,
                },
                "controllers": [stack.urls.alice_as],
            },
            timeout=2,
        )
        assert materialize.status_code == 201
        derived_edoc_id = materialize.json()["derived_edoc_id"]
        alice_with_derived = requests.get(
            f"{root}/api/providers/alice/documents", timeout=2
        ).json()["documents"]
        bob_without_derived = requests.get(
            f"{root}/api/providers/bob/documents", timeout=2
        ).json()["documents"]
        derived_docs = [
            document
            for document in alice_with_derived
            if document.get("kind") == "derived"
        ]
        assert len(derived_docs) == 1
        assert derived_docs[0]["edoc_id"] == derived_edoc_id
        assert derived_docs[0]["published"] is False
        assert derived_docs[0]["possessor"] == DESTINATION_AGENT
        assert all(
            document.get("kind") != "derived" for document in bob_without_derived
        )

        created = requests.post(
            f"{root}/api/providers/alice/documents",
            json={
                "title": "Alice project roster",
                "description": "A second Alice-owned CSV",
                "csv": "project,owner\nAtlas,Avery\nBeacon,Casey\n",
            },
            timeout=2,
        )
        assert created.status_code == 201
        assert created.json()["document"]["resource_uri"].startswith(
            "edoc://alice/"
        )
        alice_documents = requests.get(
            f"{root}/api/providers/alice/documents", timeout=2
        ).json()["documents"]
        bob_documents = requests.get(
            f"{root}/api/providers/bob/documents", timeout=2
        ).json()["documents"]
        assert len(
            [document for document in alice_documents if document.get("kind") != "derived"]
        ) == 2
        assert any(
            document["edoc_id"] == derived_edoc_id for document in alice_documents
        )
        assert len(bob_documents) == 1
        added_id = created.json()["document"]["edoc_id"]
        renamed = requests.patch(
            f"{root}/api/providers/alice/documents/{added_id}",
            json={"title": "Renamed project roster"},
            timeout=2,
        )
        assert renamed.status_code == 200
        assert renamed.json()["document"]["title"] == "Renamed project roster"
        disabled = requests.put(
            f"{root}/api/providers/alice/documents/{added_id}/enabled",
            json={"enabled": False},
            timeout=2,
        )
        assert disabled.status_code == 200
        assert disabled.json()["document"]["enabled"] is False

        seed = requests.get(
            f"{root}/api/providers/alice/policies", timeout=2
        ).json()["rules"][0]
        target = dict(seed["target"])
        target["function_args"] = {
            **target["function_args"],
            "parameters": ["finance"],
        }
        created_rule = requests.post(
            f"{root}/api/providers/alice/policies",
            json={"target": target, "prerequisite": None},
            timeout=2,
        )
        assert created_rule.status_code == 201
        rule_id = created_rule.json()["rule"]["rule_id"]
        target["function_args"]["parameters"] = ["legal"]
        replaced = requests.put(
            f"{root}/api/providers/alice/policies/{rule_id}",
            json={"target": target, "prerequisite": None},
            timeout=2,
        )
        assert replaced.status_code == 200
        assert replaced.json()["rule"]["rule_id"] == rule_id
        assert len(stack.policies["alice"].list_rules()) == 4
        assert len(stack.policies["bob"].list_rules()) == 1
        assert requests.delete(
            f"{root}/api/providers/alice/policies/{rule_id}", timeout=2
        ).status_code == 204

        assert requests.get(
            f"{root}/api/providers/mallory/documents", timeout=2
        ).status_code == 404
        assert requests.post(
            f"{root}/api/providers/alice/policies",
            json={"target": {}},
            timeout=2,
        ).status_code == 400
    finally:
        stack.stop()


@pytest.mark.asyncio
async def test_agent_registers_function_then_owner_policy_enables_it(
    monkeypatch,
    tmp_path,
):
    state_dir = tmp_path / "agent-function-state"
    stack = DemoStack(state_dir, _demo_urls())
    ps_service = None
    try:
        stack.start()
        ps_service = _mint_agent(
            state_dir,
            stack.urls,
            role="producer",
            agent_id=DESTINATION_AGENT,
            person="alice",
        )
        for line in (state_dir / "agents" / "producer.env").read_text().splitlines():
            name, value = line.split("=", 1)
            monkeypatch.setenv(name, value)
        proxy = build_server(ProxyConfig.from_env())
        prompts = []

        async def approve(_context, params):
            prompts.append(params)
            return ElicitResult(action="accept", content={"approve": True})

        arguments = {"department": "engineering"}
        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            resource_refs = await _discover_resource_refs(client)
            functions_before = await client.call_tool(
                "list_edocs_functions",
                {},
            )
            registered = await client.call_tool(
                "register_edocs_function",
                {
                    "function_id": "department_names@1",
                    "description": "List names in one department",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "department": {"type": "string"},
                        },
                        "required": ["department"],
                        "additionalProperties": False,
                    },
                    "implementation": {
                        "runtime": "sql",
                        "source": (
                            "SELECT name FROM document "
                            "WHERE department = $department ORDER BY name"
                        ),
                    },
                },
            )
            functions_after = await client.call_tool(
                "list_edocs_functions",
                {},
            )
            denied = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_refs[DEMO_RESOURCE_URI],
                    "function_id": "department_names@1",
                    "arguments": arguments,
                },
            )

        assert registered.is_error is False
        assert "department_names@1" not in {
            function["function_id"]
            for function in _payload(functions_before)["functions"]
        }
        discovered_function = next(
            function
            for function in _payload(functions_after)["functions"]
            if function["function_id"] == "department_names@1"
        )
        assert discovered_function["input_schema"]["required"] == [
            "department"
        ]
        assert set(discovered_function) == {
            "function_id",
            "description",
            "input_schema",
            "digest",
        }
        assert "department_names@1" in stack.registry.functions
        assert denied.is_error is True
        assert all(
            flow.function != "department_names@1"
            for flow in stack.registry.materialized
        )

        policy_response = requests.post(
            f"{stack.urls.control}/api/providers/alice/policies",
            json={
                "target": {
                    "source": ALICE_SOURCE_AGENT,
                    "function": "department_names@1",
                    "document": DEMO_EDOC_ID,
                    "destination": DESTINATION_AGENT,
                    "function_args": arguments,
                },
                "prerequisite": None,
            },
            timeout=2,
        )
        assert policy_response.status_code == 201

        async with Client(
            proxy,
            mode="legacy",
            elicitation_callback=approve,
        ) as client:
            resource_refs = await _discover_resource_refs(client)
            allowed = await client.call_tool(
                "invoke_edocs_function",
                {
                    "resource_ref": resource_refs[DEMO_RESOURCE_URI],
                    "function_id": "department_names@1",
                    "arguments": arguments,
                },
            )

        assert allowed.is_error is False
        payload = json.loads(allowed.content[0].text)
        assert payload["structured_content"]["rows"] == [
            {"name": "Avery"},
            {"name": "Casey"},
        ]
        assert len(prompts) == 2
    finally:
        if ps_service is not None:
            ps_service.stop()
        stack.stop()
