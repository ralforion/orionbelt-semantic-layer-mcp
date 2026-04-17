<!-- mcp-name: io.github.ralfbecher/orionbelt-semantic-layer-mcp -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Semantic Layer MCP</h1>

<p align="center"><strong>Thin MCP server that delegates to the OrionBelt Semantic Layer REST API</strong></p>

[![Version 1.5.4](https://img.shields.io/badge/version-1.5.4-purple.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/releases)
[![OrionBelt Semantic Layer 1.5](https://img.shields.io/badge/OrionBelt_Semantic_Layer-1.5-0054A6.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer)
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
- **Hosted** — Use the managed deployment on [Prefect Horizon](https://horizon.prefect.io) with zero local setup (see [Live Demo Hosting](#mcp-live-demo-hosting-at-prefect-horizon) below).

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
- **22 tools** (single-model mode) or **25 tools** (multi-model mode) for querying, execution, discovery, diagrams, RDF/SPARQL, and format conversion
- **3 prompts + 1 resource** for OBML reference and usage guidance

<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/architecture.png" alt="OrionBelt Analytics Architecture" width="900">
</p>

## Live Demo

A public demo of the OrionBelt Semantic Layer API is available at:

> **API endpoint:** `http://35.187.174.102` — [Swagger UI](http://35.187.174.102/docs) | [ReDoc](http://35.187.174.102/redoc) | [Gradio UI](http://35.187.174.102/ui/?__theme=dark)

Set `API_BASE_URL=http://35.187.174.102` in your `.env` file to use it (see `.env.example`).

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

## Tools

### Model lifecycle

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `get_obml_reference()`            | Returns the full OBML format specification               |
| `load_model(model_yaml)`          | Parse, validate, and store a semantic model              |
| `validate_model(model_yaml)`      | Validate a model without storing it                      |
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
| `find_artefacts(model_id, query)` | Search artefacts by name or synonym                      |
| `get_join_graph(model_id)`        | Return the join graph as an adjacency list               |

### Query, execution & diagrams

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `compile_query(...)`              | Compile a semantic query to SQL (with explain plan)      |
| `execute_query(...)`              | Compile and execute a query, returning SQL + result data |
| `get_model_diagram(model_id)`     | Generate a Mermaid ER diagram for a loaded model         |

### Semantic graph (RDF / SPARQL)

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `get_graph(model_id)`             | Return the model as OBSL-Core RDF (Turtle)               |
| `sparql_query(model_id, query)`   | Run a read-only SPARQL query (SELECT / ASK)              |

### Utilities

| MCP Tool                          | Description                                              |
| --------------------------------- | -------------------------------------------------------- |
| `list_dialects()`                 | List available SQL dialects and capabilities             |
| `get_settings()`                  | Get API config (single-model mode, TTL, Flight SQL)      |
| `convert_osi_to_obml(input_yaml)` | Convert OSI YAML to OBML format                          |
| `convert_obml_to_osi(input_yaml)` | Convert OBML YAML to OSI format                          |

## Supported SQL Dialects

`postgres`, `snowflake`, `clickhouse`, `databricks`, `dremio`, `bigquery`, `duckdb`

## Workflow

1. **Get reference** — call `get_obml_reference()` to learn OBML syntax
2. **Load model** — call `load_model(model_yaml)` to get a `model_id`
3. **Explore** — call `describe_model(model_id)` or use discovery tools (`list_dimensions`, `find_artefacts`, `explain_artefact`, etc.)
4. **Query** — call `compile_query(model_id, dimensions=[...], measures=[...])` to generate SQL
5. **Execute** — call `execute_query(model_id, dimensions=[...], measures=[...])` to run SQL and get results (requires `FLIGHT_ENABLED=true` on the API)

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

## MCP Live Demo Hosting at Prefect Horizon

The OrionBelt Semantic Layer MCP server is available as a hosted live demo on [Prefect Horizon](https://horizon.prefect.io), a managed platform for deploying MCP servers.

### MCP URL

Use this URL to connect any MCP-compatible client to the hosted server (no authentication required):

```
https://orionbelt-semantic-layer.fastmcp.app/mcp
```

### Quick start with Claude Desktop

1. Download the Desktop Extension:
   [orionbelt-semantic-layer.dxt](https://orionbelt-semantic-layer.fastmcp.app/manifest.dxt)
2. Open the `.dxt` file in [Claude Desktop](https://claude.ai/download)
   (requires Claude Desktop with MCP support)

No local setup or API key needed — the hosted server connects to the live demo API.

## License

Copyright 2025 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/RALFORION_doo_Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>
