# eDocs AAuth coding-agent bridge

This project exposes a local stdio MCP server to Codex and Claude Code. The
server acts as an AAuth-aware client of remote eDocs Streamable HTTP MCP
servers, so coding-agent hosts do not need custom HTTP authentication
implementations.

The bridge uses standard MCP tools, tool annotations, server instructions, and
form elicitation. Host-specific code is limited to launch configuration:
`mcp_edocs_agent.gateway.EdocsGateway` owns discovery, authorization, consent,
and invocation, while `mcp_edocs_agent.mcp_adapter` exposes that API over MCP.
The former `mcp_aauth_codex` Python package and `mcp-aauth-codex` command remain
as compatibility aliases.

Set these variables before starting a coding-agent host:

- `EDOCS_PROVIDER_FILE`: path to the private provider-directory JSON file
- `EDOCS_AGENT_TOKEN_FILE`: path to the agent token
- `EDOCS_AGENT_KEY_FILE`: path to the agent's private JWK
- `EDOCS_PERSON`: prototype Person Server login identity used by the demo
- `EDOCS_SENTINEL_URL`: Sentinel base URL (set by `new_agent` for publish and registry)
- `EDOCS_AGENT_RESOURCE_URL`: this agent's resource server (set by `new_agent`)
- `EDOCS_DEMO_AGENT_ID`: this agent's AAuth identity (set by `new_agent`)

The plugin provides `list_providers()`,
`list_resources(provider_ref)`, and
`list_edocs_functions()`, plus
`invoke_edocs_function(resource_ref, function_id, arguments)` and
`publish_derived_edoc(derived_edoc_id)` when the agent hosts a resource server.
Provider listing comes from the bridge's directory, while resource listing is
forwarded live to the selected provider's MCP server without starting a
consent flow. Provider and resource references are opaque and scoped to the
bridge process: `provider_ref` is issued only by `list_providers`, and
`resource_ref` is issued only by `list_resources`. The invocation tool does not
accept an `edoc://` URI, so a URI learned or guessed outside discovery cannot
skip the workflow. These references enforce discovery order; they do not
replace AAuth authorization.

`list_edocs_functions()` fetches the live shared registry and returns only each
registered function's ID, description, input schema, and descriptor digest.
It does not return implementation source, implementation locations, or
policy-derived authorization information. Registration therefore describes
what exists, not what a provider policy will permit.

The selected provider ID is repeated at authorization and execution, so a
directory entry that points Alice at Bob's endpoint is rejected before consent
or materialization. A remote AAuth resource challenge is exchanged at the
Person Server. If consent is
required, the bridge asks the host to display an MCP elicitation containing only
the PS-verified eDocs claims. A grant is submitted to the PS, the bridge polls
for the final resource-scoped token, and the original MCP request is retried.

`EDOCS_PERSON` is only the current prototype login bridge. Production person
authentication should provide an already-authenticated PS session and should
not pass credentials through an MCP elicitation.

Install the locked local development environment with `uv sync --frozen`.
The demo launcher uses this project's `.venv`, including DuckDB and the
editable adjacent dependencies. The stdio bridge launcher prefers this
project's environment and retains the adjacent `mcp-aauth` environment as a
workspace fallback.

Demo coding-agent sessions start in a fresh empty temporary workspace and do not
inherit `EDOCS_*` runtime values. The launchers disable general-purpose shell,
filesystem-reading, web, browser, connector, and subagent tools; only the
configured eDocs MCP tools and consent interaction are intended to remain
available. The separately printed control-panel URL is for the human operator
and is not included in the agent environment. This tool restriction is a demo
boundary, not a replacement for OS isolation when running hostile code.

## Live demo

Start shared infra in one terminal (Alice/Bob/Carol resource and access
servers, Sentinel, and the control panel):

```bash
scripts/run_infra.sh
```

In another terminal, attach a coding agent. `CLIENT` is `codex` or `claude`,
and `ROLE` is a local label such as `producer`, `carol`, or `bob`:

```bash
scripts/run_new_agent.sh codex producer \
  --agent-id aauth:producer@demo.local \
  --person alice \
  --display-name Producer
```

Or Claude Code:

```bash
scripts/run_new_agent.sh claude producer \
  --agent-id aauth:producer@demo.local \
  --person alice \
  --display-name Producer
```

Arguments after `--` are passed to the selected client. For example:

```bash
scripts/run_new_agent.sh claude producer \
  --agent-id aauth:producer@demo.local \
  --person alice \
  --display-name Producer \
  -- --model sonnet
```

Convenience wrappers still exist: `scripts/run_demo.sh` starts infra and one
Producer session, and for three independent sessions install `tmux` and run:

```bash
scripts/run_multi_agent_demo.sh
scripts/run_multi_agent_demo.sh --client claude
```

The multi-agent launcher opens tiled Producer, Carol, and Bob panes backed by
distinct private keys and agent tokens:

- Producer: `aauth:producer@demo.local`
- Carol: `aauth:carol@demo.local`
- Bob: `aauth:bob@demo.local`

All three sessions share the same provider directory, function registry,
Sentinel, and control panel, while each bridge receives only its own credential
files. The generated configurations live under
`.demo-state/agents/{producer,carol,bob}.{env,jwk,token}` with mode `0600`.

Carol and Bob can demonstrate independent identity and public provider
discovery. After Producer successfully invokes an upstream function, the
result includes a `derived_edoc_id`. Producer must call `publish_derived_edoc`
to expose that output on Producer's own resource server. Alice remains the
controller via inherited controllers; Carol can then invoke `identity@1` on
the published eDoc through Sentinel, while Bob is still denied by Alice's
policy.

The launchers compose the shared AAuth Agent Provider, Person Server, and
Sentinel with three independent provider domains. Alice, Bob, and Carol each
have their own Access Server, MCP resource server, source agent, signing key,
catalog, and DuckDB storage. It generates an ephemeral demo agent key, token,
and private `providers.json` directory under `.demo-state/`. Codex receives a
one-run `mcp_servers.edocs-aauth` configuration with MCP elicitation approval
enabled. Claude Code receives a generated role-specific MCP config through
`--strict-mcp-config`. Neither path modifies user-level configuration.

Generated credentials, environment files, and Claude MCP configurations live
under `.demo-state/agents/` with mode `0600`.

## Plugin manifests

The repository contains both host adapters:

- `.codex-plugin/plugin.json` points Codex at `.mcp.json`.
- `.claude-plugin/plugin.json` declares the same stdio MCP server using
  `${CLAUDE_PLUGIN_ROOT}`.

The demo launcher does not require either plugin to be installed; it supplies
an isolated per-run configuration. The Claude manifest follows Claude Code's
plugin layout and expects the same `EDOCS_*` environment variables listed
above.

The demo setup script resets all three resource-owned DuckDB databases. The
providers deliberately use the same opaque local ID, exposed as distinct
`edoc://alice/doc_01JDEMO7F3A`, `edoc://bob/doc_01JDEMO7F3A`, and
`edoc://carol/doc_01JDEMO7F3A` URIs. All expose `query_table@1`, but return
their own provider-local data. Filenames remain catalog metadata and are not
authorization identities.

Reusable provider behavior now lives in the adjacent `mcp-edocs-provider`
repository: public catalog discovery, proactive authorization, provider
binding, final-token validation, and dispatch through an injected function
loader. `demo.py` supplies only localhost composition, provider
specifications, policies, and seeded state.

Each demo provider also exposes the provider package's localhost document
administration API. Metadata and enabled-state changes are isolated to that
provider and remain in memory, so restarting the demo restores the seeded
catalogs.

The outer launcher also prints a human-only localhost demo-control URL. Open it to switch
between Alice, Bob, and Carol; upload CSV files; enable or disable documents;
and create, edit, or delete exact eDocs policy rules. The page delegates to
the providers' mutable catalogs and controller policy stores, so changes take
effect immediately and remain isolated by provider. This separate control
service is deliberately unauthenticated and intended only for the localhost
demo. It is not exposed through the agent-facing MCP server.

The page's Sentinel tab shows registered resource bindings, authoritative
controllers, functions, and materialized dataflows. Refresh it after an
invocation to show the resulting provenance state. Its function table displays
each function's ID, description, and SQL.

The dashboard and agent-facing `register_edocs_function` tool can upload a
schema-conforming function to the demo's shared mutable registry. The current
demo runtime accepts one read-only SQL statement and computes the immutable
descriptor digest server-side. Registration installs the function for all
three providers but deliberately creates no invocation policy. A provider
owner must add a matching exact-dataflow policy before the function can run on
that provider's document. Policy cards summarize the allowed function,
source, destination, and document; selector-based editing is hidden until
requested, and only function-specific arguments remain JSON.

Each provider tab repeats the shared function table and adds that provider's
current policy status. Functions with no matching provider policy are labeled
“No policy — invocation denied,” making the authorization boundary visible
before demonstrating the rejected call.

Alice also starts with a future-output policy. Before any query result exists,
it permits `identity@1` from Producer to Carol over any derived eDoc whose
trusted producer is Alice's exact seeded `query_table@1` dataflow. It does not
permit the equivalent Bob destination. The policy stores an
`OutputOf(producer)` selector rather than guessing a future eDoc ID.

Issuing authorization no longer marks a dataflow as materialized. After a
provider function completes successfully, the provider records a derived eDoc
with a unique opaque ID, the exact producer dataflow, output digest, Producer as
custodian, and inherited controllers. The Sentinel dashboard shows these
derived eDocs and their provenance. Alice's future-output rule begins matching
the concrete derived ID only after this registration. The derived eDoc is not
discoverable to peers until the custodian agent publishes it with
`publish_derived_edoc`, which catalogs it on that agent's resource server and
registers the inherited controllers with the Sentinel registry API.

In either coding agent, ask:

```text
List the available providers, list Alice's resources, list the registered
functions, then use query_table@1 on Alice's discovered resource with:
{"statement":"SELECT name, department FROM document WHERE department = ? ORDER BY name","parameters":["engineering"]}
```

The coding agent calls the local stdio bridge. The bridge obtains an
agent-signed proactive resource token, and displays an MCP elicitation
containing the Person Server-verified function, opaque eDoc, exact arguments,
agents, resource, Sentinel, and controllers. After approval and Sentinel
authorization, the resource verifies the invocation digest and executes it
against its own DuckDB instance.

`run_infra.sh` keeps the localhost services up until you stop it with Ctrl-C;
agent sessions from `run_new_agent.sh` exit independently. The
`EDOCS_PERSON=alice` login and generated `.demo-state/` credentials are
strictly demo-only; they are not a production person-authentication design.
