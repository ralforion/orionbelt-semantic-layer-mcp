# OpenAI Agents SDK Integration

Connect the OrionBelt Semantic Layer MCP server to [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) so that OpenAI-powered agents can discover semantic models, compile queries, and execute analytics — all via MCP tools.

## Prerequisites

```bash
pip install openai-agents
```

## Option A — stdio (local server)

The agent launches the MCP server as a subprocess. No network setup needed.

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStdio

async def main():
    # Launch the OrionBelt MCP server via stdio
    async with MCPServerStdio(
        name="OrionBelt Semantic Layer",
        params={
            "command": "uv",
            "args": ["run", "python", "server.py"],
            "env": {
                "API_BASE_URL": "https://orionbelt.ralforion.com",  # demo API
            },
        },
        cwd="/path/to/orionbelt-semantic-layer-mcp",
    ) as server:
        agent = Agent(
            name="Analytics Agent",
            instructions=(
                "You are a data analytics assistant. "
                "Use the OrionBelt Semantic Layer tools to explore models, "
                "compile semantic queries to SQL, and execute them."
            ),
            mcp_servers=[server],
        )

        result = await Runner.run(
            agent,
            "Load the sample model and list all available measures.",
        )
        print(result.final_output)

asyncio.run(main())
```

## Option B — Streamable HTTP (remote / hosted server)

Connect to a running MCP server over HTTP — works with the hosted demo or any self-hosted instance.

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

async def main():
    # Connect to the hosted demo (no local setup needed)
    async with MCPServerStreamableHttp(
        name="OrionBelt Semantic Layer",
        params={
            "url": "https://orionbelt.ralforion.com/mcp",
        },
    ) as server:
        agent = Agent(
            name="Analytics Agent",
            instructions=(
                "You are a data analytics assistant. "
                "Use the OrionBelt Semantic Layer tools to explore models, "
                "compile semantic queries to SQL, and execute them."
            ),
            mcp_servers=[server],
        )

        result = await Runner.run(
            agent,
            "Describe the loaded model and show me its dimensions.",
        )
        print(result.final_output)

asyncio.run(main())
```

## Option C — SSE (legacy)

For older deployments using Server-Sent Events transport:

```python
from agents.mcp import MCPServerSse

async with MCPServerSse(
    name="OrionBelt Semantic Layer",
    params={
        "url": "http://localhost:9000/sse",
    },
) as server:
    # ... same agent setup as above
```

## Multi-agent example

Create specialized agents that collaborate using OrionBelt tools:

```python
import asyncio
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp

async def main():
    async with MCPServerStreamableHttp(
        name="OrionBelt Semantic Layer",
        params={
            "url": "https://orionbelt.ralforion.com/mcp",
        },
    ) as server:
        # Agent 1: Model expert — explores and explains the semantic model
        model_expert = Agent(
            name="Model Expert",
            instructions=(
                "You explore semantic models. Use describe_model, "
                "list_dimensions, list_measures, and explain_artefact "
                "to understand and explain the data model."
            ),
            mcp_servers=[server],
        )

        # Agent 2: Query builder — compiles and executes queries
        query_builder = Agent(
            name="Query Builder",
            instructions=(
                "You write semantic queries. Use execute_query to compile and run a "
                "QueryObject (query_json) and return results. "
                "Always pick the correct dialect for the target database."
            ),
            mcp_servers=[server],
        )

        # Agent 3: Orchestrator — delegates to specialists
        orchestrator = Agent(
            name="Analytics Orchestrator",
            instructions=(
                "You coordinate analytics tasks. "
                "Hand off model exploration to Model Expert "
                "and query tasks to Query Builder."
            ),
            handoffs=[model_expert, query_builder],
        )

        result = await Runner.run(
            orchestrator,
            "Explore the model, then query total revenue by region.",
        )
        print(result.final_output)

asyncio.run(main())
```

## Key tools available to the agent

| Tool | Purpose |
|------|---------|
| `get_obml_reference()` | Learn OBML syntax |
| `load_model(model_yaml)` | Load a semantic model (multi-model mode) |
| `describe_model(...)` | Inspect model structure |
| `execute_query(...)` | Run query and get results |
| `list_dimensions(...)` | Browse available dimensions |
| `list_measures(...)` | Browse available measures |
| `find_artefacts(...)` | Search by name or synonym |
| `get_model_diagram(...)` | Generate Mermaid ER diagram |

See the full [tool reference](../../README.md#tools) for all available tools.
