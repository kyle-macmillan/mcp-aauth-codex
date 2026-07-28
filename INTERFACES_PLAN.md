# Multi-provider eDocs Demo and Administration Interfaces

## Current implementation handoff (2026-07-27)

The exact-invocation DuckDB vertical slice is complete on `main` at
`5f9d97b`. The demo now uses proactive, agent-signed authorization; opaque
eDoc IDs; exact opaque JSON argument binding; resource-side final-token
verification; and `query_table@1` execution against the resource's own
per-eDoc DuckDB database. `scripts/setup_demo_db.py` resets and seeds the demo.
AAuth does not validate function schemas or SQL.

Related implementation commits are `aauth/55673bc` on `edocs-demo` and
`mcp-aauth/12d909e` on `edocs-demo`; the preceding exact-binding commits are
`aauth/4ff9f98` and `mcp-aauth/ed734ed`. The local `python-sdk` fork remains
unchanged on `aauth-auth-middleware-hook`. Verification is 212 passed and
1 skipped in `aauth`, 68 passed in `mcp-aauth`, and 19 passed here.

The next session should start here by generalizing the working resource into
Alice, Bob, and Carol provider/resource/AS domains. Implement provider-qualified
catalog discovery, opaque routing, and isolation tests before dashboards.
Reuse the existing `FunctionLoader` boundary and authorization/execution flow;
do not add an AAuth-side query language, SQL validation, or function-schema
enforcement. Filenames remain catalog metadata rather than identifiers.

## Summary

Build the next demo milestone in two gated stages:

1. Extend the reusable eDocs authorization model so an authorization binds the
   exact canonical JSON arguments of a server-side function.
2. Replace the single hard-coded resource with Alice, Bob, and Carol, each
   owning a multi-file MCP catalog, an Access Server (AS), and a combined
   administration dashboard.

The Sentinel remains the authoritative registry and audit point. Generic
`mcp-aauth` and the forked MCP SDK must remain free of eDocs-specific behavior.

## 1. Exact-argument authorization foundation (complete)

Complete and test this stage before starting the dashboards.

- `Dataflow` retains readable canonical arguments and derives their
  domain-separated digest. Existing four-field construction represents `{}`.
- Resource tokens carry the full `function_args` object and its digest.
  Controller, conditional, and final tokens carry only the digest.
- Person Server consent shows the full object from the verified resource token.
- AAuth treats function arguments as opaque JSON. It does not apply function
  schemas, defaults, SQL validation, or application semantics.
- Include the normalized arguments in Person Server consent review and the
  trusted Codex approval prompt.
- Function descriptors carry input JSON Schema as descriptive metadata for
  resources, discovery, and dashboards. The resource owns argument handling
  and execution.
- Generalize the controller-policy dependency to an evaluator interface.
  Existing immutable `ControllerPolicy` instances remain supported, while the
  demo can supply a policy store that changes at runtime.
- Continue treating final-token issuance as materialization. Signed execution
  receipts remain deferred for this milestone.
- The complete `aauth` and `mcp-aauth` suites pass with empty-argument and
  argument-tampering coverage.

The current vertical slice is `query_table@1`: a resource-owned DuckDB
database, opaque eDoc identity, proactive authorization, exact policy, trusted
consent, and execution only after Sentinel authorization.

## 2. Demo topology and MCP behavior

Run one shared Agent Provider, Person Server, Sentinel, and Codex stdio proxy,
plus:

- Alice's AS and MCP resource server;
- Bob's AS and MCP resource server;
- Carol's AS and MCP resource server; and
- one local administration web application.

Each provider has a distinct source agent, resource issuer, signing key, and
trusted resource-owner-AS binding. Resource tokens use an empty advisory
controller list so the Sentinel selects the provider's AS from trusted
configuration.

### Catalogs

- Each provider may upload multiple CSV, Parquet, and PDF eDocs.
- Enabled files appear through that provider's MCP `resources/list`.
- The Codex proxy aggregates the three catalogs under provider-qualified URIs:
  `edoc://alice/...`, `edoc://bob/...`, and `edoc://carol/...`.
- `resources/read` returns only title, description, media type, provider, and
  compatible function schemas. It never returns the underlying file bytes.
- Catalog discovery does not require person or AS approval.

### Authorization and invocation

- Each resource server exposes an agent-signed `/authorize` endpoint accepting
  the selected eDoc, function ID, and function arguments.
- The endpoint verifies that the eDoc is enabled and the function is deployed,
  then issues a resource token for the exact opaque JSON arguments. It neither
  executes the function nor applies defaults.
- The Codex-facing invocation becomes
  `invoke_edocs_function(resource_uri, function_id, arguments)`.
- The proxy obtains the resource token, runs the existing PS/Sentinel/AS
  consent flow, and invokes the correct remote MCP server with the final token.
- Before execution, the resource verifies provider, source agent, eDoc,
  function, normalized arguments, destination agent, issuer, audience, and
  proof key.

### Initial functions

`query_table@1`, for the initial DuckDB-backed CSV/Parquet vertical slice:

- a SQL statement;
- a JSON array of bound parameters; and
- resource-controlled result limits.

The resource resolves the opaque eDoc ID to its own DuckDB instance and
executes only after checking the final token's exact argument digest. AAuth
does not parse SQL. Implementations may come from any trusted source so long
as the resource deploys the immutable descriptor used by the AS and Sentinel.

`search_pdf_text@1`, for PDF:

- non-empty search text;
- case-sensitive or case-insensitive matching;
- optional inclusive page bounds; and
- a match limit from 1 through 50.

Both functions execute entirely on the resource server and return structured
derived results rather than raw files.

## 3. Administration interfaces

Use server-rendered Flask pages with JSON endpoints underneath. These are
localhost demo interfaces with no login and no durable state.

### Provider dashboards

Alice, Bob, and Carol each receive one combined dashboard. It is a façade over
two separate authorities:

- file, metadata, enablement, and function changes go to the provider's
  resource service;
- policy changes go to the provider's AS.

The dashboard allows its provider to:

- upload validated CSV, Parquet, and PDF files;
- edit title and description;
- enable or disable an eDoc;
- enable compatible server-side functions;
- inspect AS and resource status; and
- create, edit, or remove exact dataflow allow rules.

Policy is exact-match and default-deny. The editor supports both `X` and
conditional `X | Y` rules. It renders guided fields from the selected
function's schema, including column/filter controls for tables and search/page
controls for PDFs. The complete canonical target and optional prerequisite are
shown before saving.

### Sentinel dashboard

The Sentinel operator can:

- register or change provider-to-resource/AS bindings;
- enable registered function descriptors;
- inspect authoritative per-eDoc controller mappings;
- inspect controller outcomes, denials, issued authorizations, and
  materialized dataflows; and
- invalidate cached controller mappings automatically when a provider binding
  changes.

The Sentinel interface never edits an AS policy or fabricates provenance.

### Demo state and uploads

- Reset catalogs, policies, provenance, audit history, and uploaded files on
  every launch.
- Seed representative files and successful example policies.
- Use generated storage names and provider-specific temporary directories.
- Validate extension, media type, parser compatibility, size, eDoc ID,
  function selection, and policy inputs.
- Support enable/disable but not permanent file deletion.

## 4. Tests and documentation

- Test canonical equivalence, argument tampering, schema defaults, consent
  rendering, conditional prerequisites, and empty-argument compatibility.
- Test upload validation, metadata-only MCP reads, catalog isolation,
  enable/disable behavior, and both server-side function engines.
- Exercise successful flows through Alice, Bob, and Carol and prove that only
  the owning AS is contacted.
- Test default denial, exact-argument mismatch, disabled files/functions,
  wrong providers, controller rebinding, and conditional policy behavior.
- Test provider and Sentinel dashboard APIs, provider isolation, policy CRUD,
  and reset-on-restart behavior.
- Add a real Codex stdio-process test covering aggregated discovery, approval,
  routing, and server-side execution.
- Update the demo README with the topology, dashboard URLs, and sample prompts.

## Assumptions and boundaries

- A single Codex MCP entry aggregates the three provider catalogs.
- Every provider accepts all three supported file formats.
- Policy arguments are exact; there are no wildcards, boolean expressions, or
  attribute-based rules in this milestone.
- Final-token issuance, rather than confirmed execution, continues to satisfy
  a materialization prerequisite.
- No persistent database, production administrator authentication, raw file
  download, or permanent deletion is included.
- Do not modify `mcp-aauth` or `mcp-python-sdk` for eDocs-specific behavior.
  Reuse their existing public extension and authentication surfaces.
