"""Thin MCP server for OrionBelt Semantic Layer.

Delegates all business logic to the OrionBelt Semantic Layer REST API via HTTP.
No embedded engine — pure API pass-through.

Run via::

    uv run python server.py                        # stdio (default)
    MCP_TRANSPORT=http uv run python server.py     # streamable HTTP on port 9000

Entrypoint for Prefect Horizon: ``server.py:mcp``
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Literal, NoReturn
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts.prompt import Prompt as _BasePrompt
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger("orionbelt.mcp")


class Settings(BaseSettings):
    """Configuration loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_base_url: str
    mcp_transport: Literal["stdio", "http", "sse"] = "stdio"
    mcp_server_host: str = "localhost"
    mcp_server_port: int = 9000
    log_level: str = "INFO"
    api_timeout: int = 30


settings = Settings()

# All API routes (except /health) are under the /v1 prefix since API v1.0.0
_API_V1 = "/v1"

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("OrionBelt Semantic Layer")

# ---------------------------------------------------------------------------
# Internal session management
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_api_session_id: str | None = None
_http_client: httpx.Client | None = None


def _get_client() -> httpx.Client:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None:
        with _state_lock:
            if _http_client is None:  # double-check under lock
                _http_client = httpx.Client(
                    base_url=settings.api_base_url,
                    timeout=settings.api_timeout,
                    headers={"User-Agent": "OrionBelt-MCP/1.0"},
                )
    return _http_client


def _create_api_session() -> str:
    """Create a new API session and return its session_id."""
    client = _get_client()
    try:
        resp = client.post(f"{_API_V1}/sessions", json={"metadata": {"source": "mcp"}})
        resp.raise_for_status()
    except httpx.ConnectError:
        raise ToolError(
            f"Cannot connect to OrionBelt Semantic Layer API at {settings.api_base_url}"
        ) from None
    except httpx.TimeoutException:
        raise ToolError("API request timed out while creating session") from None
    except httpx.HTTPStatusError as exc:
        _raise_api_error(exc.response)
    data = _parse_json(resp)
    return data["session_id"]


def _ensure_session() -> str:
    """Return the cached session ID, creating one if needed."""
    global _api_session_id
    if _api_session_id is None:
        with _state_lock:
            if _api_session_id is None:  # double-check under lock
                _api_session_id = _create_api_session()
                logger.info("Created API session: %s", _api_session_id)
    return _api_session_id


def _invalidate_session() -> None:
    """Clear the cached session ID (e.g. on 404)."""
    global _api_session_id
    with _state_lock:
        _api_session_id = None


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------


def _parse_json(resp: httpx.Response):
    """Parse JSON from a successful API response, raising ToolError on failure."""
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"API returned invalid JSON: {exc}") from None


def _parse_error_detail(response: httpx.Response) -> str:
    """Extract error detail string from an API error response."""
    try:
        body = response.json()
        return str(body.get("detail", response.text))
    except (ValueError, json.JSONDecodeError):
        return response.text


def _raise_api_error(response: httpx.Response, detail: str | None = None) -> NoReturn:
    """Raise ToolError from an API error response."""
    if detail is None:
        detail = _parse_error_detail(response)
    raise ToolError(f"API error ({response.status_code}): {detail}")


def _is_session_expired(response: httpx.Response) -> bool:
    """Return True if the API error indicates an expired/missing session.

    Checks for a structured ``code`` field first, then falls back to string
    matching on the detail message.
    """
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return False
    # Prefer structured error code when available
    if body.get("code") == "SESSION_NOT_FOUND":
        return True
    # Fallback: match on detail text
    detail = str(body.get("detail", "")).lower()
    return "session" in detail and "not found" in detail


def _do_request(
    client: httpx.Client,
    method: str,
    path: str,
    json_body: dict | None,
) -> httpx.Response:
    """Execute a single HTTP request, wrapping connection/timeout errors."""
    try:
        return client.request(method, path, json=json_body)
    except httpx.ConnectError:
        raise ToolError(
            f"Cannot connect to OrionBelt Semantic Layer API at {settings.api_base_url}"
        ) from None
    except httpx.TimeoutException:
        raise ToolError("API request timed out") from None


def _api_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    retry_on_expired: bool = True,
    path_suffix: str | None = None,
) -> httpx.Response:
    """Make an API request with auto-session retry.

    If the session returns 404 and retry_on_expired is True,
    re-create the session and retry once.  When *path_suffix* is provided,
    the retry reconstructs the path from the new session ID.
    """
    client = _get_client()
    resp = _do_request(client, method, path, json_body)

    if _is_session_expired(resp) and retry_on_expired and path_suffix is not None:
        # Session expired — recreate and retry once
        _invalidate_session()
        sid = _ensure_session()
        new_path = f"{_API_V1}/sessions/{sid}{path_suffix}"
        resp = _do_request(client, method, new_path, json_body)

    if resp.status_code >= 400:
        _raise_api_error(resp)

    return resp


def _session_request(
    method: str,
    path_suffix: str,
    *,
    json_body: dict | None = None,
) -> httpx.Response:
    """Make an API request scoped to the current session.

    Automatically ensures a session exists.
    """
    sid = _ensure_session()
    path = f"{_API_V1}/sessions/{sid}{path_suffix}"
    return _api_request(method, path, json_body=json_body, path_suffix=path_suffix)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

OBML_REFERENCE = """\
# OBML (OrionBelt ML) Reference

OBML is a YAML-based semantic model format. A model has four top-level sections:

## 1. dataObjects — physical tables/views

```yaml
dataObjects:
  Orders:                         # data object name
    code: ORDERS                  # physical table/view name
    database: EDW                 # database
    schema: SALES_MART            # schema
    columns:
      Order ID:                   # column name — must be unique within this data object
        code: ID                  # physical column name
        abstractType: string      # see abstractType values below
      Amount:
        code: AMOUNT
        abstractType: float
    joins:                        # optional — defined on fact tables
      - joinType: many-to-one     # many-to-one | one-to-one
        joinTo: Customers         # target data object name
        columnsFrom:
          - Customer ID           # local column name
        columnsTo:
          - Customer ID           # target column name
```

## 2. dimensions — named analytical dimensions

```yaml
dimensions:
  Customer Country:
    dataObject: Customers         # which data object owns this dimension
    column: Country               # column within that data object
    resultType: string            # data type of the result (informative only)
    timeGrain: month              # optional: year | quarter | month | week | day | hour
```

## 3. measures — aggregations

```yaml
measures:
  Total Revenue:                  # measure name
    columns:                      # column references (for simple aggregations)
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum              # see aggregation values below
    total: false                  # optional: use total (unfiltered) value in metrics

  Profit:                         # expression-based measure
    resultType: float
    aggregation: sum
    expression: '{[Orders].[Amount]} - {[Orders].[Cost]}'  # {[DataObject].[Column]} syntax

  Filtered Measure:               # measure with a filter
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum
    filter:
      column:
        dataObject: Orders
        column: Status
      operator: equals            # equals | gt | gte | lt | lte | in | not_in | ...
      values:
        - dataType: string
          valueString: completed
```

## 4. metrics — composite calculations from measures

```yaml
metrics:
  Profit Margin:
    expression: '{[Profit]} / {[Total Revenue]}'  # {[Measure Name]} syntax
```

## abstractType Values

string, int, float, date, time, time_tz, timestamp,
timestamp_tz, boolean, json

## Aggregation Values

sum, count, count_distinct, avg, min, max,
any_value, median, mode, listagg

## 5. synonyms — alternative names (optional, LLM hints)

All five element levels (dataObject, column, dimension, measure, metric) support
an optional `synonyms` list — alternative names or terms that help LLMs
map natural-language questions to the correct model element:

```yaml
dataObjects:
  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
    synonyms: [client, buyer, purchaser]
    columns:
      Country:
        code: COUNTRY
        abstractType: string
        synonyms: [nation, region]

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    synonyms: [client country, buyer country]

measures:
  Revenue:
    aggregation: sum
    expression: '{[Orders].[Amount]}'
    synonyms: [sales, income, turnover]
```

## 6. customExtensions — vendor-keyed metadata (optional)

All six levels (model, dataObject, column, dimension, measure, metric) support
an optional `customExtensions` array for vendor-specific metadata:

```yaml
customExtensions:
  - vendor: OSI
    data: '{"instructions": "Use for retail analytics", "synonyms": ["sales"]}'
  - vendor: GOVERNANCE
    data: '{"owner": "data-team", "classification": "internal"}'
```

Each entry has `vendor` (identifier string) and `data` (opaque JSON string).
OrionBelt preserves these during parsing but does not interpret them.

## Key Rules

1. **Column names are unique within each data object**.
   Dimensions, measures, and metrics must be unique across the model.
2. Measure expressions use `{[DataObject].[Column]}` to reference columns.
3. Metric expressions use `{[Measure Name]}` to reference measures by name.
4. Joins are defined on fact tables pointing to dimension tables \
(many-to-one or one-to-one).
5. A dimension references exactly one `dataObject` + `column` pair.

## Complete Minimal Example

```yaml
version: 1.0

dataObjects:
  Orders:
    code: ORDERS
    database: EDW
    schema: SALES
    columns:
      Order ID:
        code: ID
        abstractType: string
      Customer ID:
        code: CUST_ID
        abstractType: string
      Amount:
        code: AMOUNT
        abstractType: float
    joins:
      - joinType: many-to-one
        joinTo: Customers
        columnsFrom:
          - Customer ID
        columnsTo:
          - Cust ID

  Customers:
    code: CUSTOMERS
    database: EDW
    schema: SALES
    columns:
      Cust ID:
        code: ID
        abstractType: string
      Country:
        code: COUNTRY
        abstractType: string

dimensions:
  Customer Country:
    dataObject: Customers
    column: Country
    resultType: string

measures:
  Total Revenue:
    columns:
      - dataObject: Orders
        column: Amount
    resultType: float
    aggregation: sum

metrics:
  Revenue Per Order:
    expression: '{[Total Revenue]} / {[Order Count]}'
```

## Supported SQL Dialects

postgres, snowflake, clickhouse, databricks, dremio, bigquery, duckdb

## Workflow

1. `load_model(model_yaml)` — parse, validate, store → returns `model_id`
2. `describe_model(model_id)` — inspect data objects, dimensions, measures, metrics
3. `compile_query(model_id, dimensions=[...], measures=[...])` — generate SQL
"""


@mcp.resource("obml://reference")
def obml_reference() -> str:
    """Full OBML format reference — data objects, dimensions, measures, metrics, joins."""
    return OBML_REFERENCE


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
def get_obml_reference() -> str:
    """Get the OBML format reference.

    IMPORTANT: Call this tool BEFORE composing any OBML YAML to understand
    the correct syntax.  Returns the full specification with examples for
    dataObjects, dimensions, measures, metrics, joins, and expressions.
    """
    return OBML_REFERENCE


@mcp.tool
def load_model(model_yaml: str) -> str:
    """Load an OBML semantic model into a session.

    IMPORTANT: Before composing OBML YAML, call ``get_obml_reference()``
    first to learn the correct format.

    Parse, validate, and store the model.  Returns a model_id that you must
    pass to other tools (describe_model, compile_query, etc.).

    The OBML YAML must start with ``version: 1.0`` and uses YAML **mappings**
    (not lists) for all sections.  Quick structure::

        version: 1.0
        dataObjects:
          <Name>:                    # mapping key = data object name
            code: <TABLE>
            database: <DB>
            schema: <SCHEMA>
            columns:
              <Column Name>:         # unique within this data object
                code: <COLUMN>
                abstractType: string # see OBML reference for all types
            joins:                   # optional, on fact tables
              - joinType: many-to-one
                joinTo: <Target>
                columnsFrom: [<local column>]
                columnsTo: [<target column>]
        dimensions:
          <Dim Name>:
            dataObject: <Name>       # must match a dataObjects key
            column: <Column Name>    # must match a column in that object
            resultType: string
        measures:
          <Measure Name>:
            columns:
              - dataObject: <Name>
                column: <Column Name>
            resultType: float
            aggregation: sum         # see OBML reference for all types
        metrics:
          <Metric Name>:
            expression: '{[Measure A]} / {[Measure B]}'

    Args:
        model_yaml: Complete OBML YAML content (version 1.0).
    """
    logger.info("load_model called (yaml length=%d)", len(model_yaml))
    resp = _session_request("POST", "/models", json_body={"model_yaml": model_yaml})
    data = _parse_json(resp)

    parts = [
        f"Model loaded successfully.  model_id: {data['model_id']}",
        f"  data objects: {data['data_objects']}",
        f"  dimensions:   {data['dimensions']}",
        f"  measures:     {data['measures']}",
        f"  metrics:      {data['metrics']}",
    ]
    if data.get("warnings"):
        parts.append(f"  warnings: {'; '.join(data['warnings'])}")
    return "\n".join(parts)


@mcp.tool
def validate_model(model_yaml: str) -> str:
    """Validate an OBML model without storing it.

    Returns validation errors and warnings.  Useful for checking a model
    before loading it.

    Args:
        model_yaml: Complete OBML YAML content.
    """
    logger.info("validate_model called (yaml length=%d)", len(model_yaml))
    resp = _session_request("POST", "/validate", json_body={"model_yaml": model_yaml})
    data = _parse_json(resp)

    if data["valid"]:
        msg = "Model is valid."
        if data.get("warnings"):
            msg += "\nWarnings:"
            for w in data["warnings"]:
                msg += f"\n  [{w['code']}] {w['message']}"
        return msg

    lines = ["Model has validation errors:"]
    for e in data.get("errors", []):
        line = f"  [{e['code']}] {e['message']}"
        if e.get("path"):
            line += f"  (at {e['path']})"
        lines.append(line)
    if data.get("warnings"):
        lines.append("Warnings:")
        for w in data["warnings"]:
            lines.append(f"  [{w['code']}] {w['message']}")
    return "\n".join(lines)


@mcp.tool
def describe_model(model_id: str) -> str:
    """Describe the contents of a loaded model.

    Shows data objects (with columns and joins), dimensions, measures, and
    metrics.  Use this after ``load_model`` to explore the model.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}")
    desc = _parse_json(resp)

    lines: list[str] = [f"Model {model_id}:", ""]

    # Data objects
    lines.append("DATA OBJECTS:")
    for obj in desc.get("data_objects", []):
        lines.append(f"  {obj['label']}  (code: {obj['code']})")
        lines.append(f"    columns: {', '.join(obj.get('columns', []))}")
        if obj.get("join_targets"):
            lines.append(f"    joins to: {', '.join(obj['join_targets'])}")
        if obj.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(obj['synonyms'])}")
    lines.append("")

    # Dimensions
    lines.append("DIMENSIONS:")
    for dim in desc.get("dimensions", []):
        grain = f"  grain={dim['time_grain']}" if dim.get("time_grain") else ""
        lines.append(
            f"  {dim['name']}  ({dim['result_type']}, {dim['data_object']}.{dim['column']}{grain})"
        )
        if dim.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(dim['synonyms'])}")
    lines.append("")

    # Measures
    lines.append("MEASURES:")
    for m in desc.get("measures", []):
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        lines.append(f"  {m['name']}  ({m['result_type']}, {m['aggregation']}{expr})")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    lines.append("")

    # Metrics
    metrics = desc.get("metrics", [])
    if metrics:
        lines.append("METRICS:")
        for met in metrics:
            lines.append(f"  {met['name']}  expr: {met['expression']}")
            if met.get("synonyms"):
                lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool
def compile_query(
    model_id: str,
    dialect: str = "postgres",
    dimensions: list[str] | None = None,
    measures: list[str] | None = None,
    query_json: str | None = None,
    use_path_names: list[dict[str, str]] | None = None,
) -> str:
    """Compile a semantic query to SQL.

    Two modes:

    **Simple mode** — pass ``dimensions`` and ``measures`` lists directly::

        compile_query(model_id="abc12345", dimensions=["Country"], measures=["Revenue"])

    **Full mode** — pass a complete query as JSON via ``query_json``::

        compile_query(
            model_id="abc12345",
            query_json='{"select":{"dimensions":["Country"],"measures":["Revenue"]},"where":[{"field":"Country","op":"equals","value":"US"}],"order_by":[{"field":"Revenue","direction":"desc"}],"limit":10}'
        )

    The full query JSON supports: ``select`` (dimensions + measures), ``where``,
    ``having``, ``order_by``, ``limit``, ``usePathNames``.

    Use ``describe_model`` first to discover available dimension and measure
    names.  Filter operators: equals, notequals, gt, gte, lt, lte, inlist,
    notinlist, in, not_in, contains, notcontains, like, notlike, starts_with,
    ends_with, between, notbetween, set, notset, is_null, is_not_null,
    relative.

    For secondary joins, pass ``use_path_names`` (simple mode) or include
    ``usePathNames`` in query_json (full mode). Each item has ``source``,
    ``target``, and ``pathName`` keys.

    Args:
        model_id: The id returned by ``load_model``.
        dialect: Target SQL dialect (postgres, snowflake, clickhouse, databricks, dremio).
        dimensions: List of dimension names (simple mode).
        measures: List of measure names (simple mode).
        query_json: Full query object as JSON string (full mode).
        use_path_names: List of {source, target, pathName} dicts for
            selecting secondary joins (simple mode).
    """
    logger.info("compile_query called (model_id=%s, dialect=%s)", model_id, dialect)

    # Build the query object for the API
    if query_json is not None:
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid query JSON: {exc}") from exc
    elif dimensions is not None or measures is not None:
        query: dict = {  # type: ignore[no-redef]
            "select": {
                "dimensions": dimensions or [],
                "measures": measures or [],
            },
        }
        if use_path_names:
            query["usePathNames"] = use_path_names
    else:
        raise ToolError(
            "Provide either dimensions/measures (simple mode) or query_json (full mode)."
        )

    resp = _session_request(
        "POST",
        "/query/sql",
        json_body={"model_id": model_id, "dialect": dialect, "query": query},
    )
    data = _parse_json(resp)

    resolved = data.get("resolved", {})
    parts = [
        f"-- Dialect: {data['dialect']}",
        f"-- Fact tables: {', '.join(resolved.get('fact_tables', []))}",
        f"-- Dimensions: {', '.join(resolved.get('dimensions', []))}",
        f"-- Measures: {', '.join(resolved.get('measures', []))}",
        "",
        data["sql"],
    ]
    if not data.get("sql_valid", True):
        parts.append("")
        parts.append("-- WARNING: Generated SQL may not be valid for this dialect")
    if data.get("explain"):
        exp = data["explain"]
        parts.append("")
        parts.append(f"-- Planner: {exp['planner']} ({exp.get('planner_reason', '')})")
        parts.append(
            f"-- Base object: {exp['base_object']} ({exp.get('base_object_reason', '')})"
        )
        for j in exp.get("joins", []):
            parts.append(
                f"--   Join: {j['from_object']} -> {j['to_object']} ({j.get('reason', '')})"
            )
        if exp.get("has_totals"):
            parts.append(f"-- Totals: yes (CFL legs: {exp.get('cfl_legs', 0)})")
    if data.get("warnings"):
        parts.append("")
        parts.append(f"-- Warnings: {'; '.join(data['warnings'])}")
    return "\n".join(parts)


@mcp.tool
def list_models() -> str:
    """List all models currently loaded in a session."""
    resp = _session_request("GET", "/models")
    models = _parse_json(resp)
    if not models:
        return "No models loaded.  Use load_model to load one."
    lines = ["Loaded models:", ""]
    for m in models:
        lines.append(
            f"  {m['model_id']}  "
            f"({m['data_objects']} objects, {m['dimensions']} dims, "
            f"{m['measures']} measures, {m['metrics']} metrics)"
        )
    return "\n".join(lines)


@mcp.tool
def list_dialects() -> str:
    """List available SQL dialects and their capabilities."""
    resp = _api_request("GET", f"{_API_V1}/dialects", retry_on_expired=False)
    data = _parse_json(resp)
    lines = ["Available dialects:", ""]
    for d in data.get("dialects", []):
        caps = d.get("capabilities", {})
        enabled = [k for k, v in caps.items() if v]
        cap_str = ", ".join(enabled) if enabled else "(none)"
        lines.append(f"  {d['name']}: {cap_str}")
    return "\n".join(lines)


@mcp.tool
def get_model_diagram(
    model_id: str,
    show_columns: bool = True,
    theme: str = "default",
) -> str:
    """Generate a Mermaid ER diagram for a loaded model.

    Returns a Mermaid diagram script that visualises the data objects,
    columns, and join relationships in the model.

    Args:
        model_id: The id returned by ``load_model``.
        show_columns: Whether to include column details in the diagram.
        theme: Mermaid diagram theme (e.g. "default", "dark", "forest").
    """
    params = f"?show_columns={str(show_columns).lower()}&theme={theme}"
    resp = _session_request("GET", f"/models/{model_id}/diagram/er{params}")
    data = _parse_json(resp)
    return data["mermaid"]


@mcp.tool
def remove_model(model_id: str) -> str:
    """Remove a model from the current session.

    Args:
        model_id: The id returned by ``load_model``.
    """
    _session_request("DELETE", f"/models/{model_id}")
    return f"Model {model_id} removed."


@mcp.tool
def get_model_schema(model_id: str) -> str:
    """Get the full model structure as JSON.

    Returns a detailed JSON representation of the model including all data
    objects (with columns, types, comments, owners), dimensions, measures,
    metrics, and their synonyms.  More detailed than ``describe_model``.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}/schema")
    return json.dumps(_parse_json(resp), indent=2)


@mcp.tool
def list_dimensions(model_id: str) -> str:
    """List all dimensions in a model.

    Returns dimension details including data object, column, result type,
    time grain, and synonyms.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}/dimensions")
    dims = _parse_json(resp)
    if not dims:
        return "No dimensions in this model."
    lines = ["Dimensions:", ""]
    for d in dims:
        grain = f"  grain={d['time_grain']}" if d.get("time_grain") else ""
        lines.append(
            f"  {d['name']}  ({d['result_type']}, {d['data_object']}.{d['column']}{grain})"
        )
        if d.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(d['synonyms'])}")
    return "\n".join(lines)


@mcp.tool
def get_dimension(model_id: str, name: str) -> str:
    """Get a single dimension by name.

    Args:
        model_id: The id returned by ``load_model``.
        name: The dimension name.
    """
    resp = _session_request("GET", f"/models/{model_id}/dimensions/{quote(name, safe='')}")
    return json.dumps(_parse_json(resp), indent=2)


@mcp.tool
def list_measures(model_id: str) -> str:
    """List all measures in a model.

    Returns measure details including aggregation type, expression, result
    type, and synonyms.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}/measures")
    measures = _parse_json(resp)
    if not measures:
        return "No measures in this model."
    lines = ["Measures:", ""]
    for m in measures:
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        lines.append(f"  {m['name']}  ({m['result_type']}, {m['aggregation']}{expr})")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    return "\n".join(lines)


@mcp.tool
def get_measure(model_id: str, name: str) -> str:
    """Get a single measure by name.

    Args:
        model_id: The id returned by ``load_model``.
        name: The measure name.
    """
    resp = _session_request("GET", f"/models/{model_id}/measures/{quote(name, safe='')}")
    return json.dumps(_parse_json(resp), indent=2)


@mcp.tool
def list_metrics(model_id: str) -> str:
    """List all metrics in a model.

    Returns metric details including expression, component measures, and
    synonyms.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}/metrics")
    metrics = _parse_json(resp)
    if not metrics:
        return "No metrics in this model."
    lines = ["Metrics:", ""]
    for met in metrics:
        components = ", ".join(met.get("component_measures", []))
        lines.append(f"  {met['name']}  expr: {met['expression']}")
        if components:
            lines.append(f"    components: {components}")
        if met.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
    return "\n".join(lines)


@mcp.tool
def get_metric(model_id: str, name: str) -> str:
    """Get a single metric by name.

    Args:
        model_id: The id returned by ``load_model``.
        name: The metric name.
    """
    resp = _session_request("GET", f"/models/{model_id}/metrics/{quote(name, safe='')}")
    return json.dumps(_parse_json(resp), indent=2)


@mcp.tool
def explain_artefact(model_id: str, name: str) -> str:
    """Explain the lineage of a dimension, measure, or metric.

    Traces the composition chain from the named artefact down to the
    underlying data objects and columns.  Useful for understanding how a
    measure is computed or where a dimension originates.

    Args:
        model_id: The id returned by ``load_model``.
        name: The dimension, measure, or metric name to explain.
    """
    resp = _session_request("GET", f"/models/{model_id}/explain/{quote(name, safe='')}")
    data = _parse_json(resp)
    lines = [f"Explain: {data['name']}  (type: {data['type']})", ""]
    for item in data.get("lineage", []):
        detail = f"  — {item['detail']}" if item.get("detail") else ""
        lines.append(f"  [{item['type']}] {item['name']}{detail}")
    return "\n".join(lines)


@mcp.tool
def find_artefacts(
    model_id: str,
    query: str,
    types: list[str] | None = None,
) -> str:
    """Search across model artefacts by name or synonym.

    Finds dimensions, measures, metrics, and data objects whose name or
    synonym matches the search query (case-insensitive substring match).

    Args:
        model_id: The id returned by ``load_model``.
        query: Search term (matched against names and synonyms).
        types: Object types to search.  Defaults to all types:
            dimension, measure, metric, data_object.
    """
    body: dict = {"query": query}
    if types is not None:
        body["types"] = types
    resp = _session_request("POST", f"/models/{model_id}/find", json_body=body)
    data = _parse_json(resp)
    results = data.get("results", [])
    if not results:
        return f"No artefacts found matching '{query}'."
    lines = [f"Search results for '{query}':", ""]
    for r in results:
        lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    return "\n".join(lines)


@mcp.tool
def get_join_graph(model_id: str) -> str:
    """Return the join graph as an adjacency list.

    Shows the data object nodes and join edges (with cardinality and join
    columns) in the model.  Useful for understanding table relationships.

    Args:
        model_id: The id returned by ``load_model``.
    """
    resp = _session_request("GET", f"/models/{model_id}/join-graph")
    data = _parse_json(resp)
    lines = [f"Nodes: {', '.join(data.get('nodes', []))}", ""]
    edges = data.get("edges", [])
    if edges:
        lines.append("Edges:")
        for e in edges:
            cols = (
                f"  on ({', '.join(e['columns_from'])}) = ({', '.join(e['columns_to'])})"
                if e.get("columns_from")
                else ""
            )
            secondary = " [secondary]" if e.get("secondary") else ""
            path = f" path={e['path_name']}" if e.get("path_name") else ""
            lines.append(
                f"  {e['from_object']} --[{e['cardinality']}]--> "
                f"{e['to_object']}{cols}{secondary}{path}"
            )
    else:
        lines.append("No joins defined.")
    return "\n".join(lines)


@mcp.tool
def get_settings() -> str:
    """Get API configuration settings.

    Returns whether the API is in single-model mode, the session TTL,
    and any pre-loaded model YAML.
    """
    resp = _api_request("GET", f"{_API_V1}/settings", retry_on_expired=False)
    data = _parse_json(resp)
    lines = ["API Settings:", ""]
    lines.append(f"  Single-model mode: {data.get('single_model_mode', False)}")
    lines.append(f"  Session TTL: {data.get('session_ttl_seconds', 'N/A')}s")
    if data.get("model_yaml"):
        lines.append(f"  Pre-loaded model: yes ({len(data['model_yaml'])} chars)")
    return "\n".join(lines)


@mcp.tool
def convert_osi_to_obml(input_yaml: str) -> str:
    """Convert an OSI (Open Semantic Interchange) YAML model to OBML format.

    Takes an OSI-format YAML string and returns the equivalent OBML YAML
    along with any conversion warnings and validation results.

    Args:
        input_yaml: OSI YAML content to convert.
    """
    resp = _api_request(
        "POST",
        f"{_API_V1}/convert/osi-to-obml",
        json_body={"input_yaml": input_yaml},
        retry_on_expired=False,
    )
    data = _parse_json(resp)

    parts = [data["output_yaml"]]
    if data.get("warnings"):
        parts.append(f"\nWarnings: {'; '.join(data['warnings'])}")
    validation = data.get("validation", {})
    if not validation.get("schema_valid", True) or not validation.get("semantic_valid", True):
        errors = validation.get("schema_errors", []) + validation.get("semantic_errors", [])
        parts.append(f"\nValidation errors: {'; '.join(errors)}")
    if validation.get("semantic_warnings"):
        parts.append(f"\nValidation warnings: {'; '.join(validation['semantic_warnings'])}")
    return "\n".join(parts)


@mcp.tool
def convert_obml_to_osi(
    input_yaml: str,
    model_name: str = "semantic_model",
    model_description: str = "",
    ai_instructions: str = "",
) -> str:
    """Convert an OBML YAML model to OSI (Open Semantic Interchange) format.

    Takes an OBML-format YAML string and returns the equivalent OSI YAML
    along with any conversion warnings and validation results.

    Args:
        input_yaml: OBML YAML content to convert.
        model_name: Name for the OSI model.
        model_description: Description for the OSI model.
        ai_instructions: AI instructions for the OSI model.
    """
    resp = _api_request(
        "POST",
        f"{_API_V1}/convert/obml-to-osi",
        json_body={
            "input_yaml": input_yaml,
            "model_name": model_name,
            "model_description": model_description,
            "ai_instructions": ai_instructions,
        },
        retry_on_expired=False,
    )
    data = _parse_json(resp)

    parts = [data["output_yaml"]]
    if data.get("warnings"):
        parts.append(f"\nWarnings: {'; '.join(data['warnings'])}")
    validation = data.get("validation", {})
    if not validation.get("schema_valid", True) or not validation.get("semantic_valid", True):
        errors = validation.get("schema_errors", []) + validation.get("semantic_errors", [])
        parts.append(f"\nValidation errors: {'; '.join(errors)}")
    if validation.get("semantic_warnings"):
        parts.append(f"\nValidation warnings: {'; '.join(validation['semantic_warnings'])}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class StaticPrompt(_BasePrompt):
    """Prompt with static text exposed in prompts/list for Horizon compat."""

    text: str

    def to_mcp_prompt(self, **overrides):  # type: ignore[override]
        result = super().to_mcp_prompt(**overrides)
        result.text = self.text  # type: ignore[attr-defined]  # extra="allow"
        return result

    async def render(self, arguments=None):  # type: ignore[override]
        return self.text


_WRITE_OBML_MODEL_TEXT = """\
# OBML (OrionBelt ML) Syntax Reference

An OBML model is a YAML file with four top-level sections:

```yaml
version: 1.0

dataObjects:
  <ObjectName>:
    code: <TABLE_NAME>             # physical table/view name
    database: <DB>
    schema: <SCHEMA>
    columns:
      <Column Name>:              # unique within this data object
        code: <COLUMN>            # physical column name
        abstractType: string      # see abstractType values below
    joins:                        # optional — define on fact tables
      - joinType: many-to-one     # many-to-one | one-to-one
        joinTo: <TargetObject>
        columnsFrom:
          - <local column name>
        columnsTo:
          - <target column name>

dimensions:
  <Dimension Name>:
    dataObject: <ObjectName>       # which data object owns this dimension
    column: <Column Name>          # column within that data object
    resultType: string             # data type
    timeGrain: month               # optional: year | quarter | month | week | day | hour

measures:
  <Measure Name>:
    columns:                       # column references (for simple aggregations)
      - dataObject: <ObjectName>
        column: <Column Name>
    resultType: float
    aggregation: sum               # see aggregation values below
    expression: '{[Orders].[Amount]} - {[Orders].[Cost]}'  # {[DataObject].[Column]}
    filter:                        # optional measure-level filter
      column:
        dataObject: <ObjectName>
        column: <Column Name>
      operator: gt
      values:
        - dataType: float
          valueFloat: 100.0

metrics:
  <Metric Name>:
    expression: '{[Measure A]} / {[Measure B]}'   # {[Measure Name]} syntax

# Optional on dataObject, column, dimension, measure, metric:
# synonyms: [alternative name, ...]   # LLM hints for matching user intent

# Optional on any level: model, dataObject, column, dimension, measure, metric
customExtensions:
  - vendor: <VENDOR>
    data: '<JSON string>'
```

## abstractType Values

string, int, float, date, time, time_tz, timestamp,
timestamp_tz, boolean, json

## Aggregation Values

sum, count, count_distinct, avg, min, max,
any_value, median, mode, listagg

## Key Rules

1. **Column names are unique within each data object**.
   Dimensions, measures, and metrics must be unique across the model.
2. Measure expressions use `{[DataObject].[Column]}` to reference columns.
3. Metric expressions use `{[Measure Name]}` to reference measures.
4. Joins are defined on fact tables pointing to dimension tables.
5. A dimension references exactly one `dataObject` + `column` pair.

## Workflow

1. `load_model(model_yaml)` → get a `model_id`
2. `describe_model(model_id)` → see what's in the model
3. `compile_query(model_id, ...)` → generate SQL
"""

_WRITE_QUERY_TEXT = """\
# Compiling Queries with OrionBelt

## Simple Mode

Pass dimension and measure names directly:

```
compile_query(
  model_id="abc12345",
  dialect="postgres",
  dimensions=["Customer Country"],
  measures=["Total Revenue"]
)
```

## Full Mode (filters, ordering, limits)

Pass a complete query as JSON:

```
compile_query(
  model_id="abc12345",
  dialect="snowflake",
  query_json='{
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Total Revenue"]
    },
    "where": [
      {"field": "Customer Country", "op": "equals", "value": "US"}
    ],
    "order_by": [
      {"field": "Total Revenue", "direction": "desc"}
    ],
    "limit": 10
  }'
)
```

## Filter Operators

- Equality: `equals`, `notequals`, `=`, `!=`
- Comparison: `gt`, `gte`, `lt`, `lte`, `>`, `>=`, `<`, `<=`
- Set: `in`, `not_in`, `inlist`, `notinlist`
- Null: `is_null`, `is_not_null`, `set`, `notset`
- String: `contains`, `notcontains`, `like`, `notlike`, `starts_with`, `ends_with`
- Range: `between`, `notbetween`, `relative`

## Supported Dialects

`postgres`, `snowflake`, `clickhouse`, `databricks`, `dremio`, `bigquery`, `duckdb`

## Tips

- Use `describe_model` first to see available dimension/measure names.
- Use `list_dialects` to check dialect capabilities.
- Dimension names with time grain: append `:month`, `:year`, etc.
"""

_DEBUG_VALIDATION_TEXT = """\
# OBML Validation Error Codes

## Parse Errors

- `YAML_PARSE_ERROR`: Invalid YAML syntax.
  Fix: Check indentation, quoting, colons.
- `YAML_SAFETY_ERROR`: YAML safety constraint violated (anchors, oversized).
  Fix: OBML does not use anchors/aliases. Reduce document size.
- `DATA_OBJECT_PARSE_ERROR`: Cannot parse a data object.
  Fix: Check required fields (code, database, schema, columns).
- `DIMENSION_PARSE_ERROR`: Cannot parse a dimension definition.
  Fix: Check required fields (dataObject, column, resultType).
- `MEASURE_PARSE_ERROR`: Cannot parse a measure definition.
  Fix: Check required fields (aggregation, resultType) and either columns or expression.
- `METRIC_PARSE_ERROR`: Cannot parse a metric definition.
  Fix: Check required field (expression).

## Reference Errors

- `UNKNOWN_DATA_OBJECT`: References non-existent data object.
  Fix: Check spelling; suggestions are included.
- `UNKNOWN_COLUMN`: Column name not found in data object.
  Fix: Check column name spelling within the referenced data object.
- `UNKNOWN_DATA_OBJECT_IN_EXPRESSION`: Measure expression `{[DataObject].[Column]}` \
references unknown data object.
  Fix: Check data object name in the expression.
- `UNKNOWN_COLUMN_IN_EXPRESSION`: Measure expression `{[DataObject].[Column]}` \
references unknown column.
  Fix: Check column name in the expression.
- `UNKNOWN_MEASURE_REF`: Metric expression `{[Measure Name]}` references unknown measure.
  Fix: Check measure name in the expression.
- `UNKNOWN_MEASURE`: Query references missing measure.
  Fix: Check measure name in query select.
- `UNKNOWN_DIMENSION`: Query references missing dimension.
  Fix: Check dimension name in query select.
- `UNKNOWN_JOIN_TARGET`: `joinTo` references unknown data object.
  Fix: Check `joinTo` value matches a data object name.
- `UNKNOWN_JOIN_COLUMN`: Join column not found in data object.
  Fix: Check `columnsFrom`/`columnsTo` column names exist.
- `UNKNOWN_PATH_NAME`: `usePathNames` references non-existent path.
  Fix: Check source, target, and pathName match a secondary join.

## Semantic Errors

- `DUPLICATE_IDENTIFIER`: Duplicate name across data objects, dimensions, measures, or metrics.
  Fix: All names must be unique across the model.
- `CYCLIC_JOIN`: Join graph contains a cycle.
  Fix: Remove circular join references.
- `MULTIPATH_JOIN`: Multiple join paths between two data objects.
  Fix: Make join graph unambiguous, or use secondary joins with pathName.
- `JOIN_COLUMN_COUNT_MISMATCH`: `columnsFrom` and `columnsTo` have different lengths.
  Fix: Ensure both lists have the same number of entries.

## Secondary Join Errors

- `SECONDARY_JOIN_MISSING_PATH_NAME`: Secondary join has no `pathName`.
  Fix: Add a `pathName` to the secondary join.
- `DUPLICATE_JOIN_PATH_NAME`: Duplicate `pathName` for the same (source, target) pair.
  Fix: Use a unique `pathName` per (source, target) pair.

## Resolution Errors (at query time)

- `AMBIGUOUS_JOIN`: Multiple join paths found during query resolution.
  Fix: Make join graph unambiguous or use `usePathNames`.
- `INVALID_METRIC_EXPRESSION`: Metric expression could not be parsed.
  Fix: Use `{[Measure Name]}` syntax in metric expressions.
- `INVALID_FILTER_OPERATOR`: Unrecognised filter operator in query.
  Fix: Use a supported operator (equals, gt, gte, lt, lte, inlist, etc.).
- `INVALID_RELATIVE_FILTER`: Malformed relative time filter.
  Fix: Check unit (day/week/month/year), count, direction, include_current.
- `UNKNOWN_FILTER_FIELD`: Filter field is not a dimension (WHERE) or measure (HAVING).
  Fix: Check field name matches a dimension or measure in the model.
- `UNREACHABLE_FILTER_FIELD`: Filter dimension's data object is not reachable \
from the query's join graph.
  Fix: Ensure the data object is connected via joins to the queried tables.
- `UNKNOWN_ORDER_BY_FIELD`: ORDER BY field is not a dimension or measure in the query's SELECT.
  Fix: Use a field name from `select.dimensions` or `select.measures`, or a numeric position.
- `INVALID_ORDER_BY_POSITION`: Numeric ORDER BY position is out of range.
  Fix: Use a position between 1 and the number of SELECT columns.

## Debugging Steps

1. Run `validate_model(model_yaml)` to check for errors.
2. Read the error code and message carefully.
3. Fix the YAML and re-validate.
4. Once valid, use `load_model(model_yaml)` to load it.
"""

mcp.add_prompt(StaticPrompt(
    name="write_obml_model",
    description="OBML syntax reference — how to write a semantic model in YAML.",
    text=_WRITE_OBML_MODEL_TEXT,
    meta={"text": _WRITE_OBML_MODEL_TEXT},
))
mcp.add_prompt(StaticPrompt(
    name="write_query",
    description="How to use the compile_query tool — simple and full modes.",
    text=_WRITE_QUERY_TEXT,
    meta={"text": _WRITE_QUERY_TEXT},
))
mcp.add_prompt(StaticPrompt(
    name="debug_validation",
    description="All OBML validation error codes with causes and fixes.",
    text=_DEBUG_VALIDATION_TEXT,
    meta={"text": _DEBUG_VALIDATION_TEXT},
))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _check_api_health() -> None:
    """Check that the OrionBelt Semantic Layer API is reachable at startup."""
    client = _get_client()
    try:
        resp = _do_request(client, "GET", "/health", None)
        resp.raise_for_status()
        logger.info("API health check passed (%s)", settings.api_base_url)
    except ToolError:
        logger.error(
            "Cannot reach OrionBelt Semantic Layer API at %s — is the service running?",
            settings.api_base_url,
        )
        raise SystemExit(1) from None
    except httpx.HTTPStatusError as exc:
        logger.error("API health check failed: %s %s", exc.response.status_code, exc.response.text)
        raise SystemExit(1) from None


def main() -> None:
    """Run the MCP server using settings from environment / .env file."""
    logging.basicConfig(level=settings.log_level.upper())
    logger.info(
        "OrionBelt MCP Server (thin client) starting (transport=%s, api=%s)",
        settings.mcp_transport,
        settings.api_base_url,
    )

    _check_api_health()

    try:
        if settings.mcp_transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(
                transport=settings.mcp_transport,
                host=settings.mcp_server_host,
                port=settings.mcp_server_port,
                log_level=settings.log_level.lower(),
            )
    except KeyboardInterrupt:
        logger.info("Shutting down…")
    finally:
        # Best-effort session cleanup
        if _api_session_id is not None:
            try:
                client = _get_client()
                client.delete(f"{_API_V1}/sessions/{_api_session_id}")
                logger.info("Cleaned up API session: %s", _api_session_id)
            except Exception:
                logger.debug("Session cleanup failed (API TTL will handle it)")
        if _http_client is not None:
            _http_client.close()


if __name__ == "__main__":
    main()
