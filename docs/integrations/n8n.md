# n8n Integration

Connect the OrionBelt Semantic Layer MCP server to [n8n](https://n8n.io) — a low-code workflow automation platform with built-in MCP client support. This enables AI-powered analytics workflows that combine semantic queries with other data sources, notifications, and business logic — all without writing code.

## Prerequisites

- n8n **1.76+** (MCP client node support)
- OrionBelt MCP server running in **HTTP** or **SSE** transport mode (n8n connects over the network)

### Start the MCP server

```bash
# Option 1: Local server with HTTP transport
cd /path/to/orionbelt-semantic-layer-mcp
MCP_TRANSPORT=http uv run python server.py
# Server runs at http://localhost:9000

# Option 2: Use the hosted demo (no local setup needed)
# URL: https://orionbelt.ralforion.com/mcp
```

## Setup in n8n

### Step 1 — Add MCP Server credentials

1. Go to **Settings > Credentials > Add Credential**
2. Search for **MCP Client**
3. Configure the connection:

| Field | Value |
|-------|-------|
| **Connection Type** | Streamable HTTP |
| **URL** | `http://localhost:9000/mcp` (local) or `https://orionbelt.ralforion.com/mcp` (hosted demo) |

4. Click **Save**

### Step 2 — Use the MCP Client node

1. Add an **MCP Client** node to your workflow
2. Select the credential created above
3. Choose **List Tools** to see all available OrionBelt tools
4. Select a tool (e.g., `describe_model`, `execute_query`)
5. Fill in the required parameters

## Example workflows

### Workflow 1: Scheduled analytics report

Generate a daily SQL query from the semantic model and send it via email.

```
Schedule Trigger (daily 8am)
  → MCP Client: list_measures()
  → MCP Client: execute_query(query_json='{"select": {"dimensions": ["Date","Region"], "measures": ["Revenue"]}}')
  → Email: send compiled SQL to analytics team
```

**Node configuration:**

1. **Schedule Trigger** — set to daily at 08:00
2. **MCP Client** node (compile query):
   - Tool: `execute_query`
   - Parameters:
     - `dimensions`: `["Date", "Region"]`
     - `measures`: `["Revenue"]`
     - `dialect`: `bigquery`
3. **Send Email** node — use `{{ $json.sql }}` in the body

### Workflow 2: AI-powered data Q&A chatbot

Combine an AI agent with OrionBelt tools to answer natural language data questions.

```
Chat Trigger (webhook)
  → AI Agent (OpenAI / Anthropic)
      ├── MCP Client Tool: OrionBelt (all tools)
      └── respond to user
```

**Node configuration:**

1. **Chat Trigger** — receives user questions via webhook
2. **AI Agent** node:
   - Model: GPT-4o or Claude
   - System prompt: *"You are a data analyst. Use the OrionBelt Semantic Layer tools to explore models and compile queries. Always explain the SQL you generate."*
   - Tools: attach the **MCP Client** node as a tool
3. The agent automatically selects the right OrionBelt tool based on the user's question

### Workflow 3: Model validation pipeline

Validate OBML models from a Git repository on every push.

```
GitHub Trigger (push)
  → HTTP Request: fetch OBML file from repo
  → MCP Client: validate_model(model_yaml)
  → IF validation errors
      → Slack: notify #data-engineering channel
  → ELSE
      → MCP Client: execute_query (smoke test)
      → Slack: notify success
```

### Workflow 4: Data catalog sync

Keep an external catalog updated with model metadata.

```
Schedule Trigger (hourly)
  → MCP Client: describe_model()
  → MCP Client: list_dimensions()
  → MCP Client: list_measures()
  → MCP Client: list_metrics()
  → Code: transform to catalog format
  → HTTP Request: POST to data catalog API
```

## Using with the AI Agent node

n8n's **AI Agent** node can use MCP tools directly, enabling natural language interaction with the semantic layer:

1. Add an **AI Agent** node
2. Connect your LLM (OpenAI, Anthropic, etc.)
3. Under **Tools**, add an **MCP Client** tool node
4. Configure the MCP Client with the OrionBelt server credentials
5. The AI agent will automatically discover and use all OrionBelt tools

System prompt suggestion for the AI Agent:

> You are a data analytics assistant connected to the OrionBelt Semantic Layer.
> Available actions: explore models (describe_model, list_artefacts, find_artefacts),
> and compile + execute queries (execute_query).
> Always use the appropriate dialect for the target database.

## Available tools in n8n

All tools from the MCP server are available. Most commonly used:

| Tool | Purpose |
|------|---------|
| `describe_model(...)` | Inspect model structure |
| `execute_query(...)` | Run query and get results |
| `list_dimensions(...)` | Browse available dimensions |
| `list_measures(...)` | Browse available measures |
| `find_artefacts(...)` | Search by name or synonym |
| `validate_model(model_yaml)` | Validate OBML without loading |
| `get_model_diagram(...)` | Generate Mermaid ER diagram |

See the full [tool reference](../../README.md#tools) for all available tools.

## Tips

- **Streamable HTTP** transport is recommended for n8n. SSE also works but may have timeout issues with long-running connections.
- Use n8n's **expression editor** to dynamically build tool parameters from previous nodes (e.g., `{{ $json.dimensions }}`).
- For production workflows, deploy the MCP server with `MCP_SERVER_HOST=0.0.0.0` so n8n can reach it from a different host.
