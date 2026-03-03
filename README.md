<!-- mcp-name: io.github.ralfbecher/orionbelt-semantic-layer-mcp -->
<p align="center">
  <img src="docs/assets/ORIONBELT Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Semantic Layer MCP</h1>

<p align="center"><strong>Thin MCP server that delegates to the OrionBelt Semantic Layer REST API</strong></p>

[![Version 0.5.0](https://img.shields.io/badge/version-0.5.0-purple.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/releases)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/blob/main/LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.0+-8A2BE2)](https://gofastmcp.com)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2-E92063.svg?logo=pydantic&logoColor=white)](https://docs.pydantic.dev)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://docs.astral.sh/ruff/)

[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-4169E1.svg?logo=postgresql&logoColor=white)](https://www.postgresql.org)
[![Snowflake](https://img.shields.io/badge/Snowflake-29B5E8.svg?logo=snowflake&logoColor=white)](https://www.snowflake.com)
[![ClickHouse](https://img.shields.io/badge/ClickHouse-FFCC01.svg?logo=clickhouse&logoColor=black)](https://clickhouse.com)
[![Dremio](https://img.shields.io/badge/Dremio-31B48D.svg)](https://www.dremio.com)
[![Databricks](https://img.shields.io/badge/Databricks-FF3621.svg?logo=databricks&logoColor=white)](https://www.databricks.com)

A thin MCP server that delegates all business logic to the [OrionBelt Semantic Layer](https://github.com/ralfbecher/orionbelt-semantic-layer) REST API via HTTP. No embedded engine — pure API pass-through.

## Architecture

```
LLM Client  ──MCP──>  server.py  ──HTTP──>  OrionBelt Semantic Layer API
                       (FastMCP + httpx)     (Cloud Run / localhost)
```

- **No business logic** — all tool calls delegate to the REST API
- **Auto-session management** — creates an API session on first tool call, caches the ID
- **7 tools** for model loading, validation, querying, and introspection
- **3 prompts + 1 resource** for OBML reference and usage guidance

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

| MCP Tool                     | Description                                          |
| ---------------------------- | ---------------------------------------------------- |
| `get_obml_reference()`       | Returns the full OBML format specification           |
| `load_model(model_yaml)`     | Parse, validate, and store a semantic model          |
| `validate_model(model_yaml)` | Validate a model without storing it                  |
| `describe_model(model_id)`   | Inspect data objects, dimensions, measures, metrics  |
| `compile_query(...)`         | Compile a semantic query to SQL (simple + full mode) |
| `list_models()`              | List all models loaded in the current session        |
| `list_dialects()`            | List available SQL dialects and capabilities         |

## Supported SQL Dialects

`postgres`, `snowflake`, `clickhouse`, `databricks`, `dremio`

## Workflow

1. **Get reference** — call `get_obml_reference()` to learn OBML syntax
2. **Load model** — call `load_model(model_yaml)` to get a `model_id`
3. **Explore** — call `describe_model(model_id)` to inspect the model
4. **Query** — call `compile_query(model_id, dimensions=[...], measures=[...])` to generate SQL

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

### Quick start with Claude Desktop

1. Download the Desktop Extension:
   [orionbelt-semantic-layer.dxt](https://orionbelt-semantic-layer.fastmcp.app/manifest.dxt?v=371e7521-55de-4d82-a65d-cd5dd278daf8)
2. Open the `.dxt` file in [Claude Desktop](https://claude.ai/download)
   (requires Claude Desktop with MCP support)

No local setup or API key needed — the hosted server connects to the live demo API.

## License

Copyright 2025 [RALFORION d.o.o.](https://ralforion.com)

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

---

<p align="center">
  <a href="https://ralforion.com">
    <img src="docs/assets/RALFORION doo Logo.png" alt="RALFORION d.o.o." width="200">
  </a>
</p>
