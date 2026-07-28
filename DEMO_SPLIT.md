# Split `run_demo.sh` into infra + new-agent scripts

## Context

`scripts/run_demo.sh` currently does everything in one process: seeds the
DuckDB catalogs, builds and starts *every* AAuth component (Agent Provider,
Person Server, Sentinel, three Access Servers, three provider resource
servers, the control panel), writes credentials for three fixed agent
identities (producer/carol/bob), then hands off to an interactive coding
agent session for the `producer` identity.

The goal is two independently runnable pieces instead:

1. **Infra script** — Alice/Bob/Carol's resource servers, their Access
   Servers, the Sentinel, and the control panel UI. Long-running, Ctrl-C to
   stop. No Person Server, no agents.
2. **New-agent script** — spins up a Person Server and mints a brand-new
   agent identity (an outside party trying to access Alice/Bob/Carol's
   data — not necessarily producer/carol/bob), then launches the
   interactive coding-agent session for it, showing the elicitation/consent
   flow and whatever the Sentinel's existing policies decide (most likely
   denial for an unrecognized identity — that's the point).

Since arbitrary new agents need to be mintable at runtime, not just the three
fixed demo roles, `run_demo.sh` doesn't stay byte-for-byte unchanged — it
becomes a thin backward-compatible wrapper. That avoids running two parallel
"mint an agent identity" implementations (the old `DemoStack`
producer/carol/bob path, and a new one for arbitrary parties) that would
otherwise have to be kept behaviorally in sync. Instead there's exactly one
mechanism, and the old scripts become thin callers of it.

## Trust-dependency analysis

The split only works if it's compatible with how keys get verified:

- The **provider resource servers** (`build_provider_server` in
  `mcp-edocs-provider`) use a purely *static* key resolver
  (`_static_and_remote_resolver` in `demo.py`) — built once at construction
  from `{ap_pubkey, sentinel_pubkey, *as_pubkeys}`. It never does a live
  HTTP fetch. This is the one place that needs a key to be known **before**
  the thing that owns it (a new agent's issuer, AP) exists in this split.
- **Access Servers and Sentinel** (`create_as`, `create_sentinel` in
  `aauth/src/aauth_edocs/asrv.py` and `sentinel.py`) build their own
  `JwksResolver`, which does real HTTP `.well-known`/`jwks.json` fetches at
  verification time. They don't need any key pinned in advance.
- **Person Server** (`create_ps`) also uses `JwksResolver` — same story.
- `SentinelRegistry.resource_bindings` only stores `source_ps` as a **URL
  string** (`urls.ps`), never a key. So the Sentinel doesn't need to know
  anything about a new party's PS ahead of time either.

Net effect: the only key that must be **shared and stable** across processes
is the **Agent Provider (AP) keypair** — the resource servers' static
resolver bakes in AP's public key at construction, and any agent's token
(fixed role or brand-new party) still has to be issued by that same AP to
pass authentication (only the *authorization* step — Sentinel/AS policy
matching — is where an unrecognized new identity is expected to get
rejected). Every other key (Sentinel, each AS, each resource server, PS,
the agent's own key) can keep being freshly generated per-process, since
verification of those happens via live resolution.

## Approach

### 1. `src/mcp_edocs_agent/demo.py` — trim to pure infra

- Remove PS construction/start, `DEMO_AGENTS` token minting, and the
  per-agent credential-writing loop (plus the `agent.jwk`/`agent.token`/
  `demo.env` legacy-alias block) from `DemoStack`/`_build()`/`_write_state()`.
  `_write_state` keeps writing `providers.json` only. `wait_ready()` drops
  the PS `.well-known` URL from its checklist.
- `DemoStack` keeps: AP's metadata Flask app (`.well-known`/`jwks.json`),
  Sentinel, all three Access Servers, all three provider resource servers,
  the control panel, and the policy/function seeding — unchanged behavior
  for all of that.
- **AP key persistence**: replace the unconditional
  `SigningKey.generate("ap")` in `_build()`'s `keys = {...}` comprehension
  with a small helper, `_load_or_generate_key(path, kid) -> SigningKey`,
  used only for `"ap"`. Loads `.demo-state/keys/ap.jwk` via
  `SigningKey.from_private_jwk(json.loads(...))` if present (mode 0600),
  else generates and persists it — same "load if present else create"
  convention already used for catalogs in `_deployment`.
- `main()`/CLI stays as today (`--state-dir`), just describes less.
- `PRODUCER_AGENT`/`CAROL_RECIPIENT_AGENT`/`BOB_RECIPIENT_AGENT`
  constants and the Sentinel policy seeding that reference them
  (`self.policies[...]`, Alice's future-output rule) are untouched — a
  policy can be seeded for an identity before that identity's agent process
  exists; that's how the fixed-role demo scenarios keep working once
  producer/carol/bob get minted via the new mechanism below with matching
  agent-id strings.

### 2. `src/mcp_edocs_agent/new_agent.py` (new module) — the one agent-minting path

CLI: `--state-dir`, `--role` (filename-safe, validated the same way
`run_coding_agent.sh` already validates `EDOCS_DEMO_AGENT_ROLE`),
`--agent-id` (defaults to `aauth:<role>@newparty.local` if omitted — the
fixed-role wrapper scripts override this explicitly to reproduce today's
exact identities), `--person` (defaults to `--role`'s value).

At startup:
- Load `DemoUrls()` (same fixed ports `demo.py` uses) and require
  `.demo-state/ready` to exist — clear error pointing at the infra script
  otherwise.
- Load the persisted AP key from `.demo-state/keys/ap.jwk` — clear error if
  missing, same reasoning.
- Generate a fresh key for the new agent and a fresh PS key.
- `create_ps(urls.ps, key=ps_key, person=<--person>, ...)`, start it via
  the existing `FlaskService` class from `demo.py`.
- `issue_agent_token(issuer=urls.ap, agent=<--agent-id>, agent_jwk=agent_key.public_jwk, ps=urls.ps, key=ap_key)`.
- Write this one identity's credential files (`.jwk`/`.token`/`.env`/
  `.claude-mcp.json`) under `.demo-state/agents/<role>.*` — same file shapes
  `_write_state` used to produce, just for one role instead of a fixed loop.
- Wait for PS's `.well-known` endpoint, then write
  `.demo-state/agents/<role>.ready`.
- Print the new agent's identity and the control panel URL.
- Block on `SIGINT`/`SIGTERM` (mirror `demo.py::main()`'s
  `signal.signal` + `threading.Event` pattern), stop PS cleanly on exit.

### 3. `scripts/run_infra.sh` (new)

Run `setup_demo_db.py`, then
`exec ... python -m mcp_edocs_agent.demo --state-dir ...` directly in the
foreground — no background/trap dance needed since nothing runs after it.
`main()`'s existing SIGINT/SIGTERM handling gives Ctrl-C-to-stop for free.

### 4. `scripts/run_new_agent.sh` (new)

`scripts/run_new_agent.sh CLIENT ROLE [--agent-id ID] [--person PERSON] [--display-name NAME] [-- CLIENT_ARGS...]`

- Check `.demo-state/ready` exists (infra running) — error out with
  instructions to run `run_infra.sh` first otherwise.
- Run `python -m mcp_edocs_agent.new_agent --state-dir ... --role ... [--agent-id ...] [--person ...]`
  in the background, mirroring `run_demo.sh`'s existing
  background-PID + `trap cleanup EXIT INT TERM` pattern.
- Wait for `.demo-state/agents/<role>.ready`.
- `exec scripts/run_coding_agent.sh <client> .demo-state/agents/<role>.env "<display-name>" -- CLIENT_ARGS...`
  — reuses `run_coding_agent.sh` completely unchanged (workspace isolation,
  `--disallowedTools`, env stripping all still apply).

### 5. `scripts/run_demo.sh` and `scripts/run_multi_agent_demo.sh` — become thin wrappers

Both keep their existing `--client codex|claude` UX and printed identity
strings, but their bodies shrink to composition of the two new scripts
instead of their own copy of the backend-startup logic:

- `run_demo.sh`: backgrounds `scripts/run_infra.sh` (trap to clean up on
  exit), waits for `.demo-state/ready`, then
  `exec scripts/run_new_agent.sh "${client}" producer --agent-id aauth:producer@demo.local --person alice --display-name Producer -- "$@"`.
- `run_multi_agent_demo.sh`: same backgrounded-infra step, then each tmux
  pane runs `scripts/run_new_agent.sh "${client}" <role> --agent-id aauth:<role>@demo.local --person <role|alice> --display-name <Name>`
  instead of calling `run_coding_agent.sh` directly (producer keeps
  `--person alice` to match today; carol/bob use their own role as person,
  matching the existing printed identity strings).

Both scripts keep working exactly as documented from the outside; only
their internals change. `tests/test_launchers.py` and `tests/test_demo.py`
get updated for the parts that assert on now-removed `DemoStack` behavior
(PS construction, `DEMO_AGENTS` minting, the legacy `agent.jwk`/`agent.token`/
`demo.env` aliases).

## Verification

- `bash -n` on all four touched/new shell scripts.
- Run the existing suite (`uv run pytest`) after the `demo.py` changes;
  update the tests that assert on removed behavior as noted above.
- End-to-end manual check: run `scripts/run_infra.sh` in one terminal
  (confirm control panel at `http://127.0.0.1:8721/demo` loads, Ctrl-C
  stops it cleanly); in a second terminal with infra still up, run
  `scripts/run_new_agent.sh claude mallory` and confirm it drops into an
  interactive session where `list_providers`/`list_resources` succeed but
  `query_table@1` against Alice's doc is denied (no policy names the new
  identity) — the actual scenario this is meant to demonstrate. Then
  re-run `scripts/run_demo.sh` standalone and confirm the producer flow
  still works exactly as before (query_table@1 with the exact seeded
  arguments succeeds).
