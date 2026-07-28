# eDocs AAuth Codex plugin

This plugin exposes a local stdio MCP server to Codex. The server acts as an
AAuth-aware client of the remote eDocs Streamable HTTP MCP server. Codex does
not need a custom HTTP authentication implementation.

Set these variables before starting Codex:

- `EDOCS_MCP_URL`: remote Streamable HTTP MCP endpoint, including `/mcp`
- `EDOCS_AGENT_TOKEN_FILE`: path to the agent token
- `EDOCS_AGENT_KEY_FILE`: path to the agent's private JWK
- `EDOCS_PERSON`: prototype Person Server login identity used by the demo
The plugin provides
`invoke_edocs_function(resource_uri, function_id, arguments)`. A remote
AAuth resource challenge is exchanged at the Person Server. If consent is
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

The launcher composes the existing AAuth Agent Provider, Person Server,
Sentinel, two controller authorization servers, and the AAuth-protected eDocs
Streamable HTTP MCP server on localhost. It generates an ephemeral demo agent
key and token under `.demo-state/`, exports their file paths to the plugin, and
then starts Codex with a one-run `mcp_servers.edocs-aauth` configuration. It
also enables the `mcp_elicitations` approval category for that run so Codex can
display the eDocs consent form. It does not modify your user-level Codex
configuration.

The demo setup script resets a resource-owned DuckDB database, assigns its
seeded document the opaque URI `edoc://demo/doc_01JDEMO7F3A`, and exposes
`query_table@1`. Filenames remain catalog metadata and are not authorization
identities.

In Codex, ask:

```text
Use query_table@1 on edoc://demo/doc_01JDEMO7F3A with:
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
