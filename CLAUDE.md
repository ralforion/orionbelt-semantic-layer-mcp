# CLAUDE.md

## Project Overview

**OrionBelt Semantic Layer MCP** is a thin MCP server that delegates all business logic to the OrionBelt Semantic Layer REST API via HTTP. It contains no embedded engine — pure API pass-through.

## Architecture

```
LLM Client  ──MCP──▶  server.py  ──HTTP──▶  OrionBelt Semantic Layer API
                       (FastMCP + httpx)     (Cloud Run / localhost)
```

- **No business logic** — all tool calls delegate to the REST API
- **Auto-session management** — creates an API session on first tool call, caches the ID
- **23 tools** (no session tools exposed — session handling is internal)
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

| MCP Tool | API Endpoint | Notes |
|----------|-------------|-------|
| `get_obml_reference()` | `GET /v1/reference/obml` | Fetched from API, cached |
| `load_model(model_yaml)` | `POST /v1/sessions/{id}/models` | Auto-creates session |
| `validate_model(model_yaml)` | `POST /v1/sessions/{id}/validate` | Always 200 |
| `describe_model(model_id)` | `GET /v1/sessions/{id}/models/{mid}` | Formats nested JSON |
| `compile_query(...)` | `POST /v1/sessions/{id}/query/sql` | Simple + full mode, includes explain plan |
| `execute_query(...)` | `POST /v1/sessions/{id}/query/execute` | Compile + execute, requires QUERY_EXECUTE or FLIGHT_ENABLED |
| `list_models()` | `GET /v1/sessions/{id}/models` | Lists models in session |
| `list_dialects()` | `GET /v1/dialects` | No session needed |
| `get_model_diagram(...)` | `GET /v1/sessions/{id}/models/{mid}/diagram/er` | Mermaid ER diagram |
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
| `get_settings()` | `GET /v1/settings` | No session needed |
| `convert_osi_to_obml(...)` | `POST /v1/convert/osi-to-obml` | No session needed |
| `convert_obml_to_osi(...)` | `POST /v1/convert/obml-to-osi` | No session needed |

## Semantic Features

The API supports three **metric types** and **measure filters**:

- **Derived metrics** — expression-based: `{[Measure A]} / {[Measure B]}`
- **Cumulative metrics** — running total, rolling window (`window: N`), or grain-to-date (`grainToDate: month`)
- **Period-over-Period (PoP) metrics** — compare a measure across time periods (YoY, MoM, QoQ) with configurable comparison (`percentChange`, `difference`, `ratio`, `previousValue`)
- **Measure filters** — restrict aggregation to matching rows via `CASE WHEN` wrapping; supports leaf filters and nested AND/OR/NOT groups
- **Ratio pattern** — derived metrics referencing filtered measures (e.g. `{[US Revenue]} / {[Revenue]}`)

All features are handled by the API — the MCP server passes through OBML YAML and query parameters unchanged.

## Session Management

Sessions are fully internal — the LLM never sees session IDs:
1. On first API call, `POST /v1/sessions` creates one
2. Session ID is cached in `_api_session_id`
3. On 404 (expired), auto-recreates and retries once
4. Best-effort cleanup on shutdown via `DELETE /v1/sessions/{id}`
