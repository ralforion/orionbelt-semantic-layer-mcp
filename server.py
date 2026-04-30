"""Thin MCP server for OrionBelt Semantic Layer.

Delegates all business logic to the OrionBelt Semantic Layer REST API via HTTP.
No embedded engine — pure API pass-through.

Supports two modes:

- **Multi-model mode** (default): LLM loads models via ``load_model``, gets a
  ``model_id``, and passes it to every tool.
- **Single-model mode**: API has a pre-loaded model.  Session/model management
  tools are hidden; discovery and query tools use shortcut endpoints that
  auto-resolve session and model.

Run via::

    uv run python server.py                        # stdio (default)
    MCP_TRANSPORT=http uv run python server.py     # streamable HTTP on port 9000

Entrypoint for Prefect Horizon: ``server.py:mcp``
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import logging
import threading
from contextlib import asynccontextmanager
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


@asynccontextmanager
async def _server_lifespan(server):
    """Register mode-dependent tools when the server starts.

    Runs at actual server startup (not import time), guaranteeing the API
    is reachable for mode detection.  Used by both ``mcp.run()`` and
    Horizon's entrypoint (``server.py:mcp``).
    """
    _setup_mode_tools()
    yield
    # Best-effort session cleanup on shutdown
    global _http_client, _api_session_id
    if _api_session_id is not None:
        try:
            client = _get_client()
            client.delete(f"{_API_V1}/sessions/{_api_session_id}")
            logger.info("Cleaned up API session: %s", _api_session_id)
        except Exception:
            logger.debug("Session cleanup failed (API TTL will handle it)")
        finally:
            _api_session_id = None
    if _http_client is not None:
        _http_client.close()
        _http_client = None


mcp = FastMCP("OrionBelt Semantic Layer", lifespan=_server_lifespan)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_api_session_id: str | None = None
_http_client: httpx.Client | None = None
_single_model_mode: bool = False
_query_execute_enabled: bool = False
_tools_registered: bool = False

# ---------------------------------------------------------------------------
# HTTP client & session management
# ---------------------------------------------------------------------------


def _get_client() -> httpx.Client:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None:
        with _state_lock:
            if _http_client is None:  # double-check under lock
                try:
                    version = importlib.metadata.version("orionbelt-semantic-layer-mcp")
                except importlib.metadata.PackageNotFoundError:
                    version = "dev"
                _http_client = httpx.Client(
                    base_url=settings.api_base_url,
                    timeout=settings.api_timeout,
                    headers={"User-Agent": f"OrionBelt-MCP/{version}"},
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
        if exc.response.status_code == 429:
            detail = _parse_error_detail(exc.response)
            retry_after = exc.response.headers.get("Retry-After", "60")
            raise ToolError(
                f"Session creation rate-limited: {detail} (retry after {retry_after}s)"
            ) from None
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
        detail = body.get("detail", response.text)
        # Structured error detail (e.g. UnsupportedAggregationError)
        if isinstance(detail, dict):
            return detail.get("message", str(detail))
        return str(detail)
    except (ValueError, json.JSONDecodeError):
        return response.text


def _raise_api_error(response: httpx.Response, detail: str | None = None) -> NoReturn:
    """Raise ToolError from an API error response."""
    if detail is None:
        detail = _parse_error_detail(response)
    raise ToolError(f"API error ({response.status_code}): {detail}")


def _is_session_expired(response: httpx.Response) -> bool:
    """Return True if the API error indicates an expired/missing session.

    Matches 410 (Gone) for explicitly expired sessions, and 404 with
    session-related detail for backwards compatibility with older API versions.
    """
    # 410 Gone — API >= 1.4 uses this for expired sessions
    if response.status_code == 410:
        return True
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
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Execute a single HTTP request, wrapping connection/timeout errors."""
    try:
        return client.request(method, path, json=json_body, params=params)
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
    params: dict[str, str] | None = None,
    retry_on_expired: bool = True,
    path_suffix: str | None = None,
) -> httpx.Response:
    """Make an API request with auto-session retry.

    If the session returns 404/410 and retry_on_expired is True,
    re-create the session and retry once.  When *path_suffix* is provided,
    the retry reconstructs the path from the new session ID.
    """
    client = _get_client()
    resp = _do_request(client, method, path, json_body, params=params)

    if _is_session_expired(resp) and retry_on_expired and path_suffix is not None:
        # Session expired — recreate and retry once
        _invalidate_session()
        sid = _ensure_session()
        new_path = f"{_API_V1}/sessions/{sid}{path_suffix}"
        resp = _do_request(client, method, new_path, json_body, params=params)

    if resp.status_code >= 400:
        _raise_api_error(resp)

    return resp


def _session_request(
    method: str,
    path_suffix: str,
    *,
    json_body: dict | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Make an API request scoped to the current session.

    Automatically ensures a session exists.
    """
    sid = _ensure_session()
    path = f"{_API_V1}/sessions/{sid}{path_suffix}"
    return _api_request(
        method, path, json_body=json_body, params=params, path_suffix=path_suffix,
    )


def _shortcut_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """Make an API request to a shortcut endpoint (no session required).

    Used in single-model mode where the API auto-resolves session/model.
    """
    client = _get_client()
    full_path = f"{_API_V1}{path}"
    resp = _do_request(client, method, full_path, json_body, params=params)
    if resp.status_code >= 400:
        _raise_api_error(resp)
    return resp


# ---------------------------------------------------------------------------
# Resources & caches
# ---------------------------------------------------------------------------

_obml_reference_cache: str | None = None
_dialect_names_cache: list[str] | None = None


def _fetch_obml_reference() -> str:
    """Fetch and cache the OBML reference from the API."""
    global _obml_reference_cache
    if _obml_reference_cache is None:
        with _state_lock:
            if _obml_reference_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/reference/obml", retry_on_expired=False)
                data = _parse_json(resp)
                _obml_reference_cache = data["reference"]
    return _obml_reference_cache


def _fetch_dialect_names() -> list[str]:
    """Fetch and cache the list of supported dialect names from the API."""
    global _dialect_names_cache
    if _dialect_names_cache is None:
        with _state_lock:
            if _dialect_names_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/dialects", retry_on_expired=False)
                data = _parse_json(resp)
                _dialect_names_cache = [d["name"] for d in data.get("dialects", [])]
    return _dialect_names_cache


@mcp.resource("obml://reference")
def obml_reference() -> str:
    """Full OBML format reference — data objects, dimensions, measures, metrics, joins."""
    return _fetch_obml_reference()


# ---------------------------------------------------------------------------
# Mode-independent tools (always registered)
# ---------------------------------------------------------------------------


@mcp.tool
def get_obml_reference() -> str:
    """Get the OBML format reference.

    IMPORTANT: Call this tool BEFORE composing any OBML YAML to understand
    the correct syntax.  Returns the full specification with examples for
    dataObjects (including joins defined inside each dataObject),
    dimensions, measures, metrics, and expressions.
    """
    return _fetch_obml_reference()


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
        unsupported = d.get("unsupported_aggregations", [])
        line = f"  {d['name']}: {cap_str}"
        if unsupported:
            line += f"  (unsupported aggregations: {', '.join(unsupported)})"
        lines.append(line)
    return "\n".join(lines)


@mcp.tool
def get_settings() -> str:
    """Get API configuration settings.

    Returns whether the API is in single-model mode, the session TTL,
    query execution status, dialect/timezone resolution, and model
    settings when a model is loaded.
    """
    params: dict[str, str] = {}
    if not _single_model_mode and _api_session_id is not None:
        params["session_id"] = _api_session_id
    resp = _api_request(
        "GET", f"{_API_V1}/settings", retry_on_expired=False,
        params=params or None,
    )
    data = _parse_json(resp)
    lines = ["API Settings:", ""]
    if data.get("version"):
        lines.append(f"  API version: {data['version']}")
    if data.get("api_version"):
        lines.append(f"  API prefix: {data['api_version']}")
    lines.append(f"  Single-model mode: {data.get('single_model_mode', False)}")
    lines.append(f"  Session TTL: {data.get('session_ttl_seconds', 'N/A')}s")
    if data.get("session_max_age_seconds"):
        lines.append(f"  Session max age: {data['session_max_age_seconds']}s")
    if data.get("max_sessions"):
        lines.append(f"  Max sessions: {data['max_sessions']}")
    if data.get("max_models_per_session"):
        lines.append(f"  Max models/session: {data['max_models_per_session']}")
    if data.get("model_yaml"):
        lines.append(f"  Pre-loaded model: yes ({len(data['model_yaml'])} chars)")
    if data.get("query_execute", False):
        lines.append("  Query execution: available (use execute_query tool)")
    else:
        lines.append("  Query execution: not available")

    dialect_info = data.get("dialect")
    if dialect_info:
        lines.append("")
        lines.append("Dialect resolution:")
        if dialect_info.get("model"):
            lines.append(f"  model (defaultDialect): {dialect_info['model']}")
        if dialect_info.get("env"):
            lines.append(f"  env (DB_VENDOR): {dialect_info['env']}")
        lines.append(f"  effective: {dialect_info.get('effective', 'postgres')}")

    tz_info = data.get("timezone")
    if tz_info:
        lines.append("")
        lines.append("Timezone resolution:")
        if tz_info.get("model"):
            lines.append(f"  model (defaultTimezone): {tz_info['model']}")
        if tz_info.get("host"):
            lines.append(f"  host: {tz_info['host']}")
        if tz_info.get("database"):
            lines.append(f"  database: {tz_info['database']}")
        lines.append(f"  effective: {tz_info.get('effective', 'UTC')}")
        if tz_info.get("override_database_timezone"):
            lines.append("  overrideDatabaseTimezone: true")
        if tz_info.get("now"):
            lines.append(f"  now: {tz_info['now']}")
        if tz_info.get("utc"):
            lines.append(f"  utc: {tz_info['utc']}")

    ms_info = data.get("model_settings")
    if ms_info:
        lines.append("")
        lines.append("Model settings:")
        if ms_info.get("defaultDialect"):
            lines.append(f"  defaultDialect: {ms_info['defaultDialect']}")
        if ms_info.get("defaultNumericDataType"):
            lines.append(
                f"  defaultNumericDataType: {ms_info['defaultNumericDataType']}"
            )
        if ms_info.get("defaultTimezone"):
            lines.append(f"  defaultTimezone: {ms_info['defaultTimezone']}")
        if ms_info.get("overrideDatabaseTimezone"):
            lines.append("  overrideDatabaseTimezone: true")

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
# Implementation functions (shared logic for both modes)
# ---------------------------------------------------------------------------


def _format_metric_summary(met: dict) -> str:
    """Format a one-line summary for a metric (derived, cumulative, or PoP)."""
    met_type = met.get("type", "derived")
    if met_type == "cumulative":
        parts = [f"type: cumulative, measure: {met.get('measure', '?')}"]
        if met.get("time_dimension"):
            parts.append(f"timeDimension: {met['time_dimension']}")
        if met.get("cumulative_type") and met["cumulative_type"] != "sum":
            parts.append(f"cumulativeType: {met['cumulative_type']}")
        if met.get("window"):
            parts.append(f"window: {met['window']}")
        if met.get("grain_to_date"):
            parts.append(f"grainToDate: {met['grain_to_date']}")
        return ", ".join(parts)
    if met_type == "period_over_period":
        parts = [f"type: period_over_period, expr: {met.get('expression', '?')}"]
        pop = met.get("period_over_period") or {}
        if pop.get("time_dimension"):
            parts.append(f"timeDimension: {pop['time_dimension']}")
        if pop.get("grain"):
            parts.append(f"grain: {pop['grain']}")
        if pop.get("offset_grain"):
            parts.append(f"offsetGrain: {pop['offset_grain']}")
        if pop.get("comparison"):
            parts.append(f"comparison: {pop['comparison']}")
        return ", ".join(parts)
    return f"expr: {met.get('expression', '?')}"


def _impl_describe_model(model_id: str | None = None) -> str:
    """Describe the contents of a loaded model (shared implementation)."""
    if model_id is None:
        # Single-model shortcut — GET /v1/schema returns SchemaResponse
        resp = _shortcut_request("GET", "/schema")
        desc = _parse_json(resp)
        mid = desc.get("model_id", "default")
    else:
        resp = _session_request("GET", f"/models/{model_id}")
        desc = _parse_json(resp)
        mid = model_id

    lines: list[str] = [f"Model {mid}:", ""]

    # Composition
    extends = desc.get("extends", [])
    inherits = desc.get("inherits")
    if extends:
        lines.append(f"EXTENDS: {', '.join(extends)}")
    if inherits:
        lines.append(f"INHERITS: {inherits}")
    if extends or inherits:
        lines.append("")

    # Data objects
    lines.append("DATA OBJECTS:")
    for obj in desc.get("data_objects", []):
        # SchemaResponse uses 'name'; ModelDescription uses 'label'
        obj_name = obj.get("label", obj.get("name", "?"))
        lines.append(f"  {obj_name}  (code: {obj.get('code', '?')})")
        if obj.get("description"):
            lines.append(f"    description: {obj['description']}")
        # SchemaResponse: columns is list[ColumnDetail dict]; describe: list[str]
        cols = obj.get("columns", [])
        col_names = [c["name"] for c in cols] if cols and isinstance(cols[0], dict) else cols
        lines.append(f"    columns: {', '.join(col_names)}")
        if obj.get("join_targets"):
            lines.append(f"    joins to: {', '.join(obj['join_targets'])}")
        if obj.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(obj['synonyms'])}")
    lines.append("")

    # Dimensions
    lines.append("DIMENSIONS:")
    for dim in desc.get("dimensions", []):
        grain = f"  grain={dim['time_grain']}" if dim.get("time_grain") else ""
        via = f"  via {dim['via']}" if dim.get("via") else ""
        d_name = dim.get("name", "?")
        d_type = dim.get("result_type", "?")
        d_obj = dim.get("data_object", "?")
        d_col = dim.get("column", "?")
        lines.append(f"  {d_name}  ({d_type}, {d_obj}.{d_col}{grain}{via})")
        if dim.get("description"):
            lines.append(f"    description: {dim['description']}")
        if dim.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(dim['synonyms'])}")
    lines.append("")

    # Measures
    lines.append("MEASURES:")
    for m in desc.get("measures", []):
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        m_name = m.get("name", "?")
        m_type = m.get("result_type", "?")
        m_agg = m.get("aggregation", "?")
        dtype = f"  dataType: {m['data_type']}" if m.get("data_type") else ""
        lines.append(f"  {m_name}  ({m_type}, {m_agg}{expr}{dtype})")
        if m.get("description"):
            lines.append(f"    description: {m['description']}")
        if m.get("grain"):
            g = m["grain"]
            g_parts = [f"mode: {g.get('mode', 'RELATIVE')}"]
            if g.get("exclude"):
                g_parts.append(f"exclude: {g['exclude']}")
            if g.get("include"):
                g_parts.append(f"include: {g['include']}")
            if g.get("keepOnly") or g.get("keep_only"):
                g_parts.append(f"keepOnly: {g.get('keepOnly') or g.get('keep_only')}")
            lines.append(f"    grain: {', '.join(g_parts)}")
        if m.get("filter_context") or m.get("filterContext"):
            fc = m.get("filter_context") or m.get("filterContext")
            fc_parts = [f"mode: {fc.get('mode', 'RELATIVE')}"]
            if fc.get("exclude"):
                fc_parts.append(f"exclude: {fc['exclude']}")
            if fc.get("include"):
                fc_parts.append(f"include: {len(fc['include'])} filter(s)")
            if fc.get("keepOnly") or fc.get("keep_only"):
                fc_parts.append(f"keepOnly: {fc.get('keepOnly') or fc.get('keep_only')}")
            lines.append(f"    filterContext: {', '.join(fc_parts)}")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    lines.append("")

    # Metrics
    metrics = desc.get("metrics", [])
    if metrics:
        lines.append("METRICS:")
        for met in metrics:
            lines.append(f"  {met.get('name', '?')}  {_format_metric_summary(met)}")
            if met.get("data_type"):
                lines.append(f"    dataType: {met['data_type']}")
            if met.get("description"):
                lines.append(f"    description: {met['description']}")
            if met.get("synonyms"):
                lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
        lines.append("")

    # Model settings
    model_settings = desc.get("settings")
    if model_settings:
        lines.append("SETTINGS:")
        if model_settings.get("default_dialect"):
            lines.append(f"  defaultDialect: {model_settings['default_dialect']}")
        if model_settings.get("default_numeric_data_type"):
            lines.append(f"  defaultNumericDataType: {model_settings['default_numeric_data_type']}")
        if model_settings.get("default_timezone"):
            lines.append(f"  defaultTimezone: {model_settings['default_timezone']}")
        if model_settings.get("override_database_timezone"):
            lines.append("  overrideDatabaseTimezone: true")
        lines.append("")

    # Static filters
    filters = desc.get("filters", [])
    if filters:
        lines.append("STATIC FILTERS (applied to every query):")
        for f in filters:
            val = f.get("value")
            vals = f.get("values")
            if vals:
                val_str = f"values: {vals}"
            elif val is not None:
                val_str = f"value: {val}"
            else:
                val_str = ""
            lines.append(
                f"  {f.get('data_object', '?')}.{f.get('column', '?')} "
                f"{f.get('operator', '?')} {val_str}"
            )
        lines.append("")

    return "\n".join(lines)


def _parse_json_param(value: str | None, name: str) -> list | dict | None:
    """Parse an optional JSON string parameter."""
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ToolError(f"Invalid {name} JSON: {exc}") from exc


def _build_query_object(
    dimensions: list[str] | None,
    measures: list[str] | None,
    query_json: str | None,
    use_path_names: list[dict[str, str]] | None,
    where: str | None = None,
    having: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    dimensions_exclude: bool | None = None,
    coalesce_dimensions: str | None = None,
    fields: list[str] | None = None,
    distinct: bool | None = None,
) -> dict:
    """Build a query dict from tool arguments (shared by compile/execute)."""
    if query_json is not None:
        try:
            return json.loads(query_json)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid query JSON: {exc}") from exc
    elif fields is not None:
        # Raw mode — physical column projection, no aggregation
        select: dict = {"fields": fields}
        if distinct is not None:
            select["distinct"] = distinct
        query: dict = {"select": select}
        parsed_where = _parse_json_param(where, "where")
        if parsed_where is not None:
            query["where"] = parsed_where
        parsed_order = _parse_json_param(order_by, "order_by")
        if parsed_order is not None:
            query["order_by"] = parsed_order
        if limit is not None:
            query["limit"] = limit
        if offset is not None:
            query["offset"] = offset
        return query
    elif dimensions is not None or measures is not None:
        dim_list: list[str | dict] = list(dimensions or [])
        parsed_coalesce = _parse_json_param(coalesce_dimensions, "coalesce_dimensions")
        if parsed_coalesce is not None:
            if not isinstance(parsed_coalesce, list):
                raise ToolError("coalesce_dimensions must be a JSON array")
            dim_list.extend(parsed_coalesce)
        query = {
            "select": {
                "dimensions": dim_list,
                "measures": measures or [],
            },
        }
        if use_path_names:
            query["usePathNames"] = use_path_names
        parsed_where = _parse_json_param(where, "where")
        if parsed_where is not None:
            query["where"] = parsed_where
        parsed_having = _parse_json_param(having, "having")
        if parsed_having is not None:
            query["having"] = parsed_having
        parsed_order = _parse_json_param(order_by, "order_by")
        if parsed_order is not None:
            query["order_by"] = parsed_order
        if limit is not None:
            query["limit"] = limit
        if offset is not None:
            query["offset"] = offset
        if dimensions_exclude is not None:
            query["dimensionsExclude"] = dimensions_exclude
        return query
    else:
        raise ToolError(
            "Provide either dimensions/measures, fields, or query_json."
        )


def _format_compile_result(data: dict) -> str:
    """Format compile_query API response into human-readable output."""
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
        parts.append(f"-- Base object: {exp['base_object']} ({exp.get('base_object_reason', '')})")
        for j in exp.get("joins", []):
            parts.append(
                f"--   Join: {j['from_object']} -> {j['to_object']} ({j.get('reason', '')})"
            )
        cfl_legs = exp.get("cfl_legs", [])
        if cfl_legs:
            for leg in cfl_legs:
                measures = ", ".join(leg.get("measures", []))
                parts.append(
                    f"--   CFL leg: {leg['measure_source']} "
                    f"(root: {leg['common_root']}, measures: {measures})"
                )
                if leg.get("reason"):
                    parts.append(f"--     Reason: {leg['reason']}")
                for jn in leg.get("joins", []):
                    parts.append(f"--     Join: {jn}")
        if exp.get("has_totals"):
            parts.append("-- Totals: yes")
        if exp.get("has_grain_overrides"):
            parts.append("-- Grain overrides: yes")
        if exp.get("has_filter_context"):
            parts.append("-- Filter context: yes")
    if data.get("warnings"):
        parts.append("")
        parts.append(f"-- Warnings: {'; '.join(data['warnings'])}")
    return "\n".join(parts)


def _impl_compile_query(
    model_id: str | None,
    dialect: str | None,
    dimensions: list[str] | None,
    measures: list[str] | None,
    query_json: str | None,
    use_path_names: list[dict[str, str]] | None,
    where: str | None = None,
    having: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    dimensions_exclude: bool | None = None,
    coalesce_dimensions: str | None = None,
    fields: list[str] | None = None,
    distinct: bool | None = None,
) -> str:
    """Compile a semantic query (shared implementation)."""
    logger.info("compile_query called (model_id=%s, dialect=%s)", model_id, dialect)
    query = _build_query_object(
        dimensions,
        measures,
        query_json,
        use_path_names,
        where=where,
        having=having,
        order_by=order_by,
        limit=limit,
        offset=offset,
        dimensions_exclude=dimensions_exclude,
        coalesce_dimensions=coalesce_dimensions,
        fields=fields,
        distinct=distinct,
    )

    if model_id is None:
        params: dict[str, str] = {}
        if dialect is not None:
            params["dialect"] = dialect
        resp = _shortcut_request("POST", "/query/sql", json_body=query, params=params or None)
    else:
        body: dict = {"model_id": model_id, "query": query}
        if dialect is not None:
            body["dialect"] = dialect
        resp = _session_request("POST", "/query/sql", json_body=body)

    return _format_compile_result(_parse_json(resp))


def _impl_execute_query(
    model_id: str | None,
    dialect: str | None,
    dimensions: list[str] | None,
    measures: list[str] | None,
    query_json: str | None,
    use_path_names: list[dict[str, str]] | None,
    where: str | None = None,
    having: str | None = None,
    order_by: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    dimensions_exclude: bool | None = None,
    coalesce_dimensions: str | None = None,
    fields: list[str] | None = None,
    distinct: bool | None = None,
    output_format: str = "json",
    format_values: bool | None = None,
    locale: str | None = None,
    timezone: str | None = None,
) -> str:
    """Compile and execute a semantic query (shared implementation)."""
    logger.info("execute_query called (model_id=%s, dialect=%s)", model_id, dialect)
    query = _build_query_object(
        dimensions,
        measures,
        query_json,
        use_path_names,
        where=where,
        having=having,
        order_by=order_by,
        limit=limit,
        offset=offset,
        dimensions_exclude=dimensions_exclude,
        coalesce_dimensions=coalesce_dimensions,
        fields=fields,
        distinct=distinct,
    )

    extra_params: dict[str, str] = {"format": output_format}
    if format_values is not None:
        extra_params["format_values"] = str(format_values).lower()
    if locale is not None:
        extra_params["locale"] = locale
    if timezone is not None:
        extra_params["timezone"] = timezone

    if model_id is None:
        params: dict[str, str] = {**extra_params}
        if dialect is not None:
            params["dialect"] = dialect
        resp = _shortcut_request(
            "POST",
            "/query/execute",
            json_body=query,
            params=params,
        )
    else:
        body: dict = {"model_id": model_id, "query": query}
        if dialect is not None:
            body["dialect"] = dialect
        resp = _session_request(
            "POST",
            "/query/execute",
            json_body=body,
            params=extra_params,
        )

    if output_format == "tsv":
        return resp.text
    return json.dumps(_parse_json(resp), indent=2)


def _impl_get_model_diagram(model_id: str | None, show_columns: bool, theme: str) -> str:
    """Generate a Mermaid ER diagram (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request(
            "GET",
            "/diagram/er",
            params={"show_columns": str(show_columns).lower(), "theme": theme},
        )
    else:
        params = f"?show_columns={str(show_columns).lower()}&theme={quote(theme, safe='')}"
        resp = _session_request("GET", f"/models/{model_id}/diagram/er{params}")
    return _parse_json(resp)["mermaid"]


def _impl_get_model_schema(model_id: str | None) -> str:
    """Get the full model structure as JSON (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/schema")
    else:
        resp = _session_request("GET", f"/models/{model_id}/schema")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_list_dimensions(model_id: str | None) -> str:
    """List all dimensions (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/dimensions")
    else:
        resp = _session_request("GET", f"/models/{model_id}/dimensions")
    dims = _parse_json(resp)
    if not dims:
        return "No dimensions in this model."
    lines = ["Dimensions:", ""]
    for d in dims:
        grain = f"  grain={d['time_grain']}" if d.get("time_grain") else ""
        via = f"  via {d['via']}" if d.get("via") else ""
        d_name = d.get("name", "?")
        d_type = d.get("result_type", "?")
        d_obj = d.get("data_object", "?")
        d_col = d.get("column", "?")
        lines.append(f"  {d_name}  ({d_type}, {d_obj}.{d_col}{grain}{via})")
        if d.get("description"):
            lines.append(f"    description: {d['description']}")
        if d.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(d['synonyms'])}")
    return "\n".join(lines)


def _impl_get_dimension(model_id: str | None, name: str) -> str:
    """Get a single dimension by name (shared implementation)."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/dimensions/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/dimensions/{encoded}")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_list_measures(model_id: str | None) -> str:
    """List all measures (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/measures")
    else:
        resp = _session_request("GET", f"/models/{model_id}/measures")
    measures = _parse_json(resp)
    if not measures:
        return "No measures in this model."
    lines = ["Measures:", ""]
    for m in measures:
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        m_name = m.get("name", "?")
        m_type = m.get("result_type", "?")
        m_agg = m.get("aggregation", "?")
        dtype = f"  dataType: {m['data_type']}" if m.get("data_type") else ""
        total = "  total" if m.get("total") else ""
        grain_tag = "  grain" if m.get("grain") else ""
        fc_tag = "  filterContext" if m.get("filter_context") or m.get("filterContext") else ""
        lines.append(f"  {m_name}  ({m_type}, {m_agg}{expr}{dtype}{total}{grain_tag}{fc_tag})")
        if m.get("description"):
            lines.append(f"    description: {m['description']}")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    return "\n".join(lines)


def _impl_get_measure(model_id: str | None, name: str) -> str:
    """Get a single measure by name (shared implementation)."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/measures/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/measures/{encoded}")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_list_metrics(model_id: str | None) -> str:
    """List all metrics (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/metrics")
    else:
        resp = _session_request("GET", f"/models/{model_id}/metrics")
    metrics = _parse_json(resp)
    if not metrics:
        return "No metrics in this model."
    lines = ["Metrics:", ""]
    for met in metrics:
        components = ", ".join(met.get("component_measures", []))
        lines.append(f"  {met['name']}  {_format_metric_summary(met)}")
        if met.get("data_type"):
            lines.append(f"    dataType: {met['data_type']}")
        if met.get("description"):
            lines.append(f"    description: {met['description']}")
        if components:
            lines.append(f"    components: {components}")
        if met.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
    return "\n".join(lines)


def _impl_get_metric(model_id: str | None, name: str) -> str:
    """Get a single metric by name (shared implementation)."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/metrics/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/metrics/{encoded}")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_explain_artefact(model_id: str | None, name: str) -> str:
    """Explain the lineage of a dimension, measure, or metric (shared impl)."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/explain/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/explain/{encoded}")
    data = _parse_json(resp)
    lines = [f"Explain: {data['name']}  (type: {data['type']})", ""]
    for item in data.get("lineage", []):
        detail = f"  — {item['detail']}" if item.get("detail") else ""
        lines.append(f"  [{item['type']}] {item['name']}{detail}")
    return "\n".join(lines)


def _impl_find_artefacts(model_id: str | None, query: str, types: list[str] | None) -> str:
    """Search across model artefacts (shared implementation)."""
    body: dict = {"query": query}
    if types is not None:
        body["types"] = types
    if model_id is None:
        resp = _shortcut_request("POST", "/find", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/find", json_body=body)
    data = _parse_json(resp)
    results = data.get("results", [])
    if not results:
        return f"No artefacts found matching '{query}'."
    lines = [f"Search results for '{query}':", ""]
    for r in results:
        lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    return "\n".join(lines)


def _impl_get_graph(model_id: str | None) -> str:
    """Return the OBSL-Core RDF graph as Turtle (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/graph")
    else:
        resp = _session_request("GET", f"/models/{model_id}/graph")
    return resp.text


def _impl_sparql_query(model_id: str | None, query: str) -> str:
    """Execute a read-only SPARQL query (shared implementation)."""
    body: dict = {"query": query}
    if model_id is None:
        resp = _shortcut_request("POST", "/sparql", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/sparql", json_body=body)
    data = _parse_json(resp)

    query_type = data.get("type", "select")
    if query_type == "ask":
        return f"ASK result: {data.get('boolean', False)}"

    variables = data.get("variables", [])
    results = data.get("results", [])
    if not results:
        return "SPARQL query returned no results."

    # Format as a readable table
    lines = [" | ".join(variables)]
    lines.append(" | ".join("---" for _ in variables))
    for row in results:
        lines.append(" | ".join(str(row.get(v, "")) for v in variables))
    return "\n".join(lines)


def _impl_get_join_graph(model_id: str | None) -> str:
    """Return the join graph as an adjacency list (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/join-graph")
    else:
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


# ---------------------------------------------------------------------------
# Tool registration (mode-dependent)
# ---------------------------------------------------------------------------


def _register_single_model_tools() -> None:
    """Register tools for single-model mode (no model_id, shortcut endpoints)."""

    @mcp.tool
    def get_model() -> str:
        """Get the pre-loaded OBML YAML model source.

        Returns the original OBML YAML that was loaded into the API at startup.
        Useful for understanding the model definition in the author's terms.
        """
        resp = _api_request("GET", f"{_API_V1}/settings", retry_on_expired=False)
        data = _parse_json(resp)
        yaml_content = data.get("model_yaml")
        if not yaml_content:
            raise ToolError("No model YAML available from the API")
        return yaml_content

    @mcp.tool
    def describe_model() -> str:
        """Describe the pre-loaded model.

        Shows data objects (with columns and joins), dimensions, measures,
        and metrics.
        """
        return _impl_describe_model()

    @mcp.tool
    def compile_query(
        dialect: str | None = None,
        dimensions: list[str] | None = None,
        measures: list[str] | None = None,
        fields: list[str] | None = None,
        distinct: bool | None = None,
        where: str | None = None,
        having: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        dimensions_exclude: bool | None = None,
        coalesce_dimensions: str | None = None,
        query_json: str | None = None,
        use_path_names: list[dict[str, str]] | None = None,
    ) -> str:
        """Compile a semantic query to SQL.

        **Aggregate mode** — pass ``dimensions`` and/or ``measures``::

            compile_query(
                dimensions=["Country"],
                measures=["Revenue"],
                where='[{"field": "Country", "op": "equals", "value": "US"}]',
                limit=10,
            )

        **Raw mode** — pass ``fields`` for un-aggregated column access::

            compile_query(
                fields=["Orders.OrderDate", "Orders.Amount"],
                distinct=True,
                limit=100,
            )

        Raw mode is mutually exclusive with dimensions, measures,
        having, and dimensionsExclude.

        Alternatively, pass a complete query as JSON via ``query_json``
        (overrides all other query parameters).

        Use ``describe_model`` first to discover available names.

        Args:
            dialect: Target SQL dialect.  When omitted the API resolves
                via model.settings.defaultDialect → server default.
            dimensions: List of dimension names (aggregate mode).
            measures: List of measure names (aggregate mode).
            fields: List of physical column refs as
                "DataObject.Column" (raw mode).  Mutually exclusive
                with dimensions/measures.
            distinct: Emit SELECT DISTINCT (raw mode only).
            where: Filters as a JSON string, e.g.
                '[{"field": "Country", "op": "equals", "value": "US"}]'.
            having: Measure/metric filters as a JSON string, e.g.
                '[{"field": "Revenue", "op": "gt", "value": 1000}]'.
            order_by: Ordering as a JSON string, e.g.
                '[{"field": "Revenue", "direction": "desc"}]'.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.
            dimensions_exclude: If true, return dimension combinations that
                do NOT exist (anti-join).
            coalesce_dimensions: Coalesce groups as a JSON string, e.g.
                '[{"coalesce": ["SalesEmp", "PurchaseEmp"],
                "as": "Employee"}]'.  Merges role-playing dimensions
                into one output column via COALESCE.  All members must
                share the same resultType.
            query_json: Complete query as JSON string (overrides above).
            use_path_names: List of {source, target, pathName} dicts for
                selecting secondary joins.
        """
        return _impl_compile_query(
            None,
            dialect,
            dimensions,
            measures,
            query_json,
            use_path_names,
            where=where,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            dimensions_exclude=dimensions_exclude,
            coalesce_dimensions=coalesce_dimensions,
            fields=fields,
            distinct=distinct,
        )

    @mcp.tool
    def get_model_diagram(
        show_columns: bool = True,
        theme: str = "default",
    ) -> str:
        """Generate a Mermaid ER diagram for the pre-loaded model.

        Returns a Mermaid diagram script that visualises the data objects,
        columns, and join relationships in the model.

        Args:
            show_columns: Whether to include column details in the diagram.
            theme: Mermaid diagram theme (e.g. "default", "dark", "forest").
        """
        return _impl_get_model_diagram(None, show_columns, theme)

    @mcp.tool
    def get_model_schema() -> str:
        """Get the full model structure as JSON.

        Returns a detailed JSON representation of the model including all data
        objects (with columns, types, comments, owners), dimensions, measures,
        metrics, and their synonyms.  More detailed than ``describe_model``.
        """
        return _impl_get_model_schema(None)

    @mcp.tool
    def list_dimensions() -> str:
        """List all dimensions in the model.

        Returns dimension details including data object, column, result type,
        time grain, and synonyms.
        """
        return _impl_list_dimensions(None)

    @mcp.tool
    def get_dimension(name: str) -> str:
        """Get a single dimension by name.

        Args:
            name: The dimension name.
        """
        return _impl_get_dimension(None, name)

    @mcp.tool
    def list_measures() -> str:
        """List all measures in the model.

        Returns measure details including aggregation type, expression, result
        type, and synonyms.
        """
        return _impl_list_measures(None)

    @mcp.tool
    def get_measure(name: str) -> str:
        """Get a single measure by name.

        Args:
            name: The measure name.
        """
        return _impl_get_measure(None, name)

    @mcp.tool
    def list_metrics() -> str:
        """List all metrics in the model.

        Returns metric details including expression, component measures, and
        synonyms.
        """
        return _impl_list_metrics(None)

    @mcp.tool
    def get_metric(name: str) -> str:
        """Get a single metric by name.

        Args:
            name: The metric name.
        """
        return _impl_get_metric(None, name)

    @mcp.tool
    def explain_artefact(name: str) -> str:
        """Explain the lineage of a dimension, measure, or metric.

        Traces the composition chain from the named artefact down to the
        underlying data objects and columns.  Useful for understanding how a
        measure is computed or where a dimension originates.

        Args:
            name: The dimension, measure, or metric name to explain.
        """
        return _impl_explain_artefact(None, name)

    @mcp.tool
    def find_artefacts(
        query: str,
        types: list[str] | None = None,
    ) -> str:
        """Search across model artefacts by name or synonym.

        Finds dimensions, measures, metrics, and data objects whose name or
        synonym matches the search query (case-insensitive substring match).

        Args:
            query: Search term (matched against names and synonyms).
            types: Object types to search.  Defaults to all types:
                dimension, measure, metric, data_object.
        """
        return _impl_find_artefacts(None, query, types)

    @mcp.tool
    def get_join_graph() -> str:
        """Return the join graph as an adjacency list.

        Shows the data object nodes and join edges (with cardinality and join
        columns) in the model.  Useful for understanding table relationships.
        """
        return _impl_get_join_graph(None)

    @mcp.tool
    def get_graph() -> str:
        """Get the OBSL-Core RDF graph for the model as Turtle.

        Returns the semantic model's RDF graph serialized in Turtle format.
        The graph follows the OBSL-Core ontology and can be used for
        semantic web integration or further analysis.
        """
        return _impl_get_graph(None)

    @mcp.tool
    def sparql_query(query: str) -> str:
        """Execute a read-only SPARQL query against the model's RDF graph.

        Supports SELECT and ASK queries only (no INSERT/DELETE/UPDATE).
        The graph uses the OBSL-Core ontology.

        Args:
            query: SPARQL query string (SELECT or ASK).
        """
        return _impl_sparql_query(None, query)


def _setup_mode_tools() -> None:
    """Detect API mode and register the appropriate tool set. Idempotent."""
    global _single_model_mode, _query_execute_enabled, _tools_registered
    if _tools_registered:
        return
    _single_model_mode, _query_execute_enabled = _detect_api_mode()
    if _single_model_mode:
        logger.info("Single-model mode detected — using shortcut endpoints")
        _register_single_model_tools()
    else:
        logger.info("Multi-model mode — using session-scoped endpoints")
        _register_multi_model_tools()
    if _query_execute_enabled:
        logger.info("Query execution enabled — registering execute_query tool")
        _register_execute_query_tool()
    _tools_registered = True


def _register_multi_model_tools() -> None:
    """Register tools for multi-model mode (requires model_id, session-scoped)."""

    @mcp.tool
    def load_model(
        model: dict | str | None = None,
        extends: list[dict] | str | None = None,
        inherits: str | None = None,
    ) -> str:
        """Load a semantic model definition. Returns a model_id.

        ``model`` is mandatory — pass the OBML model as a JSON object::

            load_model(model={
                "version": 1.0,
                "dataObjects": {
                    "Sales": {
                        "code": "sales", "schema": "public",
                        "columns": {"Amount": {"abstractType": "float"},
                                    "CustomerKey": {"abstractType": "int"}},
                        "joins": [{"joinTo": "Customers", "joinType": "inner",
                                   "columnsFrom": ["CustomerKey"],
                                   "columnsTo": ["CustomerKey"]}]
                    },
                    "Customers": {
                        "code": "customers", "schema": "public",
                        "columns": {"CustomerKey": {"abstractType": "int"},
                                    "Country": {"abstractType": "string"}}
                    }
                },
                "dimensions": {
                    "Country": {
                        "dataObject": "Customers", "column": "Country",
                        "resultType": "string"
                    }
                },
                "measures": {
                    "Revenue": {
                        "aggregation": "SUM", "resultType": "float",
                        "columns": [{"dataObject": "Sales", "column": "Amount"}]
                    }
                }
            })

        IMPORTANT: Joins are defined INSIDE each dataObject (not at the top
        level).  Each join uses ``joinTo`` (target data object name),
        ``joinType`` (inner/left/right/full), ``columnsFrom`` (columns in
        this data object), and ``columnsTo`` (columns in the target).
        Column names in joins reference OBML column names (the keys in
        ``columns``), not physical database column names.

        Keys use camelCase: ``dataObjects``, ``joinType``, ``columnsFrom``,
        ``columnsTo``, ``resultType``, ``abstractType``, ``timeGrain``.

        Column ``abstractType`` values: string, int, float, date, boolean.
        Aggregation values: SUM, COUNT, AVG, MIN, MAX, count_distinct, any_value.
        Measure expressions: ``{[DataObject].[Column]}`` syntax.
        Metric expressions: ``{[MeasureName]}`` syntax.

        Call ``get_obml_reference()`` for the full specification.

        Args:
            model: (mandatory) OBML model as a JSON object (top-level keys:
                version, dataObjects, dimensions, measures, metrics).
                Joins are defined inside each dataObject, not at the top level.
            extends: Optional list of analytical fragment objects (dimensions,
                measures, metrics) to merge into the model before loading.
            inherits: Optional model_id of an already-loaded parent model in
                the session.  The child model inherits the parent's data
                objects and joins, adding or overriding analytical artefacts.
        """
        if not model:
            raise ToolError(
                "model is mandatory — provide the OBML model as a JSON object. "
                "Call get_obml_reference() first to learn the structure."
            )
        if isinstance(model, str):
            try:
                model = json.loads(model)
            except json.JSONDecodeError as exc:
                raise ToolError(f"Invalid model JSON string: {exc}") from exc
        logger.info("load_model called")
        body: dict = {"model_json": model}
        if extends:
            if isinstance(extends, str):
                try:
                    extends = json.loads(extends)
                except json.JSONDecodeError as exc:
                    raise ToolError(f"Invalid extends JSON string: {exc}") from exc
            body["extends_json"] = extends
        if inherits:
            body["inherits"] = inherits
        resp = _session_request("POST", "/models", json_body=body)
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
    def describe_model(model_id: str) -> str:
        """Describe the contents of a loaded model.

        Shows data objects (with columns and joins), dimensions, measures, and
        metrics.  Use this after ``load_model`` to explore the model.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_describe_model(model_id)

    @mcp.tool
    def compile_query(
        model_id: str,
        dialect: str | None = None,
        dimensions: list[str] | None = None,
        measures: list[str] | None = None,
        fields: list[str] | None = None,
        distinct: bool | None = None,
        where: str | None = None,
        having: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
        dimensions_exclude: bool | None = None,
        coalesce_dimensions: str | None = None,
        query_json: str | None = None,
        use_path_names: list[dict[str, str]] | None = None,
    ) -> str:
        """Compile a semantic query to SQL.

        **Aggregate mode** — pass ``dimensions`` and/or ``measures``::

            compile_query(
                model_id="abc12345",
                dimensions=["Country"],
                measures=["Revenue"],
                where='[{"field": "Country", "op": "equals", "value": "US"}]',
                limit=10,
            )

        **Raw mode** — pass ``fields`` for un-aggregated column access::

            compile_query(
                model_id="abc12345",
                fields=["Orders.OrderDate", "Orders.Amount"],
                distinct=True,
                limit=100,
            )

        Raw mode is mutually exclusive with dimensions, measures,
        having, and dimensionsExclude.

        Alternatively, pass a complete query as JSON via ``query_json``
        (overrides all other query parameters).

        Use ``describe_model`` first to discover available names.

        Args:
            model_id: The id returned by ``load_model``.
            dialect: Target SQL dialect.  When omitted the API resolves
                via model.settings.defaultDialect → server default.
            dimensions: List of dimension names (aggregate mode).
            measures: List of measure names (aggregate mode).
            fields: List of physical column refs as
                "DataObject.Column" (raw mode).  Mutually exclusive
                with dimensions/measures.
            distinct: Emit SELECT DISTINCT (raw mode only).
            where: Filters as a JSON string, e.g.
                '[{"field": "Country", "op": "equals", "value": "US"}]'.
            having: Measure/metric filters as a JSON string, e.g.
                '[{"field": "Revenue", "op": "gt", "value": 1000}]'.
            order_by: Ordering as a JSON string, e.g.
                '[{"field": "Revenue", "direction": "desc"}]'.
            limit: Maximum number of rows to return.
            offset: Number of rows to skip.
            dimensions_exclude: If true, return dimension combinations that
                do NOT exist (anti-join).
            coalesce_dimensions: Coalesce groups as a JSON string, e.g.
                '[{"coalesce": ["SalesEmp", "PurchaseEmp"],
                "as": "Employee"}]'.  Merges role-playing dimensions
                into one output column via COALESCE.  All members must
                share the same resultType.
            query_json: Complete query as JSON string (overrides above).
            use_path_names: List of {source, target, pathName} dicts for
                selecting secondary joins.
        """
        return _impl_compile_query(
            model_id,
            dialect,
            dimensions,
            measures,
            query_json,
            use_path_names,
            where=where,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            dimensions_exclude=dimensions_exclude,
            coalesce_dimensions=coalesce_dimensions,
            fields=fields,
            distinct=distinct,
        )

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
        return _impl_get_model_diagram(model_id, show_columns, theme)

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
        return _impl_get_model_schema(model_id)

    @mcp.tool
    def list_dimensions(model_id: str) -> str:
        """List all dimensions in a model.

        Returns dimension details including data object, column, result type,
        time grain, and synonyms.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_list_dimensions(model_id)

    @mcp.tool
    def get_dimension(model_id: str, name: str) -> str:
        """Get a single dimension by name.

        Args:
            model_id: The id returned by ``load_model``.
            name: The dimension name.
        """
        return _impl_get_dimension(model_id, name)

    @mcp.tool
    def list_measures(model_id: str) -> str:
        """List all measures in a model.

        Returns measure details including aggregation type, expression, result
        type, and synonyms.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_list_measures(model_id)

    @mcp.tool
    def get_measure(model_id: str, name: str) -> str:
        """Get a single measure by name.

        Args:
            model_id: The id returned by ``load_model``.
            name: The measure name.
        """
        return _impl_get_measure(model_id, name)

    @mcp.tool
    def list_metrics(model_id: str) -> str:
        """List all metrics in a model.

        Returns metric details including expression, component measures, and
        synonyms.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_list_metrics(model_id)

    @mcp.tool
    def get_metric(model_id: str, name: str) -> str:
        """Get a single metric by name.

        Args:
            model_id: The id returned by ``load_model``.
            name: The metric name.
        """
        return _impl_get_metric(model_id, name)

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
        return _impl_explain_artefact(model_id, name)

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
        return _impl_find_artefacts(model_id, query, types)

    @mcp.tool
    def get_join_graph(model_id: str) -> str:
        """Return the join graph as an adjacency list.

        Shows the data object nodes and join edges (with cardinality and join
        columns) in the model.  Useful for understanding table relationships.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_get_join_graph(model_id)

    @mcp.tool
    def get_graph(model_id: str) -> str:
        """Get the OBSL-Core RDF graph for a loaded model as Turtle.

        Returns the semantic model's RDF graph serialized in Turtle format.
        The graph follows the OBSL-Core ontology and can be used for
        semantic web integration or further analysis.

        Args:
            model_id: The id returned by ``load_model``.
        """
        return _impl_get_graph(model_id)

    @mcp.tool
    def sparql_query(model_id: str, query: str) -> str:
        """Execute a read-only SPARQL query against a model's RDF graph.

        Supports SELECT and ASK queries only (no INSERT/DELETE/UPDATE).
        The graph uses the OBSL-Core ontology.

        Args:
            model_id: The id returned by ``load_model``.
            query: SPARQL query string (SELECT or ASK).
        """
        return _impl_sparql_query(model_id, query)


def _register_execute_query_tool() -> None:
    """Register execute_query tool (only when query execution is available)."""

    if _single_model_mode:

        @mcp.tool
        def execute_query(
            dialect: str | None = None,
            dimensions: list[str] | None = None,
            measures: list[str] | None = None,
            fields: list[str] | None = None,
            distinct: bool | None = None,
            where: str | None = None,
            having: str | None = None,
            order_by: str | None = None,
            limit: int | None = None,
            offset: int | None = None,
            dimensions_exclude: bool | None = None,
            coalesce_dimensions: str | None = None,
            query_json: str | None = None,
            use_path_names: list[dict[str, str]] | None = None,
            output_format: str = "json",
            format_values: bool | None = None,
            locale: str | None = None,
            timezone: str | None = None,
        ) -> str:
            """Compile and execute a semantic query, returning SQL and results.

            Same query parameters as ``compile_query`` (aggregate and raw
            modes).  If no ``limit`` is specified, a server-side default
            row limit is enforced.

            Args:
                dialect: Target SQL dialect.  When omitted the API
                    resolves via model.settings.defaultDialect →
                    server default.
                dimensions: List of dimension names (aggregate mode).
                measures: List of measure names (aggregate mode).
                fields: List of physical column refs as
                    "DataObject.Column" (raw mode).
                distinct: Emit SELECT DISTINCT (raw mode only).
                where: Filters as a JSON string.
                having: Measure/metric filters as a JSON string.
                order_by: Ordering as a JSON string.
                limit: Maximum number of rows to return.
                offset: Number of rows to skip.
                dimensions_exclude: Anti-join mode (aggregate only).
                coalesce_dimensions: Coalesce groups as a JSON string.
                query_json: Complete query as JSON (overrides above).
                use_path_names: Secondary join path selectors.
                output_format: Response format — "json" (default) or "tsv".
                format_values: Format numeric cells as display strings.
                locale: BCP-47 locale for number formatting (e.g. "de").
                timezone: IANA timezone (e.g. "Europe/Berlin").
            """
            return _impl_execute_query(
                None,
                dialect,
                dimensions,
                measures,
                query_json,
                use_path_names,
                where=where,
                having=having,
                order_by=order_by,
                limit=limit,
                offset=offset,
                dimensions_exclude=dimensions_exclude,
                coalesce_dimensions=coalesce_dimensions,
                fields=fields,
                distinct=distinct,
                output_format=output_format,
                format_values=format_values,
                locale=locale,
                timezone=timezone,
            )

    else:

        @mcp.tool
        def execute_query(
            model_id: str,
            dialect: str | None = None,
            dimensions: list[str] | None = None,
            measures: list[str] | None = None,
            fields: list[str] | None = None,
            distinct: bool | None = None,
            where: str | None = None,
            having: str | None = None,
            order_by: str | None = None,
            limit: int | None = None,
            offset: int | None = None,
            dimensions_exclude: bool | None = None,
            coalesce_dimensions: str | None = None,
            query_json: str | None = None,
            use_path_names: list[dict[str, str]] | None = None,
            output_format: str = "json",
            format_values: bool | None = None,
            locale: str | None = None,
            timezone: str | None = None,
        ) -> str:
            """Compile and execute a semantic query, returning SQL and results.

            Same query parameters as ``compile_query`` (aggregate and raw
            modes).  If no ``limit`` is specified, a server-side default
            row limit is enforced.

            Args:
                model_id: The id returned by ``load_model``.
                dialect: Target SQL dialect.  When omitted the API
                    resolves via model.settings.defaultDialect →
                    server default.
                dimensions: List of dimension names (aggregate mode).
                measures: List of measure names (aggregate mode).
                fields: List of physical column refs as
                    "DataObject.Column" (raw mode).
                distinct: Emit SELECT DISTINCT (raw mode only).
                where: Filters as a JSON string.
                having: Measure/metric filters as a JSON string.
                order_by: Ordering as a JSON string.
                limit: Maximum number of rows to return.
                offset: Number of rows to skip.
                dimensions_exclude: Anti-join mode (aggregate only).
                coalesce_dimensions: Coalesce groups as a JSON string.
                query_json: Complete query as JSON (overrides above).
                use_path_names: Secondary join path selectors.
                output_format: Response format — "json" (default) or "tsv".
                format_values: Format numeric cells as display strings.
                locale: BCP-47 locale for number formatting (e.g. "de").
                timezone: IANA timezone (e.g. "Europe/Berlin").
            """
            return _impl_execute_query(
                model_id,
                dialect,
                dimensions,
                measures,
                query_json,
                use_path_names,
                where=where,
                having=having,
                order_by=order_by,
                limit=limit,
                offset=offset,
                dimensions_exclude=dimensions_exclude,
                coalesce_dimensions=coalesce_dimensions,
                fields=fields,
                distinct=distinct,
                output_format=output_format,
                format_values=format_values,
                locale=locale,
                timezone=timezone,
            )


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

    async def render(self, _arguments=None):  # type: ignore[override]
        return self.text


_WRITE_QUERY_TEXT = """\
# Compiling Queries with OrionBelt

## Simple Mode

Pass dimension and measure names directly:

```
compile_query(
  model_id="abc12345",
  dimensions=["Customer Country"],
  measures=["Total Revenue"]
)
```

## Full Mode (filters, ordering, limits)

Pass a complete query as JSON:

```
compile_query(
  model_id="abc12345",
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
- Regex: `regex`, `notregex` (per-dialect native syntax)
- Blank: `blank` (NULL or empty/whitespace), `notblank`
- Length: `length_eq`, `length_gt`, `length_lt` (value must be integer)
- Range: `between`, `notbetween`, `relative`

## Filter Groups (AND/OR/NOT)

`where` and `having` arrays accept both leaf filters and filter groups
for complex boolean expressions:

```json
"where": [
  {"logic": "or", "filters": [
    {"field": "Customer Country", "op": "equals", "value": "US"},
    {"field": "Customer Country", "op": "equals", "value": "DE"}
  ]},
  {"field": "Order Status", "op": "notequals", "value": "Cancelled"}
]
```

Top-level items are combined with AND.  Groups support:
- `logic`: `"and"` (default) or `"or"`
- `filters`: array of leaf filters or nested groups (recursive)
- `negated`: `true` to negate the entire group (NOT)

## Qualified Column References (WHERE only)

WHERE filter fields accept three syntaxes:
- Dimension name: `"Customer Country"`
- Qualified column: `"Orders.Order Priority"` (DataObject.Column)
- The data object must be reachable from the query's join graph (auto-joined)

HAVING filter fields reference a measure or metric name.

## dimensionsExclude

Set `"dimensionsExclude": true` to return dimension value combinations that
do NOT exist in the data (anti-join via EXCEPT).  Requires 2+ dimensions on
independent branches and no measures.

## Supported Dialects

{dialects}

## Metric Types

Metrics are queried by name like any measure.  Three types exist:

- **Derived** (default): expression-based, e.g. `{[Profit]} / {[Revenue]}`.
- **Cumulative**: running total, rolling window, or grain-to-date over a
  time dimension.  Queried as a regular metric name.
- **Period-over-Period (PoP)**: compares a measure across time periods
  (e.g. YoY growth, MoM difference).  Queried as a regular metric name.

## Measure Filters & Ratios

Measures can have **filters** that restrict which rows contribute to
aggregation (compiled as `CASE WHEN`).  A filtered measure like
"US Revenue" can then be used in a **ratio metric**:
`{{[US Revenue]}} / {{[Revenue]}}`  — no query-level WHERE needed.

## Grain Override & Filter Context

Measures can override their aggregation grain and filter context in the
OBML model definition (not at query time):

- **Grain override** (`grain:`) — controls which dimensions a measure
  aggregates over, independently from the query's dimensions.  Enables
  percent-of-total, percent-of-parent, and cross-grain calculations.
  Modes: `FIXED` (start empty) or `RELATIVE` (inherit query dims).
  Operators: `exclude`, `include`, `keepOnly`.
  `total: true` is shorthand for `grain: {{mode: FIXED}}`.

- **Filter context** (`filterContext:`) — controls which query WHERE
  filters apply to a measure.  Enables unfiltered baselines and
  selective filter exclusion.  Modes: `FIXED` (ignore all query
  filters) or `RELATIVE` (inherit and modify).
  Operators: `exclude`, `include` (static filters), `keepOnly`.

Both are defined in the OBML YAML and passed through to the API.
Call `get_obml_reference()` for full syntax and examples.

## Role-Playing Dimensions (via)

Dimensions can use `via` to force the join path through a specific intermediate
data object.  This enables **role-playing dimensions** — the same target table
accessed through different join paths:

```yaml
dimensions:
  SalesEmployee:
    dataObject: Employees
    column: Name
    via: Sales          # reach Employees through Sales

  ReturnEmployee:
    dataObject: Employees
    column: Name
    via: Returns        # reach Employees through Returns
```

Both dimensions reference `Employees.Name` but produce different results because
they join through different fact tables.  The `via` data object must be reachable
from the query's fact table, and the dimension's `dataObject` must be reachable
from `via` in the directed join graph.

When querying, simply use the role-playing dimension name:
```
compile_query(dimensions=["SalesEmployee"], measures=["Revenue"])
```

## Coalesce Dimensions

Role-playing dimensions appear as separate columns in CFL output — one row per
role per person.  To collapse them into a single output column, use
``coalesce_dimensions``::

    compile_query(
        measures=["Total Sales", "Total Purchases"],
        coalesce_dimensions=(
            '[{{"coalesce": ["SalesEmp", "PurchaseEmp"],'
            ' "as": "Employee"}}]'
        ),
    )

Or in ``query_json``::

    {{"select": {{
        "dimensions": [
            {{"coalesce": ["SalesEmployee", "PurchaseEmployee"], "as": "Employee"}}
        ],
        "measures": ["Total Sales", "Total Purchases"]
    }}}}

Each CFL leg projects only its own role-playing dimension (others NULL); the
outer wrapper emits ``COALESCE(d1, d2, ...) AS alias`` and groups by it.

Rules:
- At least 2 members; all must be existing model dimensions
- All members must share the same ``resultType``
- The ``as`` alias must not collide with any model dimension or measure name
- ``order_by`` may reference the alias directly
- ``where`` filters use the underlying dimension names (per-leg filtering)

## Raw Mode (Physical Column Access)

Raw mode returns un-aggregated rows — no GROUP BY, no measures, no metrics.
Pass ``fields`` instead of ``dimensions``/``measures``::

    compile_query(
        fields=["Orders.OrderDate", "Customers.Country"],
        distinct=True,
        where='[{{"field": "Customers.Country", "op": "equals", "value": "US"}}]',
        limit=100,
    )

Fields use ``DataObject.Column`` syntax referencing physical columns.

Raw mode is **mutually exclusive** with:
- ``dimensions`` / ``measures``
- ``having``
- ``dimensionsExclude``

``distinct`` is only valid in raw mode.  ``where``, ``order_by``, ``limit``,
and ``offset`` work in both modes.

## Execute Query — Output Formatting

``execute_query`` supports additional output parameters:

- ``format``: ``"json"`` (default) or ``"tsv"`` (tab-separated text)
- ``format_values``: Format numeric cells as locale-aware display strings
- ``locale``: BCP-47 locale tag (e.g. ``"de"``, ``"en-US"``)
- ``timezone``: IANA timezone (e.g. ``"Europe/Berlin"``)

## Default Dialect

When ``dialect`` is omitted from ``compile_query`` / ``execute_query``, the API
resolves it via: model ``settings.defaultDialect`` → server ``DB_VENDOR`` env →
``"postgres"``.  Use ``describe_model`` to see the model's default dialect.

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
  Fix: Derived metrics need `expression`.  Cumulative metrics need `measure`
  + `timeDimension` (and `window`/`grainToDate` are mutually exclusive).
  Period-over-period metrics need `expression` + `periodOverPeriod` block
  with `timeDimension`, `grain`, and `offsetGrain`.
- `MEASURE_FILTER_PARSE_ERROR`: Cannot parse a measure filter.
  Fix: Each filter needs `column` ({dataObject, column}), `operator`, and
  `values`.  Filter groups need `logic` (and/or) and `filters` array.
- `FILTER_PARSE_ERROR`: Cannot parse a static model filter.
  Fix: Each static filter needs `dataObject`, `column`, and `operator`.
  Use `value` for single-value operators, `values` for list operators (inlist, between).
  Dates must be ISO 8601 strings (e.g. '2026-01-01').

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

## Via (Role-Playing Dimension) Errors

- `INVALID_VIA_DATA_OBJECT`: Dimension `via` references an unknown data object, or the
  dimension's target data object is not reachable from `via` in the directed join graph.
  Fix: Check that `via` names an existing data object and that the dimension's `dataObject`
  is reachable from it through primary joins.
- `MISSING_VIA`: Warning — a dimension's target data object has direct joins from multiple
  fact tables, which may cause ambiguous join paths.
  Fix: Add role-playing dimensions with `via` to disambiguate, or ignore if ambiguity is
  intentional.

## Coalesce Dimension Errors

- `COALESCE_MISSING_ALIAS`: Coalesce dimension requires a non-empty `as` alias.
  Fix: Add `"as": "AliasName"` to the coalesce group.
- `DUPLICATE_COALESCE_ALIAS`: Duplicate coalesce alias in the same query.
  Fix: Use a unique `as` alias for each coalesce group.
- `COALESCE_ALIAS_COLLISION`: Coalesce alias collides with an existing model dimension
  or measure name.
  Fix: Choose an alias that doesn't match any dimension or measure name.
- `COALESCE_TOO_FEW_MEMBERS`: Coalesce requires at least 2 dimensions.
  Fix: Add at least 2 dimension names to the `coalesce` array.
- `COALESCE_TYPE_MISMATCH`: Coalesce members have incompatible result types.
  Fix: All members must share the same `resultType`.

## Grain & Filter Context Errors

- `UNKNOWN_GRAIN_DIMENSION`: Grain override references a non-existent dimension.
  Fix: Check dimension name in grain.include, grain.exclude, or grain.keepOnly.
- `UNKNOWN_FILTER_CONTEXT_FIELD`: Filter context references a non-existent field.
  Fix: Check field names in filterContext.exclude, filterContext.keepOnly, or
  filterContext.include[].field — must be a dimension name or DataObject.Column.
- `GRAIN_NOT_SUBSET`: Effective grain dimensions are not a subset of query dimensions.
  Fix: Ensure all grain dimensions are included in the query's dimension list.

## Resolution Errors (at query time)

- `AMBIGUOUS_JOIN`: Multiple join paths found during query resolution.
  Fix: Make join graph unambiguous or use `usePathNames`.
- `UNREACHABLE_REQUIRED_OBJECT`: A data object required by the query cannot be reached
  from the base object via directed joins. Many-to-one joins are forward-only; reverse
  traversal would inflate row counts.
  Fix: Add an explicit join from the base object (or an intermediate object) to the
  unreachable object, or split the query.
- `MALFORMED_EXPRESSION_REF`: Expression contains a malformed `{[...]}` reference
  (missing brackets, separators, or braces).
  Fix: Measure expressions use `{[DataObject].[Column]}` syntax;
  metric expressions use `{[Measure Name]}` syntax.  Check for missing
  `[`, `]`, `{`, `}`, or `.` separators.
- `INVALID_METRIC_EXPRESSION`: Metric expression could not be parsed.
  Fix: Use `{[Measure Name]}` syntax in metric expressions.
- `INVALID_FILTER_OPERATOR`: Unrecognised filter operator in query.
  Fix: Use a supported operator (equals, gt, gte, lt, lte, inlist, regex, blank, length_eq, etc.).
- `INVALID_FILTER_VALUE`: Filter value has the wrong type for the operator.
  Fix: `regex`/`notregex` require a string pattern; `length_eq`/`length_gt`/`length_lt`
  require an integer value.
- `INVALID_RELATIVE_FILTER`: Malformed relative time filter.
  Fix: Check unit (day/week/month/year), count, direction, include_current.
- `UNKNOWN_FILTER_FIELD`: Filter field is not a dimension (WHERE) or measure (HAVING).
  Fix: Check field name matches a dimension or measure in the model.
- `UNKNOWN_ORDER_BY_FIELD`: ORDER BY field is not a dimension or measure in the query's SELECT.
  Fix: Use a field name from `select.dimensions` or `select.measures`, or a numeric position.
- `INVALID_ORDER_BY_POSITION`: Numeric ORDER BY position is out of range.
  Fix: Use a position between 1 and the number of SELECT columns.

## Debugging Steps

1. Run `load_model(model_yaml)` — it validates and returns any errors.
2. Read the error code and message carefully.
3. Fix the YAML and re-load.
"""


@mcp.prompt
def write_obml_model() -> str:
    """OBML syntax reference — how to write a semantic model in YAML."""
    return _fetch_obml_reference()


@mcp.prompt
def write_query() -> str:
    """How to use the compile_query tool — simple and full modes."""
    dialect_list = ", ".join(f"`{d}`" for d in _fetch_dialect_names())
    return _WRITE_QUERY_TEXT.replace("{dialects}", dialect_list)


mcp.add_prompt(
    StaticPrompt(
        name="debug_validation",
        description="All OBML validation error codes with causes and fixes.",
        text=_DEBUG_VALIDATION_TEXT,
        meta={"text": _DEBUG_VALIDATION_TEXT},
    )
)


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
        logger.error(
            "API health check failed: %s %s",
            exc.response.status_code,
            exc.response.text,
        )
        raise SystemExit(1) from None

    try:
        mcp_version = importlib.metadata.version("orionbelt-semantic-layer-mcp")
    except importlib.metadata.PackageNotFoundError:
        mcp_version = None
    api_version = None
    with contextlib.suppress(ValueError, AttributeError):
        api_version = resp.json().get("version")
    if mcp_version and api_version:
        mcp_major = mcp_version.split(".")[0]
        api_major = api_version.split(".")[0]
        if mcp_major != api_major:
            logger.warning(
                "Version mismatch: MCP server v%s vs API v%s — "
                "major version differs, some features may not work correctly",
                mcp_version,
                api_version,
            )
        elif mcp_version != api_version:
            logger.warning(
                "Version mismatch: MCP server v%s vs API v%s",
                mcp_version,
                api_version,
            )


def _detect_api_mode() -> tuple[bool, bool]:
    """Query the API to detect single-model mode and query execution support.

    Returns:
        (single_model_mode, query_execute_enabled)
    """
    client = _get_client()
    try:
        resp = client.get(f"{_API_V1}/settings")
        resp.raise_for_status()
        data = resp.json()
        single = data.get("single_model_mode", False)
        can_execute = bool(data.get("query_execute", False))
        return single, can_execute
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("Could not detect API mode — defaulting to multi-model, no execute")
        return False, False


def main() -> None:
    """Run the MCP server using settings from environment / .env file."""
    global _single_model_mode

    logging.basicConfig(level=settings.log_level.upper())
    try:
        _version = importlib.metadata.version("orionbelt-semantic-layer-mcp")
    except importlib.metadata.PackageNotFoundError:
        _version = "dev"

    logger.info("=" * 60)
    logger.info("OrionBelt Semantic Layer MCP Server v%s", _version)
    logger.info("Thin MCP server — delegates to OrionBelt Semantic Layer REST API")
    logger.info("=" * 60)

    _check_api_health()

    _setup_mode_tools()

    if _single_model_mode:
        # Verify the pre-loaded model is valid and reachable (fail fast)
        client = _get_client()
        try:
            resp = client.get(f"{_API_V1}/schema")
            resp.raise_for_status()
            data = resp.json()
            logger.info(
                "Pre-loaded model validated: %d data objects, %d dimensions, "
                "%d measures, %d metrics",
                len(data.get("data_objects", [])),
                len(data.get("dimensions", [])),
                len(data.get("measures", [])),
                len(data.get("metrics", [])),
            )
        except httpx.HTTPStatusError as exc:
            logger.error("Pre-loaded model not available: %s", exc.response.text)
            raise SystemExit(1) from None
        except httpx.HTTPError as exc:
            logger.error("Cannot reach API to validate pre-loaded model: %s", exc)
            raise SystemExit(1) from None
        tool_count = 22 if _query_execute_enabled else 21
    else:
        tool_count = 25 if _query_execute_enabled else 24

    logger.info("")
    logger.info("Configuration:")
    logger.info("  API URL:    %s", settings.api_base_url)
    logger.info("  Transport:  %s", settings.mcp_transport)
    if settings.mcp_transport != "stdio":
        logger.info("  Host:       %s", settings.mcp_server_host)
        logger.info("  Port:       %s", settings.mcp_server_port)
    logger.info("  Log Level:  %s", settings.log_level)
    logger.info("  Timeout:    %ss", settings.api_timeout)
    logger.info("")
    logger.info(
        "Registered %d MCP tools (%s mode)",
        tool_count,
        "single-model" if _single_model_mode else "multi-model",
    )
    logger.info("")

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


if __name__ == "__main__":
    main()
