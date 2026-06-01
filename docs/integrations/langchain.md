# LangChain Integration

Connect the OrionBelt Semantic Layer MCP server to [LangChain](https://python.langchain.com/) using the [`langchain-mcp-adapters`](https://github.com/langchain-ai/langchain-mcp-adapters) package. This turns all MCP tools into LangChain-compatible tools that work with any LangChain agent.

## Prerequisites

```bash
pip install langchain-mcp-adapters langgraph langchain-openai
# or langchain-anthropic, langchain-google-genai, etc.
```

## Option A — stdio (local server)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o")

async with MultiServerMCPClient(
    {
        "orionbelt": {
            "command": "uv",
            "args": ["run", "python", "server.py"],
            "env": {
                "API_BASE_URL": "https://orionbelt.ralforion.com",
            },
            "cwd": "/path/to/orionbelt-semantic-layer-mcp",
            "transport": "stdio",
        }
    }
) as client:
    tools = client.get_tools()

    agent = create_react_agent(llm, tools)

    response = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Describe the model and list all measures."}]}
    )
    print(response["messages"][-1].content)
```

## Option B — Streamable HTTP (remote / hosted server)

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o")

async with MultiServerMCPClient(
    {
        "orionbelt": {
            "url": "https://orionbelt.ralforion.com/mcp",
            "transport": "streamable_http",
        }
    }
) as client:
    tools = client.get_tools()

    agent = create_react_agent(llm, tools)

    response = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Compile a query for revenue by region using BigQuery dialect."}]}
    )
    print(response["messages"][-1].content)
```

## Using with Anthropic (Claude)

```python
from langchain_anthropic import ChatAnthropic
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

llm = ChatAnthropic(model="claude-sonnet-4-20250514")

async with MultiServerMCPClient(
    {
        "orionbelt": {
            "url": "https://orionbelt.ralforion.com/mcp",
            "transport": "streamable_http",
        }
    }
) as client:
    agent = create_react_agent(llm, client.get_tools())

    response = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Find all revenue-related measures and explain their lineage."}]}
    )
    print(response["messages"][-1].content)
```

## Multiple MCP servers

LangChain's `MultiServerMCPClient` supports connecting to multiple MCP servers simultaneously:

```python
async with MultiServerMCPClient(
    {
        "orionbelt": {
            "url": "https://orionbelt.ralforion.com/mcp",
            "transport": "streamable_http",
        },
        "another-server": {
            "command": "npx",
            "args": ["-y", "@another/mcp-server"],
            "transport": "stdio",
        },
    }
) as client:
    # All tools from both servers are available
    tools = client.get_tools()
    agent = create_react_agent(llm, tools)
```

## Custom chain with tool selection

For finer control, build a chain that uses specific OrionBelt tools:

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(model="gpt-4o")

async with MultiServerMCPClient(
    {
        "orionbelt": {
            "url": "https://orionbelt.ralforion.com/mcp",
            "transport": "streamable_http",
        }
    }
) as client:
    tools = client.get_tools()

    # Filter to only query-related tools
    query_tools = [t for t in tools if t.name in ("execute_query", "list_dialects")]

    llm_with_tools = llm.bind_tools(query_tools)

    response = await llm_with_tools.ainvoke(
        [HumanMessage(content="Compile a query for total sales by product category in PostgreSQL dialect.")]
    )
    print(response)
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
