# eDocs AAuth Codex plugin

This plugin exposes a local stdio MCP server to Codex. The server acts as an
AAuth-aware client of the remote eDocs Streamable HTTP MCP server. Codex does
not need a custom HTTP authentication implementation.

Set these variables before starting Codex:

- `EDOCS_PROVIDER_FILE`: path to the private provider-directory JSON file
- `EDOCS_AGENT_TOKEN_FILE`: path to the agent token
- `EDOCS_AGENT_KEY_FILE`: path to the agent's private JWK
- `EDOCS_PERSON`: prototype Person Server login identity used by the demo

The plugin provides `list_providers()`,
`list_resources(provider_id)`, and
`invoke_edocs_function(resource_uri, function_id, arguments)`.
Provider listing comes from the proxy's directory, while resource listing is
forwarded live to the selected provider's MCP server without starting a
consent flow. The selected provider ID is repeated at authorization and
execution, so a directory entry that points Alice at Bob's endpoint is rejected
before consent or materialization. A remote AAuth resource challenge is
exchanged at the Person Server. If consent is
required, the proxy asks Codex to display an MCP elicitation containing only
the PS-verified eDocs claims. A grant is submitted to the PS, the proxy polls
for the final resource-scoped token, and the original MCP request is retried.

`EDOCS_PERSON` is only the current prototype login bridge. Production person
authentication should provide an already-authenticated PS session and should
not pass credentials through an MCP elicitation.

Install the locked local development environment with `uv sync --frozen`.
The demo launcher uses this project's `.venv`, including DuckDB and the
editable adjacent dependencies. The lightweight stdio proxy launcher reuses
the adjacent `mcp-aauth` environment, which already contains the local MCP SDK
fork.

## Live Codex demo

From this directory, run:

```bash
scripts/run_demo.sh
```

For three independent Codex sessions, install `tmux` and run:

```bash
scripts/run_multi_agent_demo.sh
```

The multi-agent launcher opens tiled Producer, Carol, and Bob panes backed by
distinct private keys and agent tokens:

- Producer: `aauth:codex@demo.local`
- Carol: `aauth:carol@demo.local`
- Bob: `aauth:bob@demo.local`

All three sessions share the same provider directory, function registry,
Sentinel, and control panel, while each proxy receives only its own credential
files. The generated configurations live under
`.demo-state/agents/{producer,carol,bob}.{env,jwk,token}` with mode `0600`.
The existing `run_demo.sh` remains the single-window Producer launcher.

Carol and Bob can currently demonstrate independent identity and public
provider discovery. Invoking the producer's derived output from those sessions
is intentionally not wired yet: the derived-resource service and dynamic
destination handling remain deferred.

The launcher composes the shared AAuth Agent Provider, Person Server, and
Sentinel with three independent provider domains. Alice, Bob, and Carol each
have their own Access Server, MCP resource server, source agent, signing key,
catalog, and DuckDB storage. It generates an ephemeral demo agent key, token,
and private `providers.json` directory under `.demo-state/`, then starts
Codex with a one-run `mcp_servers.edocs-aauth` configuration. It also enables
the `mcp_elicitations` approval category for that run so Codex can display the
eDocs consent form. It does not modify your user-level Codex configuration.

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

The launcher also prints a localhost demo-control URL. Open it to switch
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

The dashboard and the Codex-facing `register_edocs_function` tool can upload a
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
it permits `identity@1` from Codex to Carol over any derived eDoc whose trusted
producer is Alice's exact seeded `query_table@1` dataflow. It does not permit
the equivalent Bob destination. The policy stores an `OutputOf(producer)`
selector rather than guessing a future eDoc ID.

Issuing authorization no longer marks a dataflow as materialized. After a
provider function completes successfully, the provider records a derived eDoc
with a unique opaque ID, the exact producer dataflow, output digest, Codex as
custodian, and inherited controllers. The Sentinel dashboard shows these
derived eDocs and their provenance. Alice's future-output rule begins matching
the concrete derived ID only after this registration.

In Codex, ask:

```text
List the available providers, list Alice's resources, then use query_table@1
on edoc://alice/doc_01JDEMO7F3A with:
{"statement":"SELECT name, department FROM document WHERE department = ? ORDER BY name","parameters":["engineering"]}
```

Codex calls the local stdio proxy. The proxy obtains an agent-signed proactive
resource token, and displays an MCP elicitation containing the Person
Server-verified function, opaque eDoc, exact arguments, agents, resource,
Sentinel, and controllers. After approval and Sentinel authorization, the
resource verifies the invocation digest and executes it against its own
DuckDB instance.

The launcher stops the localhost services when Codex exits. The
`EDOCS_PERSON=alice` login and generated `.demo-state/` credentials are
strictly demo-only; they are not a production person-authentication design.
