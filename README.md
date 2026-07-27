# eDocs AAuth Codex plugin

This plugin exposes a local stdio MCP server to Codex. The server acts as an
AAuth-aware client of the remote eDocs Streamable HTTP MCP server. Codex does
not need a custom HTTP authentication implementation.

Set these variables before starting Codex:

- `EDOCS_MCP_URL`: remote Streamable HTTP MCP endpoint, including `/mcp`
- `EDOCS_AGENT_TOKEN_FILE`: path to the agent token
- `EDOCS_AGENT_KEY_FILE`: path to the agent's private JWK
- `EDOCS_PERSON`: prototype Person Server login identity used by the demo
The plugin provides `invoke_edocs_function(edoc_id, function_id)`. A remote
AAuth resource challenge is exchanged at the Person Server. If consent is
required, the proxy asks Codex to display an MCP elicitation containing only
the PS-verified eDocs claims. A grant is submitted to the PS, the proxy polls
for the final resource-scoped token, and the original MCP request is retried.

`EDOCS_PERSON` is only the current prototype login bridge. Production person
authentication should provide an already-authenticated PS session and should
not pass credentials through an MCP elicitation.

The included launcher reuses the adjacent `mcp-aauth` development environment
for this workspace. Outside this checkout, install the project dependencies in
the Python environment selected by `PYTHON`.

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

In Codex, ask:

```text
Use identity@1 on eDoc doc-123.
```

Codex calls the local stdio proxy. The remote eDocs server issues an AAuth
resource challenge, and the proxy displays an MCP elicitation containing the
Person Server-verified function, eDoc, agents, resource, Sentinel, and
controllers. Approving it completes the exchange and returns `hello`.

The launcher stops the localhost services when Codex exits. The
`EDOCS_PERSON=alice` login and generated `.demo-state/` credentials are
strictly demo-only; they are not a production person-authentication design.
