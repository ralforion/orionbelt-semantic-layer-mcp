# Google Agent Development Kit (ADK) Integration

Connect the OrionBelt Semantic Layer MCP server to [Google ADK](https://google.github.io/adk-docs/) so that Gemini-powered agents can explore semantic models, compile queries, and execute analytics — all via MCP tools.

## Prerequisites

```bash
pip install google-adk
```

## Option A — stdio (local server)

Use `MCPToolset.from_server` with `StdioServerParameters` to launch the MCP server as a subprocess.

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import MCPToolset, StdioServerParameters
from google.genai import types

async def main():
    tools, exit_stack = await MCPToolset.from_server(
        connection_params=StdioServerParameters(
            command="uv",
            args=["run", "python", "server.py"],
            env={
                "API_BASE_URL": "https://orionbelt.ralforion.com",
            },
            cwd="/path/to/orionbelt-semantic-layer-mcp",
        )
    )

    agent = Agent(
        name="analytics_agent",
        model="gemini-2.0-flash",
        instruction=(
            "You are a data analytics assistant. "
            "Use the OrionBelt Semantic Layer tools to explore models, "
            "compile semantic queries to SQL, and execute them."
        ),
        tools=tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_app", user_id="user1"
    )

    runner = Runner(
        agent=agent,
        app_name="orionbelt_app",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text="Describe the loaded model and list its measures.")],
    )

    async for event in runner.run_async(
        user_id="user1", session_id=session.id, new_message=content
    ):
        if event.is_final_response():
            print(event.content.parts[0].text)

    # Clean up MCP connection
    await exit_stack.aclose()

asyncio.run(main())
```

## Option B — Streamable HTTP (remote / hosted server)

Connect to the hosted demo or a self-hosted MCP server over HTTP.

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPServerParameters
from google.genai import types

async def main():
    tools, exit_stack = await MCPToolset.from_server(
        connection_params=StreamableHTTPServerParameters(
            url="https://orionbelt.ralforion.com/mcp",
        )
    )

    agent = Agent(
        name="analytics_agent",
        model="gemini-2.0-flash",
        instruction=(
            "You are a data analytics assistant. "
            "Use the OrionBelt Semantic Layer tools to explore models, "
            "compile semantic queries to SQL, and execute them."
        ),
        tools=tools,
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_app", user_id="user1"
    )

    runner = Runner(
        agent=agent,
        app_name="orionbelt_app",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text="Find revenue-related measures and compile a query by region.")],
    )

    async for event in runner.run_async(
        user_id="user1", session_id=session.id, new_message=content
    ):
        if event.is_final_response():
            print(event.content.parts[0].text)

    await exit_stack.aclose()

asyncio.run(main())
```

## Option C — SSE (legacy)

For SSE transport:

```python
from google.adk.tools.mcp_tool import MCPToolset, SseServerParameters

tools, exit_stack = await MCPToolset.from_server(
    connection_params=SseServerParameters(
        url="http://localhost:9000/sse",
    )
)
```

## Multi-agent example

Combine OrionBelt tools with other ADK agents in a hierarchy:

```python
import asyncio
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPServerParameters
from google.genai import types

async def main():
    tools, exit_stack = await MCPToolset.from_server(
        connection_params=StreamableHTTPServerParameters(
            url="https://orionbelt.ralforion.com/mcp",
        )
    )

    # Sub-agent: model explorer
    model_explorer = Agent(
        name="model_explorer",
        model="gemini-2.0-flash",
        instruction=(
            "You explore semantic models. Use describe_model, "
            "list_dimensions, list_measures, and explain_artefact "
            "to understand and explain the data model."
        ),
        tools=tools,
    )

    # Sub-agent: query runner
    query_runner = Agent(
        name="query_runner",
        model="gemini-2.0-flash",
        instruction=(
            "You compile and execute semantic queries. Use execute_query "
            "to compile and run a QueryObject (query_json)."
        ),
        tools=tools,
    )

    # Orchestrator agent with sub-agents
    orchestrator = Agent(
        name="analytics_orchestrator",
        model="gemini-2.0-flash",
        instruction=(
            "You coordinate analytics tasks. "
            "Delegate model exploration to model_explorer "
            "and query execution to query_runner."
        ),
        sub_agents=[model_explorer, query_runner],
    )

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="orionbelt_app", user_id="user1"
    )

    runner = Runner(
        agent=orchestrator,
        app_name="orionbelt_app",
        session_service=session_service,
    )

    content = types.Content(
        role="user",
        parts=[types.Part(text="Explore the model, then query total revenue by region.")],
    )

    async for event in runner.run_async(
        user_id="user1", session_id=session.id, new_message=content
    ):
        if event.is_final_response():
            print(event.content.parts[0].text)

    await exit_stack.aclose()

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
