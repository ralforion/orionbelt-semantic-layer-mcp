# CrewAI Integration

Connect the OrionBelt Semantic Layer MCP server to [CrewAI](https://www.crewai.com/) so that multi-agent crews can explore semantic models, compile queries, and execute analytics — all via MCP tools.

## Prerequisites

```bash
pip install crewai crewai-tools
```

## Option A — stdio (local server)

Use `MCPServerAdapter` with `StdioServerParameters` to launch the MCP server as a subprocess.

```python
from crewai import Agent, Task, Crew
from crewai_tools.mcp import MCPServerAdapter, StdioServerParameters

server_params = StdioServerParameters(
    command="uv",
    args=["run", "python", "server.py"],
    env={
        "API_BASE_URL": "http://35.187.174.102",
    },
    cwd="/path/to/orionbelt-semantic-layer-mcp",
)

with MCPServerAdapter(server_params) as mcp_tools:
    tools = mcp_tools.tools

    analyst = Agent(
        role="Data Analyst",
        goal="Explore semantic models and compile analytical queries",
        backstory=(
            "You are an expert data analyst who uses the OrionBelt Semantic Layer "
            "to explore data models and generate SQL queries from semantic definitions."
        ),
        tools=tools,
    )

    task = Task(
        description=(
            "Describe the loaded model, list its measures, "
            "and compile a query for revenue by region."
        ),
        expected_output="Model description, list of measures, and compiled SQL query.",
        agent=analyst,
    )

    crew = Crew(agents=[analyst], tasks=[task])
    result = crew.kickoff()
    print(result)
```

## Option B — Streamable HTTP (remote / hosted server)

Connect to a running MCP server over HTTP — works with the hosted demo or a self-hosted instance.

```python
from crewai import Agent, Task, Crew
from crewai_tools.mcp import MCPServerAdapter, StreamableHTTPServerParameters

server_params = StreamableHTTPServerParameters(
    url="https://orionbelt-semantic-layer.fastmcp.app/mcp",
)

with MCPServerAdapter(server_params) as mcp_tools:
    tools = mcp_tools.tools

    analyst = Agent(
        role="Data Analyst",
        goal="Explore semantic models and compile analytical queries",
        backstory=(
            "You are an expert data analyst who uses the OrionBelt Semantic Layer "
            "to explore data models and generate SQL queries."
        ),
        tools=tools,
    )

    task = Task(
        description="Describe the loaded model and list all available dimensions.",
        expected_output="Model description and list of dimensions.",
        agent=analyst,
    )

    crew = Crew(agents=[analyst], tasks=[task])
    result = crew.kickoff()
    print(result)
```

## Multi-agent crew example

CrewAI excels at multi-agent collaboration. Here's a crew with specialized agents for model exploration and query generation:

```python
from crewai import Agent, Task, Crew, Process
from crewai_tools.mcp import MCPServerAdapter, StreamableHTTPServerParameters

server_params = StreamableHTTPServerParameters(
    url="https://orionbelt-semantic-layer.fastmcp.app/mcp",
)

with MCPServerAdapter(server_params) as mcp_tools:
    tools = mcp_tools.tools

    # Agent 1: Model Explorer — understands the semantic model
    model_explorer = Agent(
        role="Semantic Model Explorer",
        goal="Thoroughly explore and document the semantic model structure",
        backstory=(
            "You are a data modeling expert. You explore semantic models "
            "using describe_model, list_dimensions, list_measures, list_metrics, "
            "and explain_artefact to build a complete understanding of the data model."
        ),
        tools=tools,
    )

    # Agent 2: Query Engineer — compiles and optimizes queries
    query_engineer = Agent(
        role="Query Engineer",
        goal="Compile optimized semantic queries to SQL for the target database",
        backstory=(
            "You are a SQL expert. You use compile_query and execute_query "
            "to generate and run analytical queries. You always select "
            "the correct SQL dialect and optimize for performance."
        ),
        tools=tools,
    )

    # Agent 3: Analyst — interprets results and writes reports
    report_analyst = Agent(
        role="Report Analyst",
        goal="Interpret query results and produce clear analytical summaries",
        backstory=(
            "You are a business analyst who interprets data and writes "
            "clear, actionable reports for stakeholders."
        ),
    )

    # Tasks executed sequentially
    explore_task = Task(
        description=(
            "Explore the semantic model: describe it, list all dimensions, "
            "measures, and metrics. Identify the key business metrics."
        ),
        expected_output=(
            "A structured overview of the model including all dimensions, "
            "measures, metrics, and their relationships."
        ),
        agent=model_explorer,
    )

    query_task = Task(
        description=(
            "Based on the model exploration, compile a semantic query that "
            "shows revenue broken down by region and time period. "
            "Use the BigQuery dialect."
        ),
        expected_output="Compiled SQL query with explanation of the semantic mapping.",
        agent=query_engineer,
    )

    report_task = Task(
        description=(
            "Based on the model structure and compiled query, write a brief "
            "analytical summary explaining what insights this query will provide "
            "and how the semantic model supports the analysis."
        ),
        expected_output="A concise analytical summary for stakeholders.",
        agent=report_analyst,
    )

    crew = Crew(
        agents=[model_explorer, query_engineer, report_analyst],
        tasks=[explore_task, query_task, report_task],
        process=Process.sequential,
        verbose=True,
    )

    result = crew.kickoff()
    print(result)
```

## Using with different LLMs

CrewAI supports multiple LLM providers. Configure agents with your preferred model:

```python
from crewai import Agent

# OpenAI (default)
agent = Agent(
    role="Data Analyst",
    goal="...",
    backstory="...",
    llm="gpt-4o",
    tools=tools,
)

# Anthropic Claude
agent = Agent(
    role="Data Analyst",
    goal="...",
    backstory="...",
    llm="anthropic/claude-sonnet-4-20250514",
    tools=tools,
)

# Google Gemini
agent = Agent(
    role="Data Analyst",
    goal="...",
    backstory="...",
    llm="gemini/gemini-2.0-flash",
    tools=tools,
)
```

## Key tools available to agents

| Tool | Purpose |
|------|---------|
| `get_obml_reference()` | Learn OBML syntax |
| `load_model(model_yaml)` | Load a semantic model (multi-model mode) |
| `describe_model(...)` | Inspect model structure |
| `compile_query(...)` | Generate SQL from semantic query |
| `execute_query(...)` | Run query and get results |
| `list_dimensions(...)` | Browse available dimensions |
| `list_measures(...)` | Browse available measures |
| `find_artefacts(...)` | Search by name or synonym |
| `get_model_diagram(...)` | Generate Mermaid ER diagram |

See the full [tool reference](../../README.md#tools) for all available tools.
