# Changelog

All notable changes to OrionBelt Semantic Layer MCP are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

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
