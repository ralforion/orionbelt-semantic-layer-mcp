# Changelog

All notable changes to OrionBelt Semantic Layer MCP are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

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
  Requires `QUERY_EXECUTE` or `FLIGHT_ENABLED` on the API
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
