# Changelog

All notable changes to OrionBelt Semantic Layer MCP are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [2.19.0] — 2026-07-05

Tracks OrionBelt Semantic Layer API **v2.19.0**. This is a version-tracking
release: no MCP tool is added, removed, or changed, and no request/response
shape the MCP wraps is altered.

The API's v2.19.0 work adds **auto-synthesized row-count measures** — every
countable `dataObject` now yields a grain-anchored row-count measure (default
`"Sales Count"`), a governed named measure referenced in `select.measures`
rather than an ad-hoc `COUNT(*)`. New authoring knobs (`countable` /
`countLabel` per object, `exposeCounts` / `countLabelPattern` at model level)
live in the OBML JSON Schema, which the MCP serves live via
`get_json_schema("obml")`, so the new knobs surface automatically. The derived
count measures are computed on read and flow through the schema, explain,
search, and measure endpoints the MCP already wraps (the API switched those
routes from `model.measures` to `effective_measures`) — the `MeasureDetail` /
`SchemaResponse` shapes are unchanged, so the counts appear with no MCP code
change. The remaining v2.19.0 changes are on surfaces the MCP does not wrap:
two **pgwire SQL-injection fixes** in the Postgres wire's extended-protocol
parameter substitution, and a `format_values` docstring clarification (the flag
now also applies to the Arrow transport, which the MCP does not use).

The bump keeps the MCP's `major.minor` aligned with the API, which the startup
compatibility check requires.

## [2.18.0] — 2026-07-03

Tracks OrionBelt Semantic Layer API **v2.18.0**. This is a version-tracking
release: no MCP tool is added, removed, or changed, and no request/response
shape the MCP wraps is altered.

The API's v2.18.0 work adds an **Arrow IPC result format** to the execute
endpoints — `format=arrow` (or an Arrow `Accept` header) returns a typed,
locale-neutral `application/vnd.apache.arrow.stream`, gzip'd per the client's
`Accept-Encoding`, and the result cache now stores Arrow IPC internally. Arrow
is a binary stream aimed at the interactive UI and data clients; it is not
exposed through the MCP surface, which returns text to an LLM and continues to
advertise `output_format` of `json` (default) or `tsv`. The remaining v2.18.0
changes (interactive filter/sort in the UI, measure/metric HAVING click-filters,
the YAML editor "Jump to" navigator, cache codec internals) are on surfaces the
MCP does not wrap.

The bump keeps the MCP's `major.minor` aligned with the API, which the startup
compatibility check requires.

## [2.17.0] — 2026-06-28

Tracks OrionBelt Semantic Layer API **v2.17.0**. This is a version-tracking
release: no MCP tool is added, removed, or changed, and no request/response
shape the MCP wraps is altered. The API's v2.17.0 work does not touch the REST
surface the MCP delegates to — it is a local-first `obsl` command-line interface
plus Docker Hub publishing/image-rename CI, none of which the MCP wraps.

The bump keeps the MCP's `major.minor` aligned with the API, which the startup
compatibility check requires.

## [2.16.0] — 2026-06-23

Tracks OrionBelt Semantic Layer API **v2.16.0**. This is a version-tracking
release: no MCP tool is added, removed, or changed, and no request/response
shape the MCP wraps is altered. The API's v2.16.0 work is internal or on
surfaces the MCP passes through verbatim:

- **OBML and the QueryObject are now camelCase-only contracts.** The duplicate
  snake_case spellings were dropped from both JSON Schemas (OBML
  `max_staleness` / `intent_tags`; the query `order_by` is now `orderBy`), and
  **model-load and query endpoints now validate payloads against the published
  JSON Schema, returning `422` on a violation** (snake_case keys, a string
  `version`, or uppercase enum values are rejected). The MCP never authors OBML
  or rewrites query keys — it forwards the host's payload as-is — so this is
  enforced host-side; a `422` surfaces through the normal API-error path. The
  authoritative schema served by `get_json_schema` is fetched live from the
  API, so it already reflects the camelCase-only contract.

### Changed

- **`execute_query` docs use canonical camelCase.** The worked `query_json`
  example and the query-writing reference switched the lone snake_case query
  key `order_by` to `orderBy`, matching the now camelCase-only query schema.
  (Response-reading code that consumes the API's snake_case response envelopes
  — `data_objects`, `path_name`, `result_type`, etc. — is unchanged; only the
  host-facing query payload examples moved.)
- **Internal refactors and CI quality gates** (OBML contract manifest + drift
  gate, explicit compiler wrapper passes, service-layer extraction, module
  splits, architecture/coverage guards) with no SQL or endpoint behavior change.

The bump keeps the MCP's `major.minor` aligned with the API, which the startup
compatibility check requires.

## [2.15.0] — 2026-06-18

Tracks OrionBelt Semantic Layer API **v2.15.0**. This is a version-tracking
release: the API's v2.15.0 changes are all on surfaces the MCP does not wrap
(Postgres wire protocol DECIMAL→NUMERIC reporting and Dremio federation
pushdown, the Gradio UI, the demo stack) plus internal compiler fixes (HAVING
filters on period-over-period metrics are now applied; clearer errors for
incompatible-artefact combinations), so no MCP tool is added or changed. The
bump keeps the MCP's `major.minor` aligned with the API, which the startup
compatibility check requires.

## [2.14.0] — 2026-06-16

Tracks OrionBelt Semantic Layer API **v2.14.0**, which adds **Artefacts
Composability Resolution (ACR)** — a `composables` endpoint that, for the query
built so far, reports which other artefacts can still be added and yield a
valid, fanout-free result. This release wraps that endpoint as an MCP tool.
(The API skipped a public v2.13; the MCP version tracks the API.)

### Added

- **`find_composables` tool** (model-discovery, run-only). Given an anchor —
  an in-progress query (`query_json`) or one or more named artefacts
  (`anchors`, optionally narrowed by `anchor_type`) — returns the directly
  composable `dimensions`, `measures`, and `metrics`, plus the `cflMeasures` /
  `cflMetrics` combinable only through the Composite Fact Layer. Routes to the
  session-scoped `POST/GET /v1/sessions/{id}/models/{mid}/composables` in
  multi-model mode and the top-level `/v1/composables` shortcut in single-model
  mode. ACR reuses the planner's join-graph reachability, so anything reported
  is guaranteed to compile.

## [2.12.0] — 2026-06-14

Tracks OrionBelt Semantic Layer API **v2.12.0**, which gates every `/v1/*`
endpoint behind authentication when the API runs with `AUTH_MODE=api_key`.
This release teaches the MCP to authenticate so it keeps working once the API
turns auth on. Backward compatible: with no credential configured, behaviour
against an unauthenticated API (`AUTH_MODE=none`) is unchanged.

### Added

- **`API_KEY` setting.** When set, the credential is sent on every API request.
  Required only when the API runs with `AUTH_MODE=api_key`; leave unset for
  unauthenticated deployments.
- **`API_KEY_HEADER` setting** (default `X-API-Key`). The header the credential
  rides in; must match the API's `API_KEY_HEADER`. Set to `Authorization` (with
  a `Bearer ` value prefix) to use the API's bearer-token path instead.

### Changed

- **401/403 API responses raise an actionable error.** Authentication failures
  now point the operator at `API_KEY` / `API_KEY_HEADER` rather than surfacing a
  generic error.
- **Upgraded FastMCP to 3.4.x** (floor raised to `>=3.4,<4`), which pulls in
  Starlette 1.x. No code changes required — the middleware, tool, resource, and
  transport surfaces this server uses are unchanged.

## [2.11.0] — 2026-06-13

Tracks OrionBelt Semantic Layer API **v2.11.0**. Version-lockstep bump — no
tool signatures or response shapes change. The API release adds Dremio
federation filter pushdown, a cleaner federated catalog, and a federation
demo, alongside compiler fixes. All of these reach this MCP transparently
through the unchanged REST endpoints it delegates to.

### Changed

- **Compatibility raised to API v2.11.x.** The startup version gate (which
  requires a matching `major.minor`) now expects API `2.11.x`; running against
  `2.10.x` is no longer supported.

### Notes

- **Compiler correctness improvements flow through `execute_query`.** The API
  now supports multiple period-over-period offsets in a single query (e.g.
  MoM + YoY), fixes a cross-fact metric column leak, and resolves
  period-over-period on Dremio (reserved-word alias). These change the
  generated SQL only; the MCP relays results unchanged.
- **pgwire (Postgres wire protocol) changes are out of scope for this MCP.**
  The federated catalog and router updates target the BI-facing pgwire
  interface, which this MCP does not wrap.

## [2.10.0] — 2026-06-12

Tracks OrionBelt Semantic Layer API **v2.10.0**. Version-lockstep bump — no
tool signatures change. The API refactored the OBML ⇆ OSI converter into a
standalone optional `osi-orionbelt` package; the changes are transparent to
this MCP, which delegates conversion over the unchanged `/models/from-osi` and
`/models/{id}/osi` endpoints.

### Changed

- **Compatibility raised to API v2.10.x.** The startup version gate (which
  requires a matching `major.minor`) now expects API `2.10.x`; running against
  `2.9.x` is no longer supported.

### Notes

- **OSI conversion is now an optional API extra.** If the API is deployed
  without the converter (`pip install 'orionbelt-semantic-layer[osi]'`), the
  OSI endpoints return **503**; `load_model(osi_yaml=…)` and
  `export_model_to_osi` surface this as a clear `API error (503): OSI
  conversion is unavailable…` `ToolError`. The shipped API images bundle the
  converter, so this does not affect standard deployments.
- **OSI vendor-extension round-tripping** is now preserved at model, dataset,
  field, and measure levels, and OrionBelt/OSI-native payloads are retagged
  (`ORIONBELT`/`OSI`, legacy `COMMON`/`OBSL` still accepted). This changes only
  the *content* of OSI YAML passed through the conversion tools, which this MCP
  forwards verbatim.

## [2.9.0] — 2026-06-11

Tracks OrionBelt Semantic Layer API **v2.9.0**.

### Added

- **`export_model_to_osi` gained an `include_ontology` parameter.** When set,
  the export appends the OSI ontology document as a separate artefact (under an
  `--- OSI ONTOLOGY ---` heading) alongside the unchanged core-spec OSI YAML,
  surfacing the new `ontology_yaml` and `ontology_validation` response fields.
- **`describe_model` now surfaces `defaultLocale`** from the model's
  `settings.defaultLocale` (BCP-47 tag driving result value formatting),
  rendered in the SETTINGS block next to `defaultTimezone`.

### Changed

- Bumped the required API version to **2.9.x** (the startup compatibility gate
  matches on major.minor).

## [2.8.5] — 2026-06-09

### Changed

- **Constrained `get_model_diagram` `theme` to a `Literal` enum.** The parameter
  now accepts only the five built-in Mermaid themes (`default`, `dark`,
  `forest`, `neutral`, `base`) instead of a free-form string, so invalid themes
  are rejected at the input boundary and the constraint is published in the
  tool's JSON schema. Tightens schema rigor; no behavior change for valid
  callers (default remains `default`).

### Documentation

- Corrected the advertised tool counts to **14** (single-model) / **18**
  (multi-model), and clarified that 19 distinct tools exist in total with the
  active subset selected by API mode.
- Trimmed trailing whitespace flagged by `git diff --check`.

### Tooling

- Added `scripts/setup-hooks.sh` to install the pre-commit `ruff` format/check
  hook (it was referenced in the README but missing from the repo).

## [2.8.4] — 2026-06-08

### Fixed

- **Tightened load_model docstring to reduce semantic overlap.** Simplified
  the docstring to focus on action and source choice, moving OBML structure
  details to the model argument. Removed reference to "OBML schema and full
  specification" that caused confusion with get_obml_reference.

## [2.8.3] — 2026-06-08

### Fixed

- **Further tool description confusability reduction.** Removed additional
  cross-references to sibling tools that were causing selection errors:
  - `execute_query`: Removed mentions of `get_json_schema`, `describe_model`,
    and `get_example` from main description
  - All model_id args: Changed "id from load_model" to "a loaded model's id"
    across 11 tools to prevent misdirection to `load_model`

## [2.8.2] — 2026-06-08

### Fixed

- **Tool description cross-references causing misdirection.** Removed imperative
  references to other tools from descriptions and error messages that were
  causing LLMs to incorrectly route to referenced tools instead of the intended
  one. Changes:
  - `load_model`: Removed "Call get_json_schema()" and "get_obml_reference()"
    imperatives from description
  - `get_obml_reference`: Removed "IMPORTANT: Call this tool BEFORE" directive
  - Error messages: Replaced "Call load_model" with non-imperative phrasing
  - Tool descriptions: Changed "call this after load_model" to "use this after
    loading a model"

## [2.8.1] — 2026-06-06

### Changed

- **Tool consolidation to cut context bloat and tool-choice noise.** Two pairs
  of tools collapsed into one each; no API change (still aligned with OrionBelt
  Semantic Layer API 2.8). The underlying behaviours are unchanged — only the
  exposed tool surface shrank.
  - **`list_artefacts` merged into `find_artefacts`.** `find_artefacts` now
    takes an optional `query`: with `query` it does the fuzzy ranked search as
    before; without `query` it does the exact, deterministic enumeration that
    `list_artefacts` used to (no args → every artefact; `kind` → that whole
    kind; `name` → that exact artefact). `name` is accepted in the enumeration
    mode and ignored when `query` is set.
  - **`load_model_from_osi` merged into `load_model`.** `load_model` now takes
    an optional `osi_yaml`: pass `model` (OBML JSON) **or** `osi_yaml` (OSI
    YAML, converted to OBML server-side). The two are mutually exclusive, and
    `osi_yaml` cannot be combined with `extends`/`inherits` (a `ToolError`
    spells this out).

### Removed

- **`list_artefacts` and `load_model_from_osi` tools** — subsumed by
  `find_artefacts` and `load_model` respectively (see above). Callers using the
  old names should switch: `list_artefacts(kind, name)` →
  `find_artefacts(kind=…, name=…)` (omit `query`);
  `load_model_from_osi(osi_yaml)` → `load_model(osi_yaml=…)`.

## [2.8.0] — 2026-06-02

### Added

- **`load_model_from_osi` tool.** Loads a model from an Open Semantic
  Interchange (OSI) YAML string: the API converts it to OBML server-side and
  loads it into the session, returning a `model_id`. Surfaces the OSI → OBML
  conversion warnings and advisory OSI-schema validation alongside the standard
  model-load summary. Wraps `POST /v1/sessions/{id}/models/from-osi`
  (multi-model mode; always-on bucket, like `load_model`).
- **`export_model_to_osi` tool.** Exports a loaded model as OSI YAML, with
  optional `model_name` / `model_description` / `ai_instructions` overrides.
  Wraps `GET /v1/sessions/{id}/models/{mid}/osi` (multi-model mode; run-time
  bucket). These restore the model-centric OSI round-trip foreshadowed when the
  stateless `convert_osi_to_obml` / `convert_obml_to_osi` tools were removed in
  2.7.x — now backed by dedicated, session-aware API endpoints.

### Changed

- **Aligned with OrionBelt Semantic Layer API v2.8.0**, which adds the
  session-scoped OSI load/export endpoints.
- **Trimmed the `load_model` and `run_batch` tool descriptions.** Dropped the
  large inline OBML example and duplicated spec details from `load_model`
  (pointing to `get_json_schema("obml")` / `get_obml_reference()` instead) and
  condensed `run_batch`'s prose — both were among the largest token consumers in
  the tool surface. The two non-obvious `load_model` gotchas (joins live inside
  each dataObject; they reference OBML column names) are retained.

## [2.7.9] — 2026-06-01

### Changed

- **`get_json_schema` is now available in the run phase too.** Moved it from the
  design-only bucket to the always-on bucket, so it is listed in both the design
  and run phases — agents need the QueryObject schema to author `execute_query`
  payloads while a model is loaded.
- **Trimmed `execute_query`'s description.** Dropped the two inline query
  examples in favour of a concise pointer: `get_json_schema("query")` for the
  schema, `describe_model` for valid names, and `get_example` for worked queries.

## [2.7.8] — 2026-06-01

### Changed

- **`execute_query` now takes a single `query_json` argument** (a complete
  QueryObject as a JSON string, **required**) instead of the per-shape
  convenience params. Removed `dimensions`, `measures`, `fields`, `distinct`,
  `where`, `having`, `order_by`, `limit`, `offset`, `dimensions_exclude`,
  `coalesce_dimensions`, and `use_path_names` — all expressible inside
  `query_json`, whose schema is available via `get_json_schema("query")`. Kept
  `model_id`, `dialect`, and the output params (`output_format`,
  `format_values`, `locale`, `timezone`), which are not part of the QueryObject.
  Dropped the now-unused `_build_query_object` / `_parse_json_param` helpers; the
  `write_query` prompt and integration docs were rewritten around `query_json`.

### Removed

- **`convert_osi_to_obml` and `convert_obml_to_osi` tools.** The OSI↔OBML
  `POST /v1/convert/*` endpoints remain on the API; the MCP no longer wraps
  them. (Model-centric `load_model_from_osi` / `export_model_to_osi` tools may
  return later, backed by dedicated API endpoints.) Surface: 17 → 15
  (single-model), 20 → 18 (multi-model).

## [2.7.7] — 2026-06-01

### Added

- **Restored the `get_json_schema(name)` tool** (`obml` / `query` JSON Schemas
  via `GET /v1/reference/schemas/{name}`). It was removed in 2.7.5 while the
  endpoint 500'd on non-editable installs; **API v2.7.10** now bundles the
  reference schemas into the wheel, so the endpoint works on PyPI / Docker /
  Cloud Run. Registered in the design-time bucket. Surface: 16 → 17
  (single-model), 19 → 20 (multi-model).

## [2.7.6] — 2026-06-01

### Fixed

- **`serverInfo.version` now reports this server's version, not FastMCP's.**
  `FastMCP(...)` was constructed without a `version`, so the `initialize`
  response advertised the FastMCP library version (e.g. `3.3.1`) instead of the
  MCP server's version. It now passes `version=__version__`, resolved once from
  the installed package metadata (`_package_version()`), which also deduplicates
  the three places that detected the version (User-Agent header, startup banner,
  API-compatibility check).

## [2.7.5] — 2026-05-31

### Removed

- **Eight tools dropped to slim the surface** (24 → 16 single-model, 27 → 19
  multi-model):
  - the entire **OBSQL** tool surface — `get_obsql_reference`, `compile_obsql`,
    `execute_obsql` (OBML + QueryObject remain the supported path);
  - the standalone compile/plan verbs — `compile_query`, `plan_query`
    (`execute_query` compiles and runs in one call);
  - reference helpers `list_references` and `get_json_schema` (`get_obml_reference`
    remains);
  - `get_model_schema` — redundant with `describe_model`.

  The `obsql://reference` resource and `write_obsql_query` prompt are now
  orphaned (no OBSQL tool consumes them) — flagged for follow-up.

### Changed

- **Renamed two RDF tools for clarity:** `get_graph` → `get_model_graph`,
  `sparql_query` → `query_model_graph_by_sparql`.
- **Unified the model-tool registration (internal refactor).** The two
  near-identical registration functions (`_register_single_model_tools` /
  `_register_multi_model_tools`) and the split `_register_execute_query_tool`
  collapsed into a single `_register_model_tools()`. Every model-scoped tool is
  now defined **once** with an optional `model_id`, normalized by
  `_resolve_model_id()`: ignored in single-model mode (one pre-loaded model),
  required at call time in multi-model mode (clear error if missing). Mode-only
  tools (`get_model` for single; `load_model` / `remove_model` / `list_models` /
  `run_batch` for multi) are registered conditionally. Net **−394 lines** in
  `server.py`.
  Note: in multi-model mode `model_id` now appears as an *optional* field in each
  tool's input schema (it was a required positional); it is still enforced at
  call time. MCP clients call tools by name, so argument order is unaffected.

## [2.7.4] — 2026-05-31

### Changed

- **Discovery cluster collapsed into `list_artefacts` + `find_artefacts`.**
  The six artefact tools `list_dimensions` / `list_measures` / `list_metrics`
  and `get_dimension` / `get_measure` / `get_metric` are replaced by a single
  `list_artefacts(kind?, name?)` verb, split from search on a real semantic
  boundary:
  - `list_artefacts` — **exact, deterministic, complete**. No args → every
    artefact; `kind` → that kind's full set; `name` → that exact artefact.
    Always returns full records (artefact definitions are small, so list and
    single-name lookup are the same shape at different cardinality — a separate
    `get_*` is unnecessary).
  - `find_artefacts(query, kind?)` — **fuzzy, ranked search** for "I don't
    know the exact name". Its old `types: list[str]` argument is now a single
    `kind` enum.
  The agent knows which it holds (complete set vs. ranked candidates) from the
  verb it chose. `explain_artefact` (lineage), `describe_model` (model-level
  overview), and `list_examples` / `get_example` (canned queries, not model
  artefacts) are intentionally **not** folded in. Net tool count drops by 5.

### Added

- **Design-time vs run-time tool phase switching (Option A).** The tool
  surface **swaps** between two phase-scoped sets as the model lifecycle
  moves load → query → unload, using three buckets:
  - **always** (`load_model`, `remove_model` — so a second model can be loaded
    mid-session; plus `run_batch`, the self-contained one-shot, which needs no
    prior session state) — listed in both phases;
  - **design-only** (references, `get_json_schema`, `list_dialects`,
    converters) — listed only before a model is loaded, and **hidden** in the
    run phase so authoring tools don't pollute the query surface;
  - **run-only** (`compile_query`, `execute_query`, `describe_model`,
    `list_artefacts`, introspection, …) — listed only once a model is loaded.

  Design phase shows always + design-only; run phase shows always + run-only
  (a swap, not additive). Single-model mode is permanently run-time
  (model pre-loaded). The phase is derived from explicit loaded-model state,
  not hidden per-connection state, so it stays stateless-clean. Implemented
  via a `PhaseMiddleware` that filters `tools/list` and guards `tools/call`.
- **Structured "no model loaded" guard.** Invoking a run-time verb while in
  the design phase returns a structured error steering the host to call
  `load_model` and re-list, instead of an opaque downstream failure.
- **Unified capability gating (orthogonal to phase).** `execute_query` /
  `execute_obsql` are now **always registered** and filtered out of
  `tools/list` (and refused at call time with a structured error) when the
  server is configured compile-only (`query_execute: false`), rather than
  being conditionally registered. The mechanism is a general
  capability-flag → resolver registry (`_TOOL_CAPABILITY` /
  `_CAPABILITY_RESOLVERS`) composed into `PhaseMiddleware`, so future
  "server can't do X here" flags drop in without touching registration. A
  verb is visible only if its phase is active **and** its capability is
  enabled.
- **Explicit re-list signal on lifecycle transitions.** `load_model`
  (design → run) and `remove_model` (run → design, when no models remain)
  now append a signal prompting the agent to re-discover the changed tool
  surface — the plan-preferred pull mechanism under the stateless spec
  (push `notifications/tools/list_changed` is unreliable there). The spec
  `ttlMs` / `cacheScope` cache hints (SEP-2549, final 2026-07-28) are
  deferred: FastMCP's `on_list_tools` hook exposes only the tool sequence,
  not the result envelope, and the fields are still a release candidate.

### Changed

- **`describe_model` and `load_model` now surface the server-resolved
  effective dialect and timezone** as data (an `EFFECTIVE (server-resolved)`
  block / summary lines). This preserves the three agent-relevant fields
  the removed `get_settings` tool used to expose (`dialect.effective`,
  `timezone.effective`; the model's `defaultNumericDataType` was already
  shown). Best-effort: omitted silently if `/settings` is unavailable.
- **Upgraded FastMCP 3.2.4 → 3.3.1** (dependency floor raised to
  `fastmcp>=3.3,<4`). The middleware hooks the phase surface relies on
  (`on_list_tools`, `on_call_tool`) are unchanged across the bump.

### Removed

- **`get_settings` MCP tool.** A thin pass-through wrapper over the
  `GET /v1/settings` API endpoint with no added value, mirroring the
  existing decision to leave `POST /v1/cache/sweep`,
  `POST /v1/cache/clear`, `POST /v1/heartbeat`, and `GET /v1/cache/stats`
  unwrapped. Tool count drops by one in every mode. The settings endpoint
  (modes, TTL, dialect/timezone resolution, oneshot batch limits, and the
  cache configuration summary) remains available directly on the API.

## [2.7.3] — 2026-05-30

### Removed

- **`heartbeat` and `get_cache_stats` MCP tools.** Both were thin
  pass-through wrappers over the `POST /v1/heartbeat` and
  `GET /v1/cache/stats` API endpoints with no added value, mirroring the
  existing decision to leave `POST /v1/cache/sweep` and
  `POST /v1/cache/clear` unwrapped. The `HEARTBEAT_AUTH_TOKEN` env var
  (only consumed by the removed `heartbeat` tool) is dropped as well.
  Tool count drops by two in every mode. The freshness-cache endpoints
  remain available directly on the API; the cache configuration summary
  (including heartbeat-endpoint status) is still surfaced by the server
  config tool.

## [2.7.2] — 2026-05-26

### Fixed

- **`UNKNOWN_PROPERTY` errors from API v2.7.2 now render as structured
  `code: message` lines instead of a raw JSON dump.** API v2.7.2 rejects
  unknown OBML / QueryObject properties with a top-level
  `{message, errors, warnings}` envelope — no `detail` wrapper. The MCP's
  `_parse_error_detail` previously fell through to `response.text` for
  that shape, so the LLM saw raw JSON. The parser now promotes the body
  itself as the detail dict when `detail` is absent and `errors` is a
  top-level list, and the existing nested-errors formatter renders it
  the same way as `ResolutionError` / OBSQL translation errors.

### Added

- **`UNKNOWN_PROPERTY` documented in the debug error-code reference**
  (`get_debug_validation_codes`) with a fix pointer to
  `get_json_schema()` for the real field names. Common culprits called
  out: typos (`filtter:` vs `filter:`), snake_case vs camelCase
  mix-ups, fields that moved between versions.

### Compatibility

- **Aligned with OrionBelt Semantic Layer API v2.7.2.** The strict
  same-major+minor startup check accepts any v2.7.x ↔ v2.7.y pair, so
  MCP v2.7.0 also connects to API v2.7.2 — this release is the narrative
  alignment + the error-rendering fix that makes the new
  `UNKNOWN_PROPERTY` code legible.

## [2.7.0] — 2026-05-25

### Changed

- **Version aligned with OrionBelt Semantic Layer API v2.7.0.** The
  strict same-major+minor startup check (introduced in v2.6.1) means
  MCP v2.6.x cannot connect to API v2.7.0 — bumping to 2.7.0 restores
  compatibility. No functional changes to MCP itself: the new API
  features in v2.7.0 (`exists` / `nonexists` filter operators) ride
  through the existing QueryObject forwarding path unchanged.

### Notes

- API v2.7.0 removed the deprecated `MODEL_FILE` env var; MCP never
  referenced it, so no MCP-side change is needed. Deployments setting
  `MODEL_FILE` should migrate to `MODEL_FILES=<path>` on the API side.

## [2.6.1] — 2026-05-24

### Changed

- **Startup version check is now semver-aware and strict on major/minor.**
  Same-major+minor pairs are accepted regardless of patch (e.g. MCP 2.6.0
  against API 2.6.1 or 2.6.5 starts silently), but any major or minor
  mismatch now exits with a clear error instead of merely warning. This
  prevents the server from silently starting against an API that does
  not implement the features it depends on.

## [2.6.0] — 2026-05-23

### Added

- **OSI input validation surfacing** in `convert_osi_to_obml`. The API
  v2.6.0 `/v1/convert/osi-to-obml` response now carries an
  `input_validation` block (OSI input checked against the vendored OSI
  v0.2 schema before conversion); the tool renders schema errors and
  semantic warnings as advisory text alongside the existing output-side
  `validation` block. Legacy OSI v0.1 inputs still convert via the
  upstream compat shim — any `input_validation` issues there surface as
  advisory only.
- **Window-metric rendering** in `describe_model` and `list_metrics`.
  The new OBML `type: window` metrics (rank, dense_rank, row_number,
  ntile, lag, lead, first_value, last_value) pretty-print
  `windowFunction`, `measure`, `partitionBy`, `orderDirection`
  (non-default only), `offset`, `buckets`, `defaultValue`, and
  `timeDimension` so LLMs and humans see the full metric definition at
  a glance.
- **`partitionBy` rendering on cumulative metrics** — the new v2.6.0
  per-dimension partitioning for running/rolling/grain-to-date metrics
  (e.g. moving averages per country) is surfaced in the cumulative
  one-line summary.
- **Two-column statistical aggregate columns** rendered in
  `describe_model` and `list_measures`. Measures using the new
  statistical aggregates `corr`, `covar_pop`, `covar_samp`,
  `regr_slope`, `regr_intercept` carry ordered column references, and
  the rendered output now shows `columns: [DataObj.Col, DataObj.Col]`
  so the order (significant in the compiled SQL) is visible.

### Changed

- Version bumped to 2.6.0 (aligned with OrionBelt Semantic Layer API
  2.6.0).
- OrionBelt Semantic Layer badge updated to 2.6.

### Notes

- All new behaviors are **forward-compatible**: tools read the new
  response fields defensively and stay silent when run against
  pre-v2.6.0 servers, so no observable change for v2.5.0 deployments.
- Reference content fetched via `get_obml_reference` (statistical
  aggregations list, dialect-coverage matrix, window-metric examples,
  `partitionBy` semantics) flows through automatically from the live
  API — no MCP-side hardcoding.
- `compile_obsql` / `execute_obsql` accept the new aggregations and
  metric type by pass-through; unsupported aggregation/dialect
  combinations raise `UNSUPPORTED_AGGREGATION_FOR_DIALECT` at compile
  time, formatted cleanly by the existing structured-error path.
- Upstream env-var rename `MODEL_FILE` → `MODEL_FILES` is server-side
  only — this MCP doesn't set the variable and continues to auto-detect
  single-model vs multi-model mode at startup.

---

## [2.5.0] — 2026-05-22

### Changed

- Version bumped to 2.5.0 (aligned with OrionBelt Semantic Layer API 2.5.0).
- OrionBelt Semantic Layer badge updated to 2.5.

### Notes

- No MCP code changes. API v2.5.0 introduces the PostgreSQL wire-protocol
  surface (port 5432) for BI tools (Tableau, DBeaver, Superset, Power BI,
  `psql`, Dremio as a Postgres source) and the supporting Tableau / pgjdbc
  end-to-end compatibility work. The pgwire surface is independent of the
  REST API this MCP delegates to — no new endpoints, no shape changes to
  endpoints the MCP consumes.
- Server-side improvements that flow through to existing tools without
  client changes:
  - `compile_query` / `execute_query`: CFL planner now joins tables
    referenced by measure-filter expressions, fixing a "missing
    FROM-clause entry" 500 on measures like `Electronics Sales` filtered
    on a sibling dim table.
  - `execute_query`: richer type hints when the API runs against ADBC
    Postgres (NUMERIC / MONEY / INTERVAL surfaced as `number` /
    `datetime` instead of `string`).
  - `compile_obsql` / `execute_obsql`: Tableau-style
    `HAVING (COUNT(1) > 0)` tautology is silently dropped and
    `CAST(col AS …)` wrappers in SELECT / ORDER BY are unwrapped to the
    underlying column.

---

## [2.4.0] — 2026-05-15

### Added
- **OBSQL natural SQL surface** — new tools wrap the v2.4.0 OBSQL endpoints
  so LLMs (and humans) can express queries as BI-style SQL against the
  model's virtual table instead of building a `QueryObject` JSON:
  - **`compile_obsql(sql, dialect?)`** — POST
    `/v1/sessions/{sid}/query/semantic-ql/compile` (multi-model) or
    `/v1/query/semantic-ql/compile` (single-model).  Returns compiled SQL
    plus the translated `QueryObject` and explain plan.  Surface supports
    `SELECT … FROM <model>`, `WHERE`, `HAVING`, `GROUP BY`, `ORDER BY …
    [NULLS FIRST|LAST]`, `LIMIT`, `OFFSET`, and `WITH ROLLUP | WITH CUBE`.
    `SELECT` without `FROM` resolves to the implicit model.
  - **`execute_obsql(sql, dialect?, output_format?, format_values?, locale?,
    timezone?)`** — same as above but compiles AND executes.  Registers
    only when `QUERY_EXECUTE=true` on the API
- **`get_obsql_reference()` tool** — GET `/v1/reference/obsql`.  Returns
  the full OBSQL grammar with examples.  Mode-independent.  Cached
- **`list_references()` tool** — GET `/v1/reference`.  Lists all
  reference documents (markdown + JSON schemas) published by the API
- **`get_json_schema(name)` tool** — GET `/v1/reference/schemas/{name}`.
  Returns the raw JSON Schema for `obml` (model documents) or `query`
  (QueryObject) so callers can validate documents locally
- **`obsql://reference` resource** — same content as `get_obsql_reference`,
  exposed as an MCP resource
- **`write_obsql_query` prompt** — surfaces the OBSQL reference for
  authoring agents that prefer SQL over JSON QueryObjects
- **`UNSUPPORTED_GROUPING` / `UNSUPPORTED_SQL_FEATURE` error codes** —
  documented in the `debug_validation` prompt.  `UNSUPPORTED_GROUPING`
  fires when the dialect (e.g. MySQL) cannot compile `WITH CUBE` /
  `WITH ROLLUP`; `plan_query` returns it as a structured warning
  instead of a 4xx.  `UNSUPPORTED_SQL_FEATURE` fires for OBSQL
  constructs the translator rejects (JOIN, CTE, subquery, UNION,
  window function, `SELECT *`, raw-mode with trailing
  `WITH ROLLUP`/`WITH CUBE`)
- **Structured-error context decoration** — `_parse_error_detail` now
  appends `aggregation=…, dialect=…` (or `grouping=…, dialect=…`) to
  the surfaced ToolError message when the API returns those structured
  fields, and joins nested `errors[].{code, message}` lists onto the
  detail line so the LLM sees the full context without parsing JSON
- **Determinism & caching notes** — `write_query` prompt documents the
  v2.4.0 cache-determinism behaviour: queries with `limit` and no
  `order_by` are auto-ordered by all dimensions / raw fields, and SQL
  containing `RAND()` / `NOW()` / `CURRENT_DATE` / `TABLESAMPLE` is
  excluded from the cache (surfaces as
  `ttl_source = "no_cache:non_deterministic_sql"`)

### Changed
- Version bumped to 2.4.0 (aligned with OrionBelt Semantic Layer API 2.4.0)
- OrionBelt Semantic Layer badge updated to 2.4
- Mode-independent tool count: 7 → 10 (+`get_obsql_reference`,
  `list_references`, `get_json_schema`)
- Multi-model tool count: 30 → 33 (+`compile_obsql`; +`execute_obsql`
  when `QUERY_EXECUTE=true` brings it to 35)
- Single-model tool count: 27 → 30 (+`compile_obsql`; +`execute_obsql`
  when `QUERY_EXECUTE=true` brings it to 32)

---

## [2.3.0] — 2026-05-10

### Changed

- Tested against OBSL v2.3.0; no client changes required.

## [2.2.1] — 2026-05-09

### Changed

- Version bumped to 2.2.1 (aligned with OrionBelt Semantic Layer API 2.2.1). No MCP code changes — this release tracks the API's bundled demo model rewrite and ER diagram fixes.

## [2.2.0] — 2026-05-05

### Added (post-2.2.0 follow-up: freshness cache)
- **`get_cache_stats` tool** — GET `/v1/cache/stats`: backend
  (``noop``/``file``/…), entry count, total / max size, hit-rate, oldest
  entry, next sweep time, tracked physical tables, and heartbeat
  invalidation totals.  Mode-independent
- **`heartbeat` tool** — POST `/v1/heartbeat` with bearer auth: notify the
  API that a physical table was refreshed; the cache invalidates every
  entry that depends on it.  Requires ``HEARTBEAT_AUTH_TOKEN`` env var on
  the MCP server (forwarded as ``Authorization: Bearer …``)
- **`plan_query` / `list_examples` / `get_example` in single-model mode** —
  these tools now register in single-model mode too (uses the new
  `/v1/query/plan`, `/v1/examples`, `/v1/examples/{name}` shortcut routes
  on the API)
- **`compile_query` physical tables** — output now includes
  ``-- Physical tables: …`` (DATABASE.SCHEMA.CODE refs) when the API
  surfaces ``physical_tables`` in the response.  ``execute_query`` already
  passes through raw JSON, so the new ``cached`` / ``cached_at`` /
  ``ttl_seconds`` / ``ttl_source`` / ``ttl_limiting_table`` fields appear
  automatically
- **`HEARTBEAT_AUTH_TOKEN` env var** on the MCP server, paired with the
  API's matching env

### Added
- **`run_batch` tool** — POST `/v1/oneshot/batch`: load (or reference) one
  OBML model and run N independent queries in parallel in a single round
  trip.  Stable result ordering by caller-provided id (auto-assigned
  `q0`/`q1`/… when omitted).  Per-query and batch-level timeouts honoured.
  ``fail_fast`` cancels the rest on first failure; default is partial
  failure with per-query ``status: ok|error|cancelled``
- **`plan_query` tool** — POST `/v1/sessions/{sid}/query/plan`: planner's
  understanding of a query (planner reason, physical tables, join path,
  filter count, would-compile flag) without compiling SQL or executing.
  Opt-in ``include_database_explain=true`` runs warehouse ``EXPLAIN`` and
  surfaces the raw output; failures emit ``DATABASE_EXPLAIN_FAILED``
  warnings without dropping the OBSL plan
- **`list_examples` / `get_example` tools** — GET
  `/v1/sessions/{sid}/models/{mid}/examples` (with optional ``intent``
  filter) and ``…/examples/{name}``.  Surfaces canonical example queries
  authored alongside the model with intent tags and a compiled SQL
  preview
- **`load_model` dedup + health** — new ``dedup`` argument (default
  ``True``) reuses an existing ``model_id`` when identical OBML content is
  already loaded in the session; the response now surfaces ``model_load``
  (``fresh`` | ``reused``) and a structural ``health`` block (status, join
  count, orphan dataObjects, fan-trap risks, unreachable dimensions)
- **Structured warnings** — ``compile_query``, ``load_model``, and
  ``plan_query`` render the new
  ``{code, severity, message, path, hint, context}`` shape; legacy plain
  strings still render unchanged
- **Fuzzy `find_artefacts`** — output now splits exact / synonym matches
  and surfaces fuzzy near-miss candidates (with score + reason) when no
  exact or synonym hit is found
- **`get_settings` oneshot batch limits** — surfaces ``max_queries``,
  ``max_parallelism``, per-query timeout, and batch timeout when the
  ``/v1/settings`` response includes ``oneshot_batch``

### Changed
- Version bumped to 2.2.0 (aligned with OrionBelt Semantic Layer API 2.2.0)
- OrionBelt Semantic Layer badge updated to 2.2
- Multi-model tool count: 25 → 30 (+`run_batch`, `plan_query`,
  `list_examples`, `get_example`, `get_cache_stats`, `heartbeat`)
- Single-model tool count: 22 → 27 (+`plan_query`, `list_examples`,
  `get_example`, `get_cache_stats`, `heartbeat`)

---

## [2.1.0] — 2026-04-26

### Added
- **Raw query mode (`fields`)** — `compile_query` and `execute_query` support a
  new `fields` parameter for un-aggregated physical column access via
  `select.fields` (e.g. `["Orders.OrderID", "Orders.CustomerName"]`), mutually
  exclusive with `dimensions`/`measures`
- **`distinct` parameter** — `compile_query` and `execute_query` support
  `distinct` to emit `SELECT DISTINCT` (raw mode only)
- **Execute query output formatting** — `execute_query` gains `output_format`
  (`"json"` or `"tsv"`), `format_values`, `locale`, and `timezone` parameters
  for controlling response format and locale-aware number/date rendering
- **Optional `dialect`** — `dialect` parameter on `compile_query` and
  `execute_query` is now optional (was `"postgres"` default); when omitted
  the API resolves via `model.settings.defaultDialect` → `DB_VENDOR` →
  `"postgres"`
- **`defaultDialect` in `describe_model`** — model settings section now
  surfaces the optional `defaultDialect` field
- **Filter operators** — `write_query` prompt documents 7 new filter
  operators: `regex`, `notregex`, `blank`, `notblank`, `length_eq`,
  `length_gt`, `length_lt`
- **`get_settings` enriched** — now surfaces dialect resolution chain
  (`model` / `env` / `effective`), timezone resolution chain (`model` /
  `host` / `database` / `effective`), and model settings block; in
  multi-model mode passes `session_id` so the API resolves the loaded
  model's settings automatically
- **Validation error codes** — `debug_validation` prompt updated with
  `INVALID_FILTER_VALUE` error code
- **Query prompt** — `write_query` prompt updated with Raw Mode, Execute
  Query Output Formatting, and Default Dialect documentation sections

### Changed
- Version bumped to 2.1.0 (aligned with OrionBelt Semantic Layer API 2.1.0)
- OrionBelt Semantic Layer badge updated to 2.1

---

## [2.0.1] — 2026-04-27

### Added
- **API version check** — startup compares MCP server version against API
  `/health` version and logs a warning on major or minor mismatch
- **`get_settings` version info** — now shows API version and API prefix
  from the `/v1/settings` response

---

## [2.0.0] — 2026-04-27

### Added
- **Role-playing dimensions (`via`)** — `describe_model` and `list_dimensions`
  now surface the optional `via` property on dimensions, showing which
  intermediate data object forces the join path
- **Coalesce dimensions** — `compile_query` and `execute_query` support a new
  `coalesce_dimensions` parameter to merge role-playing dimensions into a
  single output column via COALESCE (e.g.
  `[{"coalesce": ["SalesEmp", "PurchaseEmp"], "as": "Employee"}]`)
- **Validation error codes** — `debug_validation` prompt updated with
  `INVALID_VIA_DATA_OBJECT`, `MISSING_VIA`, `UNREACHABLE_REQUIRED_OBJECT`,
  `COALESCE_MISSING_ALIAS`, `DUPLICATE_COALESCE_ALIAS`,
  `COALESCE_ALIAS_COLLISION`, `COALESCE_TOO_FEW_MEMBERS`, and
  `COALESCE_TYPE_MISMATCH` error/warning codes
- **Query prompt** — `write_query` prompt updated with role-playing dimensions
  and coalesce dimensions documentation sections

### Changed
- Version bumped to 2.0.0 (aligned with OrionBelt Semantic Layer API 2.0.0)
- OrionBelt Semantic Layer badge updated to 2.0

---

## [1.8.0] — 2026-04-22

### Added
- **Grain override support** — `describe_model` and `list_measures` now
  surface per-measure `grain` overrides (mode, exclude, include, keepOnly)
  when returned by the API
- **Filter context support** — `describe_model` and `list_measures` now
  surface per-measure `filterContext` overrides (mode, exclude, include,
  keepOnly) when returned by the API
- **Explain plan** — `compile_query` output now shows `has_grain_overrides`
  and `has_filter_context` flags from the query explain plan
- **Validation error codes** — `debug_validation` prompt updated with
  `UNKNOWN_GRAIN_DIMENSION`, `UNKNOWN_FILTER_CONTEXT_FIELD`, and
  `GRAIN_NOT_SUBSET` error codes
- **Query prompt** — `write_query` prompt updated with grain override and
  filter context documentation section

### Changed
- Version bumped to 1.8.0 (aligned with OrionBelt Semantic Layer API 1.8.0)
- OrionBelt Semantic Layer badge updated to 1.8

---

## [1.3.0] — 2026-04-10

### Added
- **Semantic graph tools** (both modes):
  - `get_graph(model_id)` — returns OBSL-Core RDF as Turtle
    (`GET /v1/sessions/{id}/models/{mid}/graph` or `GET /v1/graph`)
  - `sparql_query(model_id, query)` — read-only SPARQL (SELECT / ASK)
    over the model graph (`POST /v1/sessions/{id}/models/{mid}/sparql`
    or `POST /v1/sparql`)
- **`validate_model` in single-model mode** — stateless validation via
  `POST /v1/validate` shortcut; previously only available in multi-model mode
- `vulture` added to dev dependencies for dead code detection

### Changed
- README tool counts updated to **23 tools (single-model)** /
  **25 tools (multi-model)**
- OrionBelt Semantic Layer badge updated to 1.3

## [1.2.1] — 2026-03-28

### Fixed
- Startup crash on missing package metadata (fallback to `"dev"` version string)
- Stale module-level state after shutdown — reset cached session ID and HTTP
  client so a second `main()` invocation starts cleanly
- Thread-safety hardening around session ID caching and HTTP client creation
- Code review follow-ups: input validation, error propagation, and
  additional test coverage

### Added
- Integration guides for OpenAI Agents SDK, LangChain, Google ADK, n8n,
  and CrewAI (`docs/integrations/`)
- Architecture diagram in `docs/assets/`
- MySQL and OrionBelt Semantic Layer 1.2 badges in README
- README documentation for dual-mode (single-model / multi-model) support

## [1.2.0] — 2026-03-20

### Added
- **Single-model mode** — auto-detected via `GET /v1/settings`
  (`single_model_mode: true`); the server registers a reduced tool set
  without `model_id` parameters and uses shortcut endpoints
  (`/v1/schema`, `/v1/query/sql`, `/v1/dimensions/...`, etc.)
- **Dynamic tool registration** — `_register_single_model_tools()` /
  `_register_multi_model_tools()` pick the right set at startup
- **Semantic features passthrough**:
  - Cumulative metrics (running total, rolling window, grain-to-date)
  - Period-over-Period (PoP) metrics (YoY / MoM / QoQ with `percentChange`,
    `difference`, `ratio`, `previousValue`)
  - Measure filters (leaf + nested AND/OR/NOT groups)
  - Ratio pattern via derived metrics referencing filtered measures
- Structured error handling with unsupported aggregation reporting
- OBML reference and dialect list now fetched from the API (cached) instead
  of hardcoded

### Changed
- `get_model` added in single-model mode (returns the original OBML YAML
  from `GET /v1/settings.model_yaml`); `load_model`, `remove_model`, and
  `list_models` are not registered in single-model mode

## [1.1.0] — 2026-03-17

### Added
- **`execute_query` tool** — compile and execute a query in one call,
  returning SQL plus result data (`POST /v1/sessions/{id}/query/execute`).
  Requires `QUERY_EXECUTE` on the API
- Flight SQL capability information surfaced via `get_settings`
- OBML language features: filter groups (nested AND/OR/NOT), qualified
  column references, description fields on artefacts, and `numClass`
  support
- README updated with `execute_query` documentation and Flight SQL notes

### Changed
- `execute_query` returns raw API JSON instead of reformatted output so
  clients get the full result shape

## [1.0.0] — 2026-03-16

### Added
- **API v1 support** — all endpoints now use `/v1/` prefix (aligned with API v1.0.0)
- **12 new tools** (22 total):
  - Model discovery: `get_model_schema`, `list_dimensions`, `get_dimension`,
    `list_measures`, `get_measure`, `list_metrics`, `get_metric`
  - Lineage & search: `explain_artefact`, `find_artefacts`, `get_join_graph`
  - Management: `remove_model`, `get_settings`
- **Query explain plan** — `compile_query` output now includes planner reasoning,
  base object selection, join decisions, and totals/CFL info
- **`sql_valid` warning** — `compile_query` flags potentially invalid SQL
- **BigQuery and DuckDB** dialects added to reference and prompt texts
- URL encoding for artefact names with spaces in discovery endpoints
- Guarded JSON parsing on all success API responses (`_parse_json` helper)

### Changed
- FastMCP dependency upgraded from `>=3.0` to `>=3.1`
- Dependency upper bounds added: `httpx<1`, `pydantic-settings<3`
- Classifier updated from Beta to Production/Stable
- README restructured with architecture overview, categorised tool tables
- CLAUDE.md updated with full 22-tool API mapping

## [0.7.0] — 2025-06-01

### Added
- `get_model_diagram` tool — Mermaid ER diagram generation
- `convert_osi_to_obml` and `convert_obml_to_osi` tools — OSI format conversion
- Synonym display in `describe_model` output

## [0.5.0] — 2025-05-15

### Added
- Initial release: thin MCP server delegating to OrionBelt Semantic Layer REST API
- 7 core tools: `get_obml_reference`, `load_model`, `validate_model`,
  `describe_model`, `compile_query`, `list_models`, `list_dialects`
- Auto-session management with expiry detection and retry
- Startup health check against API `/health` endpoint
- 3 prompts (`write_obml_model`, `write_query`, `debug_validation`) + 1 resource
- Thread-safe session and HTTP client management
- Prefect Horizon hosted deployment support
