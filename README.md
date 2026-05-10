<!-- mcp-name: io.github.ralfbecher/orionbelt-semantic-layer-mcp -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Semantic Layer MCP</h1>

<p align="center"><strong>Thin MCP server that delegates to the OrionBelt Semantic Layer REST API</strong></p>

[![Version 2.3.0](https://img.shields.io/badge/version-2.3.0-purple.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/releases)
[![OrionBelt Semantic Layer 2.3](https://img.shields.io/badge/OrionBelt_Semantic_Layer-2.3-0054A6.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/blob/main/LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.2+-8A2BE2)](https://gofastmcp.com)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)

[![BigQuery](https://img.shields.io/badge/BigQuery-669DF6.svg?logo=googlebigquery&logoColor=white)](https://cloud.google.com/bigquery)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Snowflake](https://img.shields.io/badge/Snowflake-29B5E8.svg?logo=snowflake&logoColor=white)](https://www.snowflake.com)
[![ClickHouse](https://img.shields.io/badge/ClickHouse-FFCC01.svg?logo=clickhouse&logoColor=black)](https://clickhouse.com)
[![Dremio](https://img.shields.io/badge/Dremio-31B48D.svg)](https://www.dremio.com)
[![Databricks](https://img.shields.io/badge/Databricks-FF3621.svg?logo=databricks&logoColor=white)](https://www.databricks.com)
[![DuckDB](https://img.shields.io/badge/DuckDB-FFF000.svg?logo=duckdb&logoColor=black)](https://duckdb.org)
[![MySQL](https://img.shields.io/badge/MySQL-4479A1.svg?logo=mysql&logoColor=white)](https://www.mysql.com)

A thin MCP server that delegates all business logic to the [OrionBelt Semantic Layer](https://github.com/ralfbecher/orionbelt-semantic-layer) REST API via HTTP. No embedded engine — pure API pass-through.

## Architecture

The OrionBelt Semantic Layer platform has two deployment modes. This MCP server supports both:

- **Standalone** — Deploy the [OrionBelt Semantic Layer API](https://github.com/ralfbecher/orionbelt-semantic-layer) anywhere (Cloud Run, Docker, localhost) and point this MCP server at it via `API_BASE_URL`.
- **Hosted** — Connect to the public Cloud Run deployment with zero local setup (see [Hosted MCP Server](#hosted-mcp-server) below).

```
┌────────────┐       ┌──────────────────────────────────────────────────────┐
│ LLM Client │       │                OrionBelt Platform                    │
│            │       │                                                      │
│  Claude,   │──MCP──│──> server.py  ──HTTP /v1──>  Semantic Layer REST API │
│  Cursor,   │       │    (FastMCP                   (FastAPI: parse OBML,  │
│  any MCP   │       │     + httpx)                   validate, compile     │
│  client    │       │                                to SQL)               │
└────────────┘       └──────────────────────────────────────────────────────┘
```

- **No business logic** — all tool calls delegate to the REST API (v1 endpoints)
- **Dual-mode** — auto-detects single-model or multi-model API mode at startup
- **Auto-session management** — creates an API session on first tool call, caches the ID (multi-model mode)
- **27 tools** (single-model mode) or **30 tools** (multi-model mode) for querying, execution, batch, planning, discovery, examples, diagrams, RDF/SPARQL, freshness cache, and format conversion
- **3 prompts + 1 resource** for OBML reference and usage guidance

<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/architecture.png" alt="OrionBelt Analytics Architecture" width="900">
</p>

## Live Demo

A public demo of the OrionBelt Semantic Layer API is available at:

> **API endpoint:** `https://orionbelt.ralforion.com` — [Swagger UI](https://orionbelt.ralforion.com/docs) | [ReDoc](https://orionbelt.ralforion.com/redoc) | [Gradio UI](https://orionbelt.ralforion.com/ui/?__theme=dark)

Set `API_BASE_URL=https://orionbelt.ralforion.com` in your `.env` file to use it (see `.env.example`).

## Installation

```bash
uv sync
```

For development (includes pytest, respx, ruff):

```bash
uv sync --all-groups
```

## Usage

### stdio (default)

```bash
uv run server.py
```

### HTTP transport

```bash
MCP_TRANSPORT=http uv run python server.py
```

### MCP client configuration

Add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "orionbelt": {
      "command": "uv",
      "args": ["run", "python", "server.py"],
      "cwd": "/path/to/orionbelt-semantic-layer-mcp"
    }
  }
}
```

## Configuration

Environment variables or `.env` file (pydantic-settings). See `.env.example` for defaults.

| Variable          | Default      | Description                           |
| ----------------- | ------------ | ------------------------------------- |
| `API_BASE_URL`    | — (required) | OrionBelt Semantic Layer REST API URL |
| `MCP_TRANSPORT`   | `stdio`      | `stdio`, `http`, or `sse`             |
| `MCP_SERVER_HOST` | `localhost`  | Bind host for HTTP/SSE                |
| `MCP_SERVER_PORT` | `9000`       | Bind port for HTTP/SSE                |
| `LOG_LEVEL`       | `INFO`       | Logging level                         |
| `API_TIMEOUT`     | `30`         | HTTP timeout in seconds               |
| `HEARTBEAT_AUTH_TOKEN` | —       | Bearer token forwarded to `POST /v1/heartbeat` (must match the API's value) |

## Tools

### Model lifecycle

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `get_obml_reference()`            | Returns the full OBML format specification               |
| `load_model(model, dedup=True)`   | Parse, validate, and store a model (returns health + model_load) |
| `describe_model(model_id)`        | Inspect data objects, dimensions, measures, metrics      |
| `remove_model(model_id)`          | Remove a model from the current session                  |
| `list_models()`                   | List all models loaded in the current session            |

### Model discovery

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `get_model_schema(model_id)`      | Full model structure as JSON (detailed)                  |
| `list_dimensions(model_id)`       | List all dimensions in a model                           |
| `get_dimension(model_id, name)`   | Get a single dimension by name                           |
| `list_measures(model_id)`         | List all measures in a model                             |
| `get_measure(model_id, name)`     | Get a single measure by name                             |
| `list_metrics(model_id)`          | List all metrics in a model                              |
| `get_metric(model_id, name)`      | Get a single metric by name                              |
| `explain_artefact(model_id, name)`| Explain lineage of a dimension, measure, or metric       |
| `find_artefacts(model_id, query)` | Search artefacts (exact / synonym / fuzzy buckets)       |
| `list_examples(model_id, intent?)`| List authored example queries (filterable by intent tag) |
| `get_example(model_id, name)`     | Get one example with query + compiled SQL preview        |
| `get_join_graph(model_id)`        | Return the join graph as an adjacency list               |

### Query, execution & diagrams

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `compile_query(...)`              | Compile a semantic query to SQL (with explain plan)      |
| `execute_query(...)`              | Compile and execute a query, returning SQL + result data |
| `plan_query(model_id, ...)`       | Planner view (no SQL); optional warehouse `EXPLAIN`      |
| `run_batch(queries, ...)`         | One-shot: load a model + run N queries in parallel       |
| `get_model_diagram(model_id)`     | Generate a Mermaid ER diagram for a loaded model         |

### Semantic graph (RDF / SPARQL)

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `get_graph(model_id)`             | Return the model as OBSL-Core RDF (Turtle)               |
| `sparql_query(model_id, query)`   | Run a read-only SPARQL query (SELECT / ASK)              |

### Freshness cache

| MCP Tool                                  | Description                                              |
| ----------------------------------------- | -------------------------------------------------------- |
| `get_cache_stats()`                       | Cache backend, entry count, hit rate, sweep time         |
| `heartbeat(database, schema, table, ts?)` | Notify the API a table refreshed (invalidates cache)     |

### Utilities

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `list_dialects()`                 | List available SQL dialects and capabilities             |
| `get_settings()`                  | Get API config (modes, TTL, oneshot batch limits)        |
| `convert_osi_to_obml(input_yaml)` | Convert OSI YAML to OBML format                          |
| `convert_obml_to_osi(input_yaml)` | Convert OBML YAML to OSI format                          |

## Supported SQL Dialects

`postgres`, `snowflake`, `clickhouse`, `databricks`, `dremio`, `bigquery`, `duckdb`

## Workflow

1. **Get reference** — call `get_obml_reference()` to learn OBML syntax
2. **Load model** — call `load_model(model_yaml)` to get a `model_id`
3. **Explore** — call `describe_model(model_id)` or use discovery tools (`list_dimensions`, `find_artefacts`, `explain_artefact`, etc.)
4. **Query** — call `compile_query(model_id, dimensions=[...], measures=[...])` to generate SQL
5. **Execute** — call `execute_query(model_id, dimensions=[...], measures=[...])` to run SQL and get results (requires `QUERY_EXECUTE=true` on the API)

## Integration Guides

Use the OrionBelt Semantic Layer MCP server with popular AI agent frameworks and automation platforms:

| Framework | Transport | Guide |
|-----------|-----------|-------|
| **OpenAI Agents SDK** | stdio, HTTP, SSE | [docs/integrations/openai-agents-sdk.md](docs/integrations/openai-agents-sdk.md) |
| **LangChain** | stdio, HTTP | [docs/integrations/langchain.md](docs/integrations/langchain.md) |
| **Google ADK** | stdio, HTTP, SSE | [docs/integrations/google-adk.md](docs/integrations/google-adk.md) |
| **n8n** | HTTP, SSE | [docs/integrations/n8n.md](docs/integrations/n8n.md) |
| **CrewAI** | stdio, HTTP | [docs/integrations/crewai.md](docs/integrations/crewai.md) |

Each guide includes quick-start examples, multi-agent patterns, and connection options for both the hosted demo and self-hosted deployments.

## Development

```bash
# Run tests
uv run pytest

# Lint
uv run ruff check server.py
uv run ruff format server.py tests/
```

## Hosted MCP Server

A public hosted instance of this MCP server runs on Google Cloud Run, connected
to the live OrionBelt Semantic Layer demo API. No local install, no API key.

### Endpoint

```
https://orionbelt.ralforion.com/mcp
```

Streamable HTTP (MCP spec 2025-03-26). Stateful — clients should send the
`initialize` handshake and reuse the returned `Mcp-Session-Id` header.

### Quick start with Claude Desktop

Claude Desktop's config schema accepts only stdio launchers — for a remote
MCP server, use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote)
stdio↔HTTP bridge (auto-fetched by `npx`, no manual install).

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and add:

```json
{
  "mcpServers": {
    "orionbelt": {
      "command": "npx",
      "args": [
        "mcp-remote",
        "https://orionbelt.ralforion.com/mcp",
        "--transport",
        "http"
      ]
    }
  }
}
```

Fully quit Claude Desktop (⌘Q on macOS — closing the window isn't enough) and
reopen. The OrionBelt tools then appear in the tools menu.

Alternatively, in newer Claude Desktop builds: **Settings → Connectors → Add
custom connector**, paste the URL above. No file editing or `npx` required.

> **Why `mcp-remote`?** Claude Desktop's `claude_desktop_config.json` schema
> currently only validates stdio entries (`command` + `args`). A bare
> `{"url": "…"}` entry is rejected with *"not valid MCP server configurations
> and were skipped"*. `mcp-remote` runs a local stdio bridge that forwards to
> the HTTPS endpoint, so Claude Desktop sees a normal stdio server. **Claude
> Code** does support `{"type": "url", "url": "…"}` natively — see below.

### Quick start with Claude Code

Add to `.mcp.json` in any repo (or `~/.config/claude-code/.mcp.json` globally):

```json
{
  "mcpServers": {
    "orionbelt": {
      "type": "url",
      "url": "https://orionbelt.ralforion.com/mcp"
    }
  }
}
```

### Other MCP clients

Any client that supports Streamable HTTP transport (MCP spec 2025-03-26) can
point at the URL above. The endpoint accepts `POST /mcp` with
`Accept: application/json, text/event-stream`. See
[`tests/cloudrun/test_mcp_cloudrun.sh`](tests/cloudrun/test_mcp_cloudrun.sh)
for a stdlib-only Python smoke test that walks the full handshake.

### Notes

- The hosted instance scales to zero when idle, so the first request after a
  cold period takes ~1–2 seconds longer.
- It connects to the public demo API at `https://orionbelt.ralforion.com` — same data,
  same dialects, no authentication. Don't load production data through it.
- For self-hosting, see the [Installation](#installation) section above and
  the [`Dockerfile`](Dockerfile).

## License

Copyright 2025 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>
