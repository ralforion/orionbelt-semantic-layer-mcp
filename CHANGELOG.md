# Changelog

All notable changes to OrionBelt Semantic Layer MCP are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

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
