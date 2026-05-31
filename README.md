<!-- mcp-name: io.github.ralfbecher/orionbelt-semantic-layer-mcp -->
<p align="center">
  <img src="https://raw.githubusercontent.com/ralfbecher/orionbelt-semantic-layer-mcp/main/docs/assets/ORIONBELT_Logo.png" alt="OrionBelt Logo" width="400">
</p>

<h1 align="center">OrionBelt Semantic Layer MCP</h1>

<p align="center"><strong>Thin MCP server that delegates to the OrionBelt Semantic Layer REST API</strong></p>

[![Version 2.7.4](https://img.shields.io/badge/version-2.7.4-purple.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/releases)
[![OrionBelt Semantic Layer 2.7](https://img.shields.io/badge/OrionBelt_Semantic_Layer-2.7-0054A6.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/ralfbecher/orionbelt-semantic-layer-mcp/blob/main/LICENSE)
[![FastMCP](https://img.shields.io/badge/FastMCP-3.3+-8A2BE2)](https://gofastmcp.com)
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
- **24 tools** (single-model mode) or **27 tools** (multi-model mode) for querying (QueryObject + OBSQL natural SQL), execution, batch, planning, discovery, examples, diagrams, RDF/SPARQL, reference docs, and format conversion. The visible surface is smaller in the design-time phase and when query execution is disabled (see [Design-time vs run-time tool switching](#design-time-vs-run-time-tool-switching))
- **4 prompts + 2 resources** for OBML / OBSQL reference and usage guidance

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

## Tools

### Model lifecycle

| MCP Tool                        | Description                                                      |
| ------------------------------- | ---------------------------------------------------------------- |
| `get_obml_reference()`          | Returns the full OBML format specification                       |
| `load_model(model, dedup=True)` | Parse, validate, and store a model (returns health + model_load) |
| `describe_model(model_id)`      | Inspect data objects, dimensions, measures, metrics              |
| `remove_model(model_id)`        | Remove a model from the current session                          |
| `list_models()`                 | List all models loaded in the current session                    |

### Model discovery

| MCP Tool                                 | Description                                                                                      |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `get_model_schema(model_id)`             | Full model structure as JSON (detailed)                                                          |
| `list_artefacts(model_id, kind?, name?)` | **Exact, deterministic lookup** — all artefacts, one kind, or one named artefact (full records)  |
| `find_artefacts(model_id, query, kind?)` | **Fuzzy, ranked search** — resolve a vague term to real artefact names (exact / synonym / fuzzy) |
| `explain_artefact(model_id, name)`       | Explain lineage of a dimension, measure, or metric                                               |
| `list_examples(model_id, intent?)`       | List authored example queries (filterable by intent tag)                                         |
| `get_example(model_id, name)`            | Get one example with query + compiled SQL preview                                                |
| `get_join_graph(model_id)`               | Return the join graph as an adjacency list                                                       |

### Query, execution & diagrams

| MCP Tool                            | Description                                              |
| ----------------------------------- | -------------------------------------------------------- |
| `compile_query(...)`                | Compile a semantic query (QueryObject) to SQL            |
| `execute_query(...)`                | Compile and execute a QueryObject, returning SQL + rows  |
| `compile_obsql(model_id, sql, ...)` | Compile an OBSQL (natural SQL) query to SQL              |
| `execute_obsql(model_id, sql, ...)` | Compile and execute an OBSQL query, returning SQL + rows |
| `plan_query(model_id, ...)`         | Planner view (no SQL); optional warehouse `EXPLAIN`      |
| `run_batch(queries, ...)`           | One-shot: load a model + run N queries in parallel       |
| `get_model_diagram(model_id)`       | Generate a Mermaid ER diagram for a loaded model         |

### Semantic graph (RDF / SPARQL)

| MCP Tool                        | Description                                 |
| ------------------------------- | ------------------------------------------- |
| `get_graph(model_id)`           | Return the model as OBSL-Core RDF (Turtle)  |
| `sparql_query(model_id, query)` | Run a read-only SPARQL query (SELECT / ASK) |

### References

| MCP Tool                | Description                                             |
| ----------------------- | ------------------------------------------------------- |
| `get_obml_reference()`  | OBML (model authoring) grammar reference                |
| `get_obsql_reference()` | OBSQL (natural SQL surface) grammar reference           |
| `list_references()`     | Index of all references published by the API            |
| `get_json_schema(name)` | JSON Schema for `obml` (model) or `query` (QueryObject) |

### Utilities

| MCP Tool                          | Description                                  |
| --------------------------------- | -------------------------------------------- |
| `list_dialects()`                 | List available SQL dialects and capabilities |
| `convert_osi_to_obml(input_yaml)` | Convert OSI YAML to OBML format              |
| `convert_obml_to_osi(input_yaml)` | Convert OBML YAML to OSI format              |

## Design-time vs run-time tool switching

The server presents a **phase-scoped tool surface**: instead of listing all
~30 tools at once, it shows only the tools that make sense for where you are in
the model lifecycle. About half the tools are meaningless until a model is
loaded (`compile_query`, `execute_query`, `list_artefacts`, …) and the rest are
about authoring or pure file transforms (`get_obml_reference`,
`convert_obml_to_osi`, …). Splitting them keeps the surface small and prevents a
whole class of error — calling a query tool with no model loaded.

### Three buckets, swapped by phase

Tools fall into three buckets. The visible surface is a **swap** at the
load/unload transition, not additive — the run phase does **not** show the
design/reference tools:

| Bucket          | Listed when                 | Tools                                                                                                                                                                                                                                                                                             |
| --------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Always**      | always (both phases)        | `load_model`, `remove_model` (transition verbs — stay available in the run phase so a second model can be loaded mid-session, up to `max_models_per_session`); `run_batch` (self-contained one-shot — loads/references a model inline, so it needs no prior session state)                        |
| **Design-only** | only when no model loaded   | `get_obml_reference`, `get_obsql_reference`, `list_references`, `get_json_schema`, `list_dialects`, `convert_obml_to_osi`, `convert_osi_to_obml`                                                                                                                                                  |
| **Run-only**    | only when a model is loaded | `describe_model`, `get_model_schema`, `get_model_diagram`, `list_artefacts`, `find_artefacts`, `explain_artefact`, `plan_query`, `compile_query`, `compile_obsql`, `execute_query`, `execute_obsql`, `list_examples`, `get_example`, `get_graph`, `get_join_graph`, `sparql_query`, `list_models` |

```
                       load_model  (returns "re-list" signal)
   ┌─────────────────┐ ────────────────────────────────▶ ┌───────────────┐
   │ design phase    │                                   │ run phase     │
   │ always + design │ ◀───────────────────────────────  │ always + run  │
   └─────────────────┘  remove_model (last model) / TTL  └───────────────┘
                        expiry — back to design phase
```

So **design phase → always + design-only**, **run phase → always + run-only**.
Design/reference tools are hidden once a model is loaded, keeping the run
surface focused on querying.

### Re-listing

The MCP `tools/list` response is filtered to the active phase. Because the
stateless MCP spec makes push notifications (`notifications/tools/list_changed`)
unreliable, transitions are **pull-based**: `load_model` (design → run) and
`remove_model` (run → design, once no models remain) return a short signal
telling the client to **re-list its tools** and pick up the swapped surface.

### Guard against premature calls

If a client calls a run-only verb while still in the design phase (e.g. a stale
host that hasn't re-listed yet), the server returns a **structured error**
rather than an opaque failure:

> No model loaded — '`compile_query`' is a run-time tool and is not available
> yet. Call `load_model` first, then re-list tools.

### Capability gating (orthogonal to phase)

Separately from lifecycle phase, a tool can be hidden because the server is
_configured_ not to support it. The execution tools `execute_query` /
`execute_obsql` are gated on the API's `query_execute` capability: when the
server runs **compile-only** they are dropped from `tools/list` and calling them
returns a structured error (`compile_query` / `compile_obsql` stay available so
you can still generate SQL). This composes with phase — a verb is listed only if
its **phase is active _and_ its capability is enabled**. The mechanism is a
general capability registry, so future "the server can't do X here" flags hide
their tools the same way.

### Single-model mode

When the API runs in **single-model mode** a model is pre-loaded at startup, so
the server is permanently in the run-time phase — every applicable tool is
listed from the first request and there is no `load_model` step.

> **Note on caching hints.** The `2026-07-28` MCP spec adds `ttlMs` / `cacheScope`
> hints on `tools/list` (SEP-2549). These are intentionally **not** set yet — the
> fields are a release candidate, and FastMCP's list-tools hook exposes only the
> tool list, not the result envelope. The explicit re-list signal above is the
> primary (and spec-recommended) transition mechanism in the meantime.

## Supported SQL Dialects

`postgres`, `snowflake`, `clickhouse`, `databricks`, `dremio`, `bigquery`, `duckdb`

## Workflow

1. **Get reference** — call `get_obml_reference()` to learn OBML syntax
2. **Load model** — call `load_model(model_yaml)` to get a `model_id`
3. **Explore** — call `describe_model(model_id)` or use discovery tools (`list_artefacts`, `find_artefacts`, `explain_artefact`)
4. **Query** — call `compile_query(model_id, dimensions=[...], measures=[...])` to generate SQL
5. **Execute** — call `execute_query(model_id, dimensions=[...], measures=[...])` to run SQL and get results (requires `QUERY_EXECUTE=true` on the API)

## Integration Guides

Use the OrionBelt Semantic Layer MCP server with popular AI agent frameworks and automation platforms:

| Framework             | Transport        | Guide                                                                            |
| --------------------- | ---------------- | -------------------------------------------------------------------------------- |
| **OpenAI Agents SDK** | stdio, HTTP, SSE | [docs/integrations/openai-agents-sdk.md](docs/integrations/openai-agents-sdk.md) |
| **LangChain**         | stdio, HTTP      | [docs/integrations/langchain.md](docs/integrations/langchain.md)                 |
| **Google ADK**        | stdio, HTTP, SSE | [docs/integrations/google-adk.md](docs/integrations/google-adk.md)               |
| **n8n**               | HTTP, SSE        | [docs/integrations/n8n.md](docs/integrations/n8n.md)                             |
| **CrewAI**            | stdio, HTTP      | [docs/integrations/crewai.md](docs/integrations/crewai.md)                       |

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
> `{"url": "…"}` entry is rejected with _"not valid MCP server configurations
> and were skipped"_. `mcp-remote` runs a local stdio bridge that forwards to
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
