# CLAUDE.md

## Project Overview

**OrionBelt Semantic Layer MCP** is a thin MCP server that delegates all business logic to the OrionBelt Semantic Layer REST API via HTTP. It contains no embedded engine — pure API pass-through.

## Architecture

```
LLM Client  ──MCP──▶  server.py  ──HTTP──▶  OrionBelt Semantic Layer API
                       (FastMCP + httpx)     (Cloud Run / localhost)
```

- **No business logic** — all tool calls delegate to the REST API
- **Two modes** — auto-detected at startup via `GET /v1/settings`
  - **Multi-model mode**: 25 tools with `model_id`, session-scoped endpoints
  - **Single-model mode**: 22 tools (no `load_model`/`remove_model`/`list_models`/`validate_model`; adds `get_model`; no `model_id`), shortcut endpoints
- **3 prompts + 1 resource** — `write_obml_model` fetched from API; others static

## Commands

```bash
# Install
uv sync                          # main deps
uv sync --all-groups             # include dev deps (pytest, respx, ruff)

# Run
uv run python server.py                       # stdio (default)
MCP_TRANSPORT=http uv run python server.py    # HTTP on :9000

# Tests
uv run pytest                    # all tests (uses respx to mock API)

# Lint
uv run ruff check server.py
uv run ruff format server.py tests/
```

## Configuration

Environment variables or `.env` file (pydantic-settings):

| Variable | Default | Description |
|----------|---------|-------------|
| `API_BASE_URL` | — (required, see `.env.example`) | OrionBelt Semantic Layer REST API URL |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `http`, or `sse` |
| `MCP_SERVER_HOST` | `localhost` | Bind host for HTTP/SSE |
| `MCP_SERVER_PORT` | `9000` | Bind port for HTTP/SSE |
| `LOG_LEVEL` | `INFO` | Logging level |
| `API_TIMEOUT` | `30` | HTTP timeout in seconds |

## Entrypoint

For Prefect Horizon: `server.py:mcp`

## Tool → API Mapping

All API endpoints use the `/v1/` prefix (since API v1.0.0).

### Multi-model mode (session-scoped)

| MCP Tool | API Endpoint | Notes |
|----------|-------------|-------|
| `get_obml_reference()` | `GET /v1/reference/obml` | Fetched from API, cached |
| `load_model(model_yaml)` | `POST /v1/sessions/{id}/models` | Auto-creates session |
| `validate_model(model_yaml)` | `POST /v1/sessions/{id}/validate` | Always 200 |
| `describe_model(model_id)` | `GET /v1/sessions/{id}/models/{mid}` | Formats nested JSON |
| `compile_query(model_id, ...)` | `POST /v1/sessions/{id}/query/sql` | Simple + full mode, includes explain plan |
| `execute_query(model_id, ...)` | `POST /v1/sessions/{id}/query/execute` | Compile + execute, requires QUERY_EXECUTE or FLIGHT_ENABLED |
| `list_models()` | `GET /v1/sessions/{id}/models` | Lists models in session |
| `list_dialects()` | `GET /v1/dialects` | No session needed |
| `get_model_diagram(model_id, ...)` | `GET /v1/sessions/{id}/models/{mid}/diagram/er` | Mermaid ER diagram |
| `remove_model(model_id)` | `DELETE /v1/sessions/{id}/models/{mid}` | Remove model from session |
| `get_model_schema(model_id)` | `GET /v1/sessions/{id}/models/{mid}/schema` | Full JSON structure |
| `list_dimensions(model_id)` | `GET /v1/sessions/{id}/models/{mid}/dimensions` | All dimensions |
| `get_dimension(model_id, name)` | `GET /v1/sessions/{id}/models/{mid}/dimensions/{name}` | Single dimension |
| `list_measures(model_id)` | `GET /v1/sessions/{id}/models/{mid}/measures` | All measures |
| `get_measure(model_id, name)` | `GET /v1/sessions/{id}/models/{mid}/measures/{name}` | Single measure |
| `list_metrics(model_id)` | `GET /v1/sessions/{id}/models/{mid}/metrics` | All metrics |
| `get_metric(model_id, name)` | `GET /v1/sessions/{id}/models/{mid}/metrics/{name}` | Single metric |
| `explain_artefact(model_id, name)` | `GET /v1/sessions/{id}/models/{mid}/explain/{name}` | Lineage trace |
| `find_artefacts(model_id, query)` | `POST /v1/sessions/{id}/models/{mid}/find` | Name/synonym search |
| `get_join_graph(model_id)` | `GET /v1/sessions/{id}/models/{mid}/join-graph` | Adjacency list |
| `get_graph(model_id)` | `GET /v1/sessions/{id}/models/{mid}/graph` | OBSL-Core RDF as Turtle |
| `sparql_query(model_id, query)` | `POST /v1/sessions/{id}/models/{mid}/sparql` | Read-only SPARQL (SELECT/ASK) |
| `get_settings()` | `GET /v1/settings` | No session needed |
| `convert_osi_to_obml(...)` | `POST /v1/convert/osi-to-obml` | No session needed |
| `convert_obml_to_osi(...)` | `POST /v1/convert/obml-to-osi` | No session needed |

### Single-model mode (shortcut endpoints, no model_id)

When `GET /v1/settings` returns `single_model_mode: true`, the server registers tools
**without** `model_id` and uses shortcut endpoints that auto-resolve session/model.
`load_model`, `remove_model`, `list_models`, and `validate_model` are **not registered**.

| MCP Tool | Shortcut Endpoint | Notes |
|----------|------------------|-------|
| `get_model()` | `GET /v1/settings` → `model_yaml` | Returns original OBML YAML source |
| `describe_model()` | `GET /v1/schema` | SchemaResponse (columns as ColumnDetail) |
| `compile_query(...)` | `POST /v1/query/sql` | dialect as query param, query body direct |
| `execute_query(...)` | `POST /v1/query/execute` | dialect as query param, query body direct |
| `get_model_diagram(...)` | `GET /v1/diagram/er` | params: show_columns, theme |
| `get_model_schema()` | `GET /v1/schema` | Full JSON structure |
| `list_dimensions()` | `GET /v1/dimensions` | All dimensions |
| `get_dimension(name)` | `GET /v1/dimensions/{name}` | Single dimension |
| `list_measures()` | `GET /v1/measures` | All measures |
| `get_measure(name)` | `GET /v1/measures/{name}` | Single measure |
| `list_metrics()` | `GET /v1/metrics` | All metrics |
| `get_metric(name)` | `GET /v1/metrics/{name}` | Single metric |
| `explain_artefact(name)` | `GET /v1/explain/{name}` | Lineage trace |
| `find_artefacts(query)` | `POST /v1/find` | Name/synonym search |
| `get_join_graph()` | `GET /v1/join-graph` | Adjacency list |
| `get_graph()` | `GET /v1/graph` | OBSL-Core RDF as Turtle |
| `sparql_query(query)` | `POST /v1/sparql` | Read-only SPARQL (SELECT/ASK) |

## Semantic Features

The API supports three **metric types** and **measure filters**:

- **Derived metrics** — expression-based: `{[Measure A]} / {[Measure B]}`
- **Cumulative metrics** — running total, rolling window (`window: N`), or grain-to-date (`grainToDate: month`)
- **Period-over-Period (PoP) metrics** — compare a measure across time periods (YoY, MoM, QoQ) with configurable comparison (`percentChange`, `difference`, `ratio`, `previousValue`)
- **Measure filters** — restrict aggregation to matching rows via `CASE WHEN` wrapping; supports leaf filters and nested AND/OR/NOT groups
- **Ratio pattern** — derived metrics referencing filtered measures (e.g. `{[US Revenue]} / {[Revenue]}`)

All features are handled by the API — the MCP server passes through OBML YAML and query parameters unchanged.

## Session Management

**Multi-model mode** — sessions are fully internal (LLM never sees session IDs):
1. On first API call, `POST /v1/sessions` creates one
2. Session ID is cached in `_api_session_id`
3. On 410 (expired) or 404 (session not found), auto-recreates and retries once
4. 429 on session creation surfaces rate-limit / capacity error to the LLM
5. Best-effort cleanup on shutdown via `DELETE /v1/sessions/{id}`

**Single-model mode** — no sessions created.  The API has a `__default__` session
with the pre-loaded model.  Shortcut endpoints auto-resolve.

## Code Structure

- Mode-independent tools: decorated with `@mcp.tool` at module level
- Mode-dependent tools: defined in `_register_single_model_tools()` / `_register_multi_model_tools()`
- Shared logic: `_impl_*` functions accept `model_id: str | None`
- Registration: `main()` calls `_detect_single_model_mode()` then registers the right set
