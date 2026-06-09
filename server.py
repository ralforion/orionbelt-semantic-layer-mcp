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
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal, NoReturn
from urllib.parse import quote

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.prompts.prompt import Prompt as _BasePrompt
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
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
    # Cloud Run injects PORT; takes precedence over MCP_SERVER_PORT.
    port: int | None = None
    log_level: str = "INFO"
    # "console" (pretty), "json" (structured), or "cloudrun" (JSON, GCP severity).
    log_format: Literal["console", "json", "cloudrun"] = "console"
    api_timeout: int = 30

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else self.mcp_server_port


settings = Settings()

# All API routes (except /health) are under the /v1 prefix since API v1.0.0
_API_V1 = "/v1"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _CloudRunJSONFormatter(logging.Formatter):
    """JSON log formatter compatible with GCP Cloud Logging severity mapping."""

    _SEVERITY = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": self._SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> None:
    """Configure root logger based on settings.log_format and settings.log_level."""
    level = settings.log_level.upper()
    formatter: logging.Formatter
    if settings.log_format in ("json", "cloudrun"):
        formatter = _CloudRunJSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _server_lifespan(server):
    """Register mode-dependent tools when the server starts.

    For stdio transport, mode detection runs eagerly here (the connection is
    established synchronously and tool registration must complete before the
    first message is processed).

    For HTTP/SSE transport on Cloud Run, mode detection is deferred to the
    first request via :class:`LazyInitMiddleware` so the container starts
    instantly — Cloud Run's startup probe shouldn't depend on the API service
    also being warm.
    """
    if settings.mcp_transport == "stdio":
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


def _package_version() -> str | None:
    """This MCP server's installed version, or None when not installed (dev tree)."""
    try:
        return importlib.metadata.version("orionbelt-semantic-layer-mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


# Reported as serverInfo.version on initialize (NOT the FastMCP library version),
# and used for the User-Agent and startup banner.
__version__ = _package_version() or "dev"

mcp = FastMCP("OrionBelt Semantic Layer", version=__version__, lifespan=_server_lifespan)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_api_session_id: str | None = None
_http_client: httpx.Client | None = None
_single_model_mode: bool = False
_query_execute_enabled: bool = False
_tools_registered: bool = False

# Model ids loaded into the current (multi-model) session. Drives the
# design-time ↔ run-time tool surface (see "Tool phase model" below).
_loaded_model_ids: set[str] = set()


# ---------------------------------------------------------------------------
# Tool phase model — design-time vs run-time surface switching
# ---------------------------------------------------------------------------
#
# The surface SWAPS between two phase-scoped sets as the lifecycle moves
# load → query → unload (design/PLAN_tool_phase_switching.md, Option A). Tools
# fall into three buckets:
#
#   * always (bucket 1) — ALWAYS listed, in both phases. The lifecycle/
#     transition verbs (load_model, remove_model), which
#     must stay available in the run phase so a second model can be loaded mid-session
#     (max_models_per_session = 10); plus run_batch, the self-contained one-shot
#     (loads/references a model inline in one call, so it depends on no prior
#     session state and is valid in either phase).
#   * design-only (bucket 2) — listed ONLY in the design phase (no model
#     loaded). Authoring/reference + pure file transforms. HIDDEN in run phase
#     so the run surface isn't polluted with authoring noise.
#   * run-only (bucket 3) — listed ONLY in the run phase (a model is loaded):
#     queries, introspection, execution.
#
#   design phase (no model) → bucket 1 + bucket 2
#   run phase   (model[s])  → bucket 1 + bucket 3   (a SWAP, not additive)
#
# Phase is *derived from explicit loaded-model state* (is a model resolvable for
# this session?), never from hidden per-connection transport state, so it stays
# stateless-clean. ``tools/list`` is filtered per phase by ``PhaseMiddleware``;
# a run-only verb invoked in the design phase is rejected with a structured
# guard error steering the host to ``load_model`` + re-list.

PHASE_DESIGN = "design"
PHASE_RUN = "run"

# Bucket 1 — always listed, in both phases: lifecycle/transition verbs plus the
# self-contained one-shot batch (depends on no prior session state) and the
# JSON-schema reference (needed to author execute_query payloads in either phase).
_ALWAYS_TOOLS: frozenset[str] = frozenset(
    {
        "load_model",
        "remove_model",
        "run_batch",
        "get_json_schema",
    }
)

# Bucket 2 — design-only. Authoring/reference + file transforms. Listed only in
# the design phase; hidden once a model is loaded.
_DESIGN_TOOLS: frozenset[str] = frozenset(
    {
        "get_obml_reference",
        "list_dialects",
    }
)

# Bucket 3 — run-only. Queries, introspection, execution. Listed only once a
# model is loaded; gated by the structured guard error otherwise. Names span the
# single-model and multi-model registrations (same names, different signatures).
_RUN_TIME_TOOLS: frozenset[str] = frozenset(
    {
        "get_model",
        "export_model_to_osi",
        "describe_model",
        "get_model_diagram",
        "find_artefacts",
        "explain_artefact",
        "execute_query",
        "list_examples",
        "get_example",
        "get_model_graph",
        "get_join_graph",
        "query_model_graph_by_sparql",
        "list_models",
    }
)


def _mark_model_loaded(model_id: str) -> None:
    """Record a model as loaded — flips the surface to the run-time phase."""
    if not model_id:
        return
    with _state_lock:
        _loaded_model_ids.add(model_id)


def _mark_model_removed(model_id: str) -> None:
    """Forget a model — flips back to design-time once none remain loaded."""
    with _state_lock:
        _loaded_model_ids.discard(model_id)


def _current_phase() -> str:
    """Resolve the active phase from loaded-model state.

    Single-model mode always has a pre-loaded model, so it is permanently in the
    run-time phase. Multi-model mode is design-time until ``load_model`` succeeds
    and reverts to design-time once every model is removed.
    """
    with _state_lock:
        if _single_model_mode or _loaded_model_ids:
            return PHASE_RUN
        return PHASE_DESIGN


def _is_runtime_tool(name: str) -> bool:
    """True if ``name`` is a run-only verb (bucket 3, gated by loaded-model state)."""
    return name in _RUN_TIME_TOOLS


def _tool_visible_in_phase(name: str, design_phase: bool) -> bool:
    """Phase visibility for ``name``: lifecycle always; otherwise swap by phase.

    design phase → always + design-only; run phase → always + run-only.
    A name in no bucket is treated as run-only (conservative: hidden until a
    model is loaded) — the partition test keeps every registered tool classified.
    """
    if name in _ALWAYS_TOOLS:
        return True
    if design_phase:
        return name in _DESIGN_TOOLS
    return name not in _DESIGN_TOOLS


# ---------------------------------------------------------------------------
# Capability gating — orthogonal to phase
# ---------------------------------------------------------------------------
#
# Some verbs should be omitted not because of lifecycle phase but because the
# server is *configured* not to support them. Unlike phase, a capability flag is
# fixed per server config (resolved once at startup), so the tools are always
# *registered* and simply filtered out of ``tools/list`` (and refused at call
# time) when their capability is disabled. This composes with phase gating: a
# verb is visible only if its phase is active *and* its capability is enabled.
#
# To add a future "the server can't do X here" flag: register a resolver in
# ``_CAPABILITY_RESOLVERS`` and map the affected tool names in ``_TOOL_CAPABILITY``.

CAP_QUERY_EXECUTE = "query_execute"

# Tool name → the capability flag it requires. Tools absent from this map need
# no capability and are always available (subject to phase).
_TOOL_CAPABILITY: dict[str, str] = {
    "execute_query": CAP_QUERY_EXECUTE,
}

# Capability flag → a resolver reading the current (startup-detected) config.
_CAPABILITY_RESOLVERS: dict[str, Callable[[], bool]] = {
    CAP_QUERY_EXECUTE: lambda: _query_execute_enabled,
}


def _capability_enabled(cap: str) -> bool:
    """True if capability ``cap`` is enabled. Unknown capabilities fail open."""
    resolver = _CAPABILITY_RESOLVERS.get(cap)
    return resolver() if resolver is not None else True


def _tool_capability_ok(name: str) -> bool:
    """True if the tool's required capability (if any) is enabled."""
    cap = _TOOL_CAPABILITY.get(name)
    return cap is None or _capability_enabled(cap)


# ---------------------------------------------------------------------------
# Lazy initialization middleware (HTTP transport only)
# ---------------------------------------------------------------------------


class LazyInitMiddleware(Middleware):
    """Defer API mode detection and tool registration to the first MCP request.

    On Cloud Run, the container starts immediately (Cloud Run startup probe
    only needs the HTTP listener to be up). The first MCP request — typically
    ``initialize`` from the client — triggers mode detection. This absorbs API
    cold-start latency into the first request rather than the container boot,
    so the two services don't have to be warm at the same time.
    """

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if not _tools_registered:
            _setup_mode_tools()
        return await call_next(context)


class PhaseMiddleware(Middleware):
    """Flip the visible tool surface between design-time and run-time phases.

    ``tools/list`` is filtered to the active phase (Option A, "hide-and-flip"):
    run-time verbs are hidden until a model is loaded. ``tools/call`` carries a
    guard so a stale host that calls a hidden run-time verb gets a structured
    "no model loaded — re-list" error rather than an opaque downstream failure.

    Note: the spec ``ttlMs`` / ``cacheScope`` cache hints on the ``tools/list``
    result (SEP-2549, final 2026-07-28) are not set here — FastMCP's
    ``on_list_tools`` hook only exposes the tool sequence, not the result
    envelope, and the fields are still a release candidate. The explicit
    re-list signal emitted by ``load_model`` / ``remove_model`` is the primary
    transition mechanism (and the plan-preferred one), so this is no loss today.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        tools = await call_next(context)
        design_phase = _current_phase() == PHASE_DESIGN
        visible = []
        for t in tools:
            # Capability gate (orthogonal to phase): drop verbs the server is
            # configured not to support, in any phase.
            if not _tool_capability_ok(t.name):
                continue
            # Phase gate (a swap): design phase shows lifecycle + design-only;
            # run phase shows lifecycle + run-only (design tools hidden).
            if not _tool_visible_in_phase(t.name, design_phase):
                continue
            visible.append(t)
        return visible

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        name = getattr(context.message, "name", None)
        if name and not _tool_capability_ok(name):
            raise ToolError(
                f"'{name}' is not available — this server is configured without "
                "the required capability (query execution is disabled). Use the "
                "compile_* tools to generate SQL without executing it."
            )
        if name and _is_runtime_tool(name) and _current_phase() == PHASE_DESIGN:
            raise ToolError(
                f"No model loaded — '{name}' is a run-time tool and is not "
                "available yet. Load a model first, then re-list tools: the "
                "run-time tool set (execute_query, describe_model, find_artefacts, "
                "…) becomes available once a model is loaded."
            )
        return await call_next(context)


# ---------------------------------------------------------------------------
# HTTP client & session management
# ---------------------------------------------------------------------------


def _get_client() -> httpx.Client:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None:
        with _state_lock:
            if _http_client is None:  # double-check under lock
                _http_client = httpx.Client(
                    base_url=settings.api_base_url,
                    timeout=settings.api_timeout,
                    headers={"User-Agent": f"OrionBelt-MCP/{__version__}"},
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
    """Extract error detail string from an API error response.

    Picks up the ``message`` field from structured detail dicts
    (``UnsupportedAggregationError``, ``UnsupportedGroupingError``, OBSQL
    translation errors, query resolution errors).  When the dict carries
    typed context (``dialect`` + ``aggregation`` / ``grouping``, or a list
    of ``errors`` with ``code`` / ``message``), it is appended to the
    message so the LLM sees the structured fields without parsing JSON.

    Also handles the top-level ``{message, errors, warnings}`` envelope the
    API uses for ``UNKNOWN_PROPERTY`` (Pydantic ``extra="forbid"`` 422s) —
    no ``detail`` wrapper there, so we promote the body itself.
    """
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" not in body and isinstance(body.get("errors"), list):
            detail: object = body
        else:
            detail = body.get("detail", response.text) if isinstance(body, dict) else response.text
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or str(detail)
            # UnsupportedAggregationError / UnsupportedGroupingError shape
            dialect = detail.get("dialect")
            aggregation = detail.get("aggregation")
            grouping = detail.get("grouping")
            if dialect and (aggregation or grouping):
                tag = aggregation or grouping
                kind = "aggregation" if aggregation else "grouping"
                message = f"{message} ({kind}={tag!r}, dialect={dialect!r})"
            # Nested error list (ResolutionError, SQLTranslationError, ...)
            errors = detail.get("errors")
            if isinstance(errors, list) and errors:
                parts = [
                    f"{e.get('code', '?')}: {e.get('message', '?')}"
                    for e in errors
                    if isinstance(e, dict)
                ]
                if parts:
                    message = f"{message} — {'; '.join(parts)}"
            return str(message)
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
        method,
        path,
        json_body=json_body,
        params=params,
        path_suffix=path_suffix,
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
_obsql_reference_cache: str | None = None
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


def _fetch_obsql_reference() -> str:
    """Fetch and cache the OBSQL reference from the API."""
    global _obsql_reference_cache
    if _obsql_reference_cache is None:
        with _state_lock:
            if _obsql_reference_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/reference/obsql", retry_on_expired=False)
                data = _parse_json(resp)
                _obsql_reference_cache = data["reference"]
    return _obsql_reference_cache


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


@mcp.resource("obsql://reference")
def obsql_reference() -> str:
    """Full OBSQL grammar reference — natural SQL surface against the semantic model."""
    return _fetch_obsql_reference()


# ---------------------------------------------------------------------------
# Mode-independent tools (always registered)
# ---------------------------------------------------------------------------


@mcp.tool
def get_obml_reference() -> str:
    """Get the OBML format reference.

    Returns the full specification with examples for dataObjects (including
    joins defined inside each dataObject), dimensions, measures, metrics, and
    expressions. Use this reference to understand the correct OBML syntax
    before composing models.
    """
    return _fetch_obml_reference()


@mcp.tool
def get_json_schema(name: Literal["obml", "query"]) -> str:
    """Get a published JSON Schema by name.

    Returns the raw JSON Schema document as a JSON string so callers can
    validate documents locally without round-tripping them to the API.

    Args:
        name: Either ``"obml"`` (the OBML model schema) or ``"query"``
            (the QueryObject input to ``execute_query``).
    """
    resp = _api_request("GET", f"{_API_V1}/reference/schemas/{name}", retry_on_expired=False)
    return json.dumps(_parse_json(resp), indent=2)


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


# ---------------------------------------------------------------------------
# Implementation functions (shared logic for both modes)
# ---------------------------------------------------------------------------


def _format_warning(w: Any) -> str:
    """Render a single warning. Accepts the structured ``StructuredWarning``
    shape introduced in API v2.2 (object with ``code``/``severity``/
    ``message``/``path``/``hint``) and the legacy plain-string shape.
    """
    if isinstance(w, str):
        return w
    if not isinstance(w, dict):
        return str(w)
    parts: list[str] = []
    code = w.get("code")
    severity = w.get("severity") or "warning"
    message = w.get("message", "")
    if code:
        parts.append(f"[{severity}:{code}]")
    elif severity != "warning":
        parts.append(f"[{severity}]")
    if message:
        parts.append(message)
    if w.get("path"):
        parts.append(f"(at {w['path']})")
    if w.get("hint"):
        parts.append(f"— hint: {w['hint']}")
    return " ".join(parts)


def _format_warnings(items: list | None, indent: str = "  warnings: ") -> list[str]:
    """Render a list of warnings as one or more output lines.

    Returns an empty list when ``items`` is falsy. The first line uses
    ``indent``; subsequent warnings get a continuation indent.
    """
    if not items:
        return []
    rendered = [_format_warning(w) for w in items]
    if len(rendered) == 1:
        return [f"{indent}{rendered[0]}"]
    pad = " " * len(indent)
    lines = [f"{indent}{rendered[0]}"]
    lines.extend(f"{pad}{r}" for r in rendered[1:])
    return lines


def _format_metric_summary(met: dict) -> str:
    """Format a one-line summary for a metric (derived, cumulative, PoP, or window)."""
    met_type = met.get("type", "derived")
    # partitionBy is shared between cumulative and window metrics (v2.6+).
    partition_by = met.get("partition_by") or met.get("partitionBy") or []
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
        if partition_by:
            parts.append(f"partitionBy: [{', '.join(partition_by)}]")
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
    if met_type == "window":
        fn = met.get("window_function") or met.get("windowFunction") or "?"
        parts = [f"type: window, windowFunction: {fn}"]
        if met.get("measure"):
            parts.append(f"measure: {met['measure']}")
        if met.get("time_dimension"):
            parts.append(f"timeDimension: {met['time_dimension']}")
        if partition_by:
            parts.append(f"partitionBy: [{', '.join(partition_by)}]")
        order_dir = met.get("order_direction") or met.get("orderDirection")
        if order_dir and order_dir != "desc":
            parts.append(f"orderDirection: {order_dir}")
        if met.get("offset") is not None:
            parts.append(f"offset: {met['offset']}")
        if met.get("buckets") is not None:
            parts.append(f"buckets: {met['buckets']}")
        default_val = met.get("default_value")
        if default_val is None:
            default_val = met.get("defaultValue")
        if default_val is not None:
            parts.append(f"defaultValue: {default_val}")
        return ", ".join(parts)
    return f"expr: {met.get('expression', '?')}"


def _fetch_effective_settings() -> dict[str, str]:
    """Best-effort fetch of the server-resolved dialect/timezone.

    These are the three agent-relevant fields the removed ``get_settings`` tool
    used to surface (``dialect.effective``, ``timezone.effective``; the model's
    ``defaultNumericDataType`` is already shown in the model SETTINGS block).
    Folding them into ``describe_model`` / ``load_model`` keeps the information
    available as *data* without a standalone settings verb.

    Returns a small dict (possibly empty). Enrichment only — any error is
    swallowed so it can never break the calling tool.
    """
    try:
        params: dict[str, str] = {}
        if not _single_model_mode and _api_session_id is not None:
            params["session_id"] = _api_session_id
        resp = _api_request(
            "GET",
            f"{_API_V1}/settings",
            retry_on_expired=False,
            params=params or None,
        )
        data = _parse_json(resp)
    except Exception:  # noqa: BLE001 — enrichment must never break the caller
        return {}
    out: dict[str, str] = {}
    dialect = data.get("dialect") or {}
    if dialect.get("effective"):
        out["dialect"] = dialect["effective"]
    tz = data.get("timezone") or {}
    if tz.get("effective"):
        out["timezone"] = tz["effective"]
    return out


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
        # Two-column statistical aggregates (corr, covar_*, regr_*) — column
        # order is significant in the compiled SQL, so surface it explicitly.
        cols = m.get("columns") or []
        if len(cols) > 1 and not m.get("expression"):
            col_refs = [
                f"{c.get('data_object') or c.get('dataObject', '?')}.{c.get('column', '?')}"
                if isinstance(c, dict)
                else str(c)
                for c in cols
            ]
            lines.append(f"    columns: [{', '.join(col_refs)}]")
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

    # Server-resolved effective dialect/timezone (the model SETTINGS block above
    # shows what the model *requested*; this shows what the server *resolved*
    # after factoring in env / host / database). Enrichment — omitted on error.
    effective = _fetch_effective_settings()
    if effective:
        lines.append("EFFECTIVE (server-resolved):")
        if effective.get("dialect"):
            lines.append(f"  dialect:  {effective['dialect']}")
        if effective.get("timezone"):
            lines.append(f"  timezone: {effective['timezone']}")
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


def _impl_execute_query(
    model_id: str | None,
    query_json: str,
    *,
    dialect: str | None = None,
    output_format: str = "json",
    format_values: bool | None = None,
    locale: str | None = None,
    timezone: str | None = None,
) -> str:
    """Compile and execute a QueryObject given as a JSON string (shared impl)."""
    logger.info("execute_query called (model_id=%s, dialect=%s)", model_id, dialect)
    try:
        query = json.loads(query_json)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"Invalid query JSON: {exc}. The QueryObject schema is available via "
            "get_json_schema('query') and worked examples via get_example."
        ) from exc

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
        resp = _shortcut_request("POST", "/query/execute", json_body=query, params=params)
    else:
        body: dict = {"model_id": model_id, "query": query}
        if dialect is not None:
            body["dialect"] = dialect
        resp = _session_request("POST", "/query/execute", json_body=body, params=extra_params)

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


# Artefact kinds discoverable via find_artefacts.
_ARTEFACT_KINDS: tuple[str, ...] = ("dimension", "measure", "metric")

# kind → (plural REST collection segment, list-section header).
_ARTEFACT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "dimension": ("dimensions", "Dimensions:"),
    "measure": ("measures", "Measures:"),
    "metric": ("metrics", "Metrics:"),
}


def _fetch_artefacts(model_id: str | None, kind: str) -> list[dict]:
    """Fetch the full set of records for one artefact kind."""
    segment = _ARTEFACT_ENDPOINTS[kind][0]
    if model_id is None:
        resp = _shortcut_request("GET", f"/{segment}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/{segment}")
    return _parse_json(resp)


def _render_dimension_lines(dims: list[dict]) -> list[str]:
    """Render dimension records as indented detail lines."""
    lines: list[str] = []
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
    return lines


def _render_measure_lines(measures: list[dict]) -> list[str]:
    """Render measure records as indented detail lines."""
    lines: list[str] = []
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
        cols = m.get("columns") or []
        if len(cols) > 1 and not m.get("expression"):
            col_refs = [
                f"{c.get('data_object') or c.get('dataObject', '?')}.{c.get('column', '?')}"
                if isinstance(c, dict)
                else str(c)
                for c in cols
            ]
            lines.append(f"    columns: [{', '.join(col_refs)}]")
        if m.get("description"):
            lines.append(f"    description: {m['description']}")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    return lines


def _render_metric_lines(metrics: list[dict]) -> list[str]:
    """Render metric records as indented detail lines."""
    lines: list[str] = []
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
    return lines


_ARTEFACT_RENDERERS = {
    "dimension": _render_dimension_lines,
    "measure": _render_measure_lines,
    "metric": _render_metric_lines,
}


def _impl_list_artefacts(
    model_id: str | None,
    kind: str | None = None,
    name: str | None = None,
) -> str:
    """Deterministic artefact lookup (shared implementation).

    Exact, complete enumeration — the authoritative set, not ranked candidates
    (that is ``find_artefacts``). With no ``name``, returns every artefact
    (optionally narrowed to one ``kind``); with ``name``, returns that exact
    artefact. Always renders full records at whatever cardinality results.
    """
    kinds = [kind] if kind else list(_ARTEFACT_KINDS)
    sections: list[str] = []
    for k in kinds:
        records = _fetch_artefacts(model_id, k)
        if name is not None:
            records = [r for r in records if r.get("name") == name]
        if not records:
            continue
        header = _ARTEFACT_ENDPOINTS[k][1]
        sections.append("\n".join([header, "", *_ARTEFACT_RENDERERS[k](records)]))
    if sections:
        return "\n\n".join(sections)
    # Nothing matched — phrase the empty case for the narrowing that was applied.
    if name is not None:
        of_kind = f" {kind}" if kind else " artefact"
        return f"No{of_kind} named '{name}' found in this model."
    if kind:
        return f"No {kind}s in this model."
    return "No artefacts in this model."


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


def _impl_find_artefacts(model_id: str | None, query: str, kind: str | None) -> str:
    """Fuzzy, ranked search across model artefacts (shared implementation)."""
    body: dict = {"query": query}
    if kind is not None:
        body["types"] = [kind]
    if model_id is None:
        resp = _shortcut_request("POST", "/find", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/find", json_body=body)
    data = _parse_json(resp)
    results = data.get("results", [])
    fuzzy = data.get("fuzzy_matches", [])
    exact = data.get("exact_matches")
    synonym = data.get("synonym_matches")
    if not results and not fuzzy:
        return f"No artefacts found matching '{query}'."
    lines = [f"Search results for '{query}':", ""]
    if exact or synonym:
        if exact:
            lines.append("Exact matches:")
            for r in exact:
                lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
        if synonym:
            if exact:
                lines.append("")
            lines.append("Synonym matches:")
            for r in synonym:
                lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    elif results:
        for r in results:
            lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    if fuzzy:
        if results or exact or synonym:
            lines.append("")
        lines.append("Fuzzy matches (no exact or synonym hit):")
        for f in fuzzy:
            score = f.get("score")
            score_str = f"  score={score:.2f}" if isinstance(score, int | float) else ""
            reason = f"  ({f['reason']})" if f.get("reason") else ""
            lines.append(f"  [{f.get('kind', '?')}] {f.get('name', '?')}{score_str}{reason}")
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


def _impl_load_model(
    model: dict | str | None,
    extends: list[dict] | str | None,
    inherits: str | None,
    dedup: bool,
) -> str:
    """Load a model and render the load summary (shared implementation)."""
    if not model:
        raise ToolError(
            "model is mandatory — provide the OBML model as a JSON object. "
            "The OBML reference is available as a separate tool to learn the structure."
        )
    if isinstance(model, str):
        try:
            model = json.loads(model)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid model JSON string: {exc}") from exc
    logger.info("load_model called")
    body: dict = {"model_json": model, "dedup": dedup}
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
    return _render_load_result(data)


def _render_load_result(data: dict, extra_lines: list[str] | None = None) -> str:
    """Render a model-load summary and run the design → run transition.

    Shared by ``_impl_load_model`` and ``_impl_load_model_from_osi``. ``data`` is
    the parsed ``ModelLoadResponse``. ``extra_lines`` are appended before the
    re-list footer (used to surface OSI conversion warnings).
    """
    load_state = data.get("model_load") or "fresh"
    header = (
        f"Model loaded ({load_state}).  model_id: {data['model_id']}"
        if load_state in ("fresh", "reused")
        else f"Model loaded successfully.  model_id: {data['model_id']}"
    )
    parts = [
        header,
        f"  data objects: {data['data_objects']}",
        f"  dimensions:   {data['dimensions']}",
        f"  measures:     {data['measures']}",
        f"  metrics:      {data['metrics']}",
    ]
    health = data.get("health")
    if health:
        parts.append(
            f"  health: {health.get('status', 'ok')}  "
            f"(joins: {health.get('joins', 0)}, "
            f"warnings: {health.get('warnings_count', 0)})"
        )
        orphans = health.get("orphan_data_objects") or []
        if orphans:
            parts.append(f"    orphan dataObjects: {', '.join(orphans)}")
        unreachable = health.get("unreachable_dimensions") or []
        if unreachable:
            parts.append(f"    unreachable dimensions: {', '.join(unreachable)}")
        for risk in health.get("fan_trap_risks") or []:
            tables = ", ".join(risk.get("tables", []))
            parts.append(f"    fan-trap risk on [{tables}]: {risk.get('reason', '')}")
    parts.extend(_format_warnings(data.get("warnings")))

    # Surface server-resolved dialect/timezone as data (see _fetch_effective_settings).
    effective = _fetch_effective_settings()
    if effective.get("dialect"):
        parts.append(f"  effective dialect:  {effective['dialect']}")
    if effective.get("timezone"):
        parts.append(f"  effective timezone: {effective['timezone']}")

    if extra_lines:
        parts.extend(extra_lines)

    # Forward transition (design → run): record the model and prompt a re-list so
    # the host discovers the now-available run-time tool set.
    _mark_model_loaded(data["model_id"])
    parts.append("")
    parts.append(
        "Run-time tools are now available (execute_query, describe_model, "
        "find_artefacts, …). Re-list tools to discover them."
    )
    return "\n".join(parts)


def _format_osi_input_validation(input_validation: dict | None) -> list[str]:
    """Render advisory OSI input-validation lines (OSI v0.2 schema check).

    Legacy OSI v0.1 inputs may produce spurious schema errors the converter's
    compat shim absorbs, so these are surfaced as advisory, not failure.
    """
    iv = input_validation or {}
    lines: list[str] = []
    in_errors = iv.get("schema_errors", []) + iv.get("semantic_errors", [])
    if in_errors:
        lines.append(f"  input validation issues (OSI v0.2 schema): {'; '.join(in_errors)}")
    if iv.get("semantic_warnings"):
        lines.append(f"  input validation warnings: {'; '.join(iv['semantic_warnings'])}")
    return lines


def _impl_load_model_from_osi(osi_yaml: str | None, dedup: bool) -> str:
    """Convert an OSI YAML model to OBML, load it, and render the summary."""
    if not osi_yaml or not osi_yaml.strip():
        raise ToolError("osi_yaml is mandatory — provide the OSI model as a YAML string.")
    logger.info("load_model_from_osi called")
    resp = _session_request(
        "POST", "/models/from-osi", json_body={"osi_yaml": osi_yaml, "dedup": dedup}
    )
    data = _parse_json(resp)

    extra: list[str] = []
    conversion_warnings = data.get("conversion_warnings") or []
    if conversion_warnings:
        extra.append(f"  OSI → OBML conversion warnings: {'; '.join(conversion_warnings)}")
    extra.extend(_format_osi_input_validation(data.get("input_validation")))
    return _render_load_result(data, extra_lines=extra)


def _impl_export_model_to_osi(
    model_id: str,
    model_name: str,
    model_description: str,
    ai_instructions: str,
) -> str:
    """Export a loaded model as OSI YAML (multi-model only — model_id required)."""
    params = {
        "model_name": model_name,
        "model_description": model_description,
        "ai_instructions": ai_instructions,
    }
    resp = _session_request("GET", f"/models/{model_id}/osi", params=params)
    data = _parse_json(resp)

    parts = [data["output_yaml"]]
    if data.get("warnings"):
        parts.append(f"\nWarnings: {'; '.join(data['warnings'])}")
    validation = data.get("validation") or {}
    if not validation.get("schema_valid", True) or not validation.get("semantic_valid", True):
        errors = validation.get("schema_errors", []) + validation.get("semantic_errors", [])
        parts.append(f"\nValidation errors: {'; '.join(errors)}")
    if validation.get("semantic_warnings"):
        parts.append(f"\nValidation warnings: {'; '.join(validation['semantic_warnings'])}")
    return "\n".join(parts)


def _impl_remove_model(model_id: str) -> str:
    """Remove a model and render the reverse-transition summary."""
    _session_request("DELETE", f"/models/{model_id}")
    # Reverse transition (run → design): forget the model; if none remain,
    # prompt a re-list so the host drops the now-invalid run-time verbs.
    _mark_model_removed(model_id)
    msg = f"Model {model_id} removed."
    if _current_phase() == PHASE_DESIGN:
        msg += (
            "\n\nNo models remain loaded — back to the design-time tool set. "
            "Re-list tools: run-time verbs are unavailable until a model "
            "is loaded again."
        )
    return msg


def _impl_list_examples(model_id: str | None, intent: str | None) -> str:
    """List canonical example queries authored alongside the model."""
    params: dict[str, str] | None = {"intent": intent} if intent else None
    if model_id is None:
        resp = _shortcut_request("GET", "/examples", params=params)
    else:
        resp = _session_request("GET", f"/models/{model_id}/examples", params=params)
    data = _parse_json(resp)
    examples = data.get("examples") or []
    suggestion = data.get("suggestion")
    if not examples:
        if suggestion:
            return suggestion
        return "No examples authored on this model."
    lines = ["Examples:", ""]
    for ex in examples:
        tags = ex.get("intent_tags") or []
        tag_str = f"  [tags: {', '.join(tags)}]" if tags else ""
        lines.append(f"  {ex['name']}{tag_str}")
        if ex.get("description"):
            lines.append(f"    {ex['description']}")
    if suggestion:
        lines.append("")
        lines.append(f"Note: {suggestion}")
    return "\n".join(lines)


def _impl_get_example(model_id: str | None, name: str) -> str:
    """Return a single example with its query and compiled SQL preview."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/examples/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/examples/{encoded}")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_run_batch(
    model_yaml: str | None,
    model_id: str | None,
    queries: list[dict],
    dialect: str | None,
    execute: bool,
    max_parallelism: int | None,
    fail_fast: bool,
    persist_model: bool,
    dedup: bool,
    session_id: str | None,
) -> str:
    """POST /v1/oneshot/batch and return a JSON-formatted result."""
    if not queries:
        raise ToolError("queries must contain at least one item")
    body: dict = {
        "queries": queries,
        "execute": execute,
        "fail_fast": fail_fast,
        "persist_model": persist_model,
        "dedup": dedup,
    }
    if model_yaml:
        body["model_yaml"] = model_yaml
    if model_id:
        body["model_id"] = model_id
    if dialect is not None:
        body["dialect"] = dialect
    if max_parallelism is not None:
        body["max_parallelism"] = max_parallelism
    if session_id is not None:
        body["session_id"] = session_id
    client = _get_client()
    resp = _do_request(client, "POST", f"{_API_V1}/oneshot/batch", body)
    if resp.status_code >= 400:
        _raise_api_error(resp)
    return json.dumps(_parse_json(resp), indent=2)


# ---------------------------------------------------------------------------
# Tool registration (mode-dependent)
# ---------------------------------------------------------------------------


def _resolve_model_id(model_id: str | None) -> str | None:
    """Normalize ``model_id`` for the active API mode.

    Single-model mode has one pre-loaded model, so any passed id is ignored and
    ``None`` is returned (routing the shared ``_impl_*`` to the shortcut
    endpoints). Multi-model mode requires an explicit id — a missing one is a
    clear error rather than a silent fall-through to the wrong endpoint.
    """
    if _single_model_mode:
        return None
    if not model_id:
        raise ToolError(
            "model_id is required (multi-model mode) — load a model first "
            "and pass the id it returns."
        )
    return model_id


def _register_model_tools() -> None:
    """Register the model-scoped tool surface — one definition per tool.

    Each tool takes an optional ``model_id`` normalized by ``_resolve_model_id``:
    ignored in single-model mode (one pre-loaded model), required at call time in
    multi-model mode. Tools that exist in only one mode (``get_model`` for
    single-model; ``load_model`` / ``remove_model`` / ``list_models`` /
    ``run_batch`` for multi-model) are registered conditionally. ``execute_query``
    is always registered and gated by the ``query_execute`` capability at
    list/call time (see ``PhaseMiddleware``).

    ``model_id`` is a loaded model's id in multi-model mode; omit it
    in single-model mode, where the one pre-loaded model is always used.
    """

    # ----- introspection -----

    @mcp.tool
    def describe_model(model_id: str | None = None) -> str:
        """Describe the contents of the model.

        Shows data objects (with columns and joins), dimensions, measures, and
        metrics.  In multi-model mode, use this after loading a model to explore
        its structure.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_describe_model(_resolve_model_id(model_id))

    @mcp.tool
    def get_model_diagram(
        model_id: str | None = None,
        show_columns: bool = True,
        theme: Literal["default", "dark", "forest", "neutral", "base"] = "default",
    ) -> str:
        """Generate a Mermaid ER diagram for the model.

        Returns a Mermaid diagram script that visualises the data objects,
        columns, and join relationships in the model.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
            show_columns: Whether to include column details in the diagram.
            theme: Mermaid diagram theme — one of the built-in themes
                "default", "dark", "forest", "neutral", or "base".
        """
        return _impl_get_model_diagram(_resolve_model_id(model_id), show_columns, theme)

    @mcp.tool
    def get_join_graph(model_id: str | None = None) -> str:
        """Return the join graph as an adjacency list.

        Shows the data object nodes and join edges (with cardinality and join
        columns) in the model.  Useful for understanding table relationships.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_join_graph(_resolve_model_id(model_id))

    @mcp.tool
    def get_model_graph(model_id: str | None = None) -> str:
        """Get the OBSL-Core RDF graph for the model as Turtle.

        Returns the semantic model's RDF graph serialized in Turtle format.
        The graph follows the OBSL-Core ontology and can be used for semantic
        web integration or further analysis.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_graph(_resolve_model_id(model_id))

    @mcp.tool
    def query_model_graph_by_sparql(query: str, model_id: str | None = None) -> str:
        """Execute a read-only SPARQL query against the model's RDF graph.

        Supports SELECT and ASK queries only (no INSERT/DELETE/UPDATE).
        The graph uses the OBSL-Core ontology.

        Args:
            query: SPARQL query string (SELECT or ASK).
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_sparql_query(_resolve_model_id(model_id), query)

    # ----- discovery -----

    @mcp.tool
    def find_artefacts(
        query: str | None = None,
        kind: Literal["dimension", "measure", "metric"] | None = None,
        name: str | None = None,
        model_id: str | None = None,
    ) -> str:
        """Look up model artefacts (dimensions, measures, metrics).

        Two modes, selected by whether you pass ``query``:

        - ``query`` set → fuzzy, ranked search. Matches names and synonyms
          (exact, synonym, and fuzzy/partial) and returns ranked candidates —
          for "I don't know the exact name". ``name`` is ignored in this mode.
        - ``query`` omitted → exact, deterministic, complete enumeration (the
          authoritative set). No args → every dimension, measure, and metric;
          ``kind`` only → the complete set of that one kind; ``name`` → that
          exact artefact (optionally constrained to ``kind``).

        ``kind`` narrows either mode to one artefact kind.

        Args:
            query: Search term (matched against names and synonyms). Omit for
                deterministic enumeration instead of ranked search.
            kind: Restrict to one artefact kind (dimension, measure, metric).
            name: In enumeration mode, return only the artefact with this exact
                name. Ignored when ``query`` is set.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        resolved = _resolve_model_id(model_id)
        if query is not None:
            return _impl_find_artefacts(resolved, query, kind)
        return _impl_list_artefacts(resolved, kind, name)

    @mcp.tool
    def explain_artefact(name: str, model_id: str | None = None) -> str:
        """Explain the lineage of a dimension, measure, or metric.

        Traces the composition chain from the named artefact down to the
        underlying data objects and columns.  Useful for understanding how a
        measure is computed or where a dimension originates.

        Args:
            name: The dimension, measure, or metric name to explain.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_explain_artefact(_resolve_model_id(model_id), name)

    @mcp.tool
    def list_examples(intent: str | None = None, model_id: str | None = None) -> str:
        """List canonical example queries authored alongside the model.

        Returns each example's name, description, and intent tags.  Use
        ``get_example`` for full detail (query payload + compiled SQL preview).

        Args:
            intent: Optional intent-tag filter.  Falls back through exact →
                contains → fuzzy tag matching.  When no examples match,
                the server returns a ``suggestion`` listing available tags.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_list_examples(_resolve_model_id(model_id), intent)

    @mcp.tool
    def get_example(name: str, model_id: str | None = None) -> str:
        """Get a single example by name with its query and compiled SQL preview.

        Args:
            name: The example's ``name`` field.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_example(_resolve_model_id(model_id), name)

    # ----- compile / plan -----

    # ----- execute (always registered; gated by the query_execute capability) -----

    @mcp.tool
    def execute_query(
        query_json: str,
        model_id: str | None = None,
        dialect: str | None = None,
        output_format: str = "json",
        format_values: bool | None = None,
        locale: str | None = None,
        timezone: str | None = None,
    ) -> str:
        """Compile and execute a semantic query (QueryObject), returning SQL + results.

        Pass ``query_json`` — a QueryObject as a JSON string. If no ``limit`` is set,
        a server-side default row limit applies.

        Args:
            query_json: Complete QueryObject as a JSON string: a ``select`` of
                dimensions/measures (or ``fields``), plus optional ``where``,
                ``having``, ``order_by``, ``limit``, ``offset``, ``dimensionsExclude``,
                ``usePathNames``, and coalesce groups. Dimension/measure names must
                match those defined in the loaded model.
            model_id: a loaded model's id (multi-model); omit in single-model.
            dialect: Target SQL dialect. When omitted the API resolves via
                model.settings.defaultDialect → server default.
            output_format: Response format — "json" (default) or "tsv".
            format_values: Format numeric cells as display strings.
            locale: BCP-47 locale for number formatting (e.g. "de").
            timezone: IANA timezone (e.g. "Europe/Berlin").
        """
        return _impl_execute_query(
            _resolve_model_id(model_id),
            query_json,
            dialect=dialect,
            output_format=output_format,
            format_values=format_values,
            locale=locale,
            timezone=timezone,
        )

    # ----- single-model only: the pre-loaded model's source -----

    if _single_model_mode:

        @mcp.tool
        def get_model() -> str:
            """Get the pre-loaded OBML YAML model source.

            Returns the original OBML YAML that was loaded into the API at
            startup.  Useful for understanding the model definition in the
            author's terms.  (Single-model mode only.)
            """
            resp = _api_request("GET", f"{_API_V1}/settings", retry_on_expired=False)
            data = _parse_json(resp)
            yaml_content = data.get("model_yaml")
            if not yaml_content:
                raise ToolError("No model YAML available from the API")
            return yaml_content

        return

    # ----- multi-model only: session/model management + one-shot batch -----

    @mcp.tool
    def load_model(
        model: dict | str | None = None,
        osi_yaml: str | None = None,
        extends: list[dict] | str | None = None,
        inherits: str | None = None,
        dedup: bool = True,
    ) -> str:
        """Load a semantic model and return a model_id.

        Provide exactly one source: ``model`` (native OBML JSON) or ``osi_yaml``
        (OSI YAML, converted to OBML server-side, surfacing conversion warnings).

        Args:
            model: OBML model as a JSON object with top-level keys: ``version``,
                ``dataObjects``, ``dimensions``, ``measures``, ``metrics``
                (camelCase throughout). Joins live INSIDE each dataObject
                (``joins`` list), not at the top level, and reference OBML column
                names. Supports ``extends``/``inherits``. Mutually exclusive with
                osi_yaml.
            osi_yaml: OSI model as a YAML string. Converted to OBML server-side
                before loading. Surfaces OSI → OBML conversion warnings and advisory
                OSI-schema validation alongside the model summary.
                ``extends``/``inherits`` do not apply to this source. Mutually
                exclusive with model.
            extends: Optional list of analytical fragment objects (dimensions,
                measures, metrics) to merge into the model before loading
                (OBML source only).
            inherits: Optional model_id of an already-loaded parent model whose
                data objects and joins the child inherits (OBML source only).
            dedup: When true (default), reuse the model_id of identical OBML
                already loaded in the session. Pass false to force a fresh load.
        """
        if osi_yaml is not None:
            if model is not None or extends is not None or inherits is not None:
                raise ToolError(
                    "Provide exactly one source: 'osi_yaml' cannot be combined "
                    "with 'model', 'extends', or 'inherits'."
                )
            return _impl_load_model_from_osi(osi_yaml, dedup)
        return _impl_load_model(model, extends, inherits, dedup)

    @mcp.tool
    def remove_model(model_id: str) -> str:
        """Remove a model from the current session.

        Args:
            model_id: The id of the model to remove.
        """
        return _impl_remove_model(model_id)

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
    def export_model_to_osi(
        model_id: str,
        model_name: str = "semantic_model",
        model_description: str = "",
        ai_instructions: str = "",
    ) -> str:
        """Export a loaded model as OSI (Open Semantic Interchange) YAML.

        Converts a model already loaded in the session (its faithful OBML
        source) to OSI format. Returns the OSI YAML plus any conversion
        warnings and validation results.

        Args:
            model_id: id of a loaded model.
            model_name: Name for the exported OSI model.
            model_description: Description for the OSI model.
            ai_instructions: AI instructions for the OSI model.
        """
        return _impl_export_model_to_osi(
            _resolve_model_id(model_id),
            model_name,
            model_description,
            ai_instructions,
        )

    @mcp.tool
    def run_batch(
        queries: list[dict],
        model_yaml: str | None = None,
        model_id: str | None = None,
        dialect: str | None = None,
        execute: bool = False,
        max_parallelism: int | None = None,
        fail_fast: bool = False,
        persist_model: bool = False,
        dedup: bool = True,
        session_id: str | None = None,
    ) -> str:
        """Run N independent queries against one model in a single round trip.

        Loads (or references) one OBML model, then runs every query in
        ``queries`` in parallel. Returns the raw JSON response (one result per
        query, keyed by ``id``, stable order). Provide exactly one of
        ``model_yaml`` or ``model_id``.

        Each ``queries`` item is a dict ``{"id": "q1", "query": {...},
        "execute": true, "dialect": "snowflake"}``; ``id`` is optional (server
        auto-assigns ``q0``, ``q1``, …) and per-item ``execute``/``dialect``
        override the batch defaults. Per-query partial failure is the default;
        ``fail_fast`` cancels the rest on first failure.

        Args:
            queries: List of query items (see above).
            model_yaml: OBML YAML string (mutually exclusive with model_id).
            model_id: ID of an already-loaded model (requires session_id).
            dialect: Default dialect for queries that omit one.
            execute: Default execute flag (compile-only when false).
            max_parallelism: Cap on concurrent executions; server caps further.
            fail_fast: If true, cancel remaining queries on first failure.
            persist_model: Keep a yaml-loaded model in the session afterwards.
            dedup: Reuse an already-loaded identical OBML model_id (default).
            session_id: Existing session to reuse; otherwise the API creates
                a new one for the batch.
        """
        return _impl_run_batch(
            model_yaml=model_yaml,
            model_id=model_id,
            queries=queries,
            dialect=dialect,
            execute=execute,
            max_parallelism=max_parallelism,
            fail_fast=fail_fast,
            persist_model=persist_model,
            dedup=dedup,
            session_id=session_id,
        )


def _setup_mode_tools() -> None:
    """Detect API mode and register the tool surface. Idempotent."""
    global _single_model_mode, _query_execute_enabled, _tools_registered
    if _tools_registered:
        return
    _single_model_mode, _query_execute_enabled = _detect_api_mode()
    logger.info(
        "%s mode detected",
        "Single-model" if _single_model_mode else "Multi-model",
    )
    _register_model_tools()
    logger.info(
        "Query execution %s (execute_* tools gated by capability)",
        "enabled" if _query_execute_enabled else "disabled",
    )
    _tools_registered = True


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
# Querying with OrionBelt

Queries are passed to `execute_query` as a single `query_json` argument — a
complete **QueryObject** encoded as a JSON string. Call
`get_json_schema("query")` for the authoritative QueryObject schema, and
`describe_model` to discover the dimension / measure / metric names to use.

## QueryObject shape

```
execute_query(
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

A minimal query is just `{"select": {"dimensions": [...], "measures": [...]}}`.
The fields below (filters, groups, dimensionsExclude, coalesce, raw `fields`,
…) all go **inside** `query_json`.

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
The full OBML syntax and examples are available in the OBML reference.

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

When querying, simply use the role-playing dimension name in `query_json`:
```
query_json='{"select": {"dimensions": ["SalesEmployee"], "measures": ["Revenue"]}}'
```

## Coalesce Dimensions

Role-playing dimensions appear as separate columns in CFL output — one row per
role per person.  To collapse them into a single output column, add a coalesce
group to the `select.dimensions` array in ``query_json``::

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
Put ``fields`` (instead of ``dimensions``/``measures``) inside ``select``::

    query_json='{{
      "select": {{"fields": ["Orders.OrderDate", "Customers.Country"],
                  "distinct": true}},
      "where": [{{"field": "Customers.Country", "op": "equals", "value": "US"}}],
      "limit": 100
    }}'

Fields use ``DataObject.Column`` syntax referencing physical columns.

Raw mode (``select.fields``) is **mutually exclusive** with
``select.dimensions`` / ``select.measures``, ``having``, and
``dimensionsExclude``.  ``select.distinct`` is only valid in raw mode;
``where``, ``order_by``, ``limit``, and ``offset`` work in both modes.

## Execute Query — Output Formatting

``execute_query`` supports additional output parameters:

- ``format``: ``"json"`` (default) or ``"tsv"`` (tab-separated text)
- ``format_values``: Format numeric cells as locale-aware display strings
- ``locale``: BCP-47 locale tag (e.g. ``"de"``, ``"en-US"``)
- ``timezone``: IANA timezone (e.g. ``"Europe/Berlin"``)

## Default Dialect

When ``dialect`` is omitted from ``execute_query``, the API
resolves it via: model ``settings.defaultDialect`` → server ``DB_VENDOR`` env →
``"postgres"``.  Use ``describe_model`` to see the model's default dialect.

## Result Caching & Determinism

The API hashes its result cache on compiled SQL.  Two behaviours keep that
deterministic:

- **Auto-ORDER BY on LIMIT** — when a query sets `limit` without `order_by`
  the compiler appends `ORDER BY <all dims>` (or `<all raw fields>`) so
  the cache never freezes an arbitrary slice.  Aggregate-only queries
  (no dimensions, no fields) are exempt — they already return one row.
- **Non-deterministic SQL bypass** — compiled SQL containing `RAND()`,
  `NOW()`, `CURRENT_DATE`, `TABLESAMPLE`, etc. is excluded from the cache.
  The `execute_query` JSON response surfaces this via
  `ttl_source = "no_cache:non_deterministic_sql"` so callers can see why
  a fresh round-trip happened.

If you need pagination, always set an explicit `order_by` and pair `limit`
with `offset`.

## Tips

- Use `describe_model` first to see available dimension/measure names.
- Use `list_dialects` to check dialect capabilities.
- Dimension names with time grain: append `:month`, `:year`, etc.
"""

_DEBUG_VALIDATION_TEXT = """\
# OBML Validation Error Codes

## Parse Errors

- `UNKNOWN_PROPERTY`: An OBML object or QueryObject contains a property name
  that is not part of its schema (e.g. `filtter:` instead of `filter:`).
  Strict parsing is the default — unknown keys are never silently dropped.
  The API response carries one error per offending key with a `path` (e.g.
  `where[0]`) and a "did you mean?" suggestion list derived from the model's
  real fields.
  Fix: Use the suggestion, or check the OBML reference (`get_obml_reference()`)
  for the exact field names. Common culprits: typos, snake_case vs camelCase
  mix-ups, fields that moved between versions.
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

## Dialect Capability Errors (at query time)

- `UNSUPPORTED_AGGREGATION`: The selected dialect does not implement the
  measure's aggregation function (HTTP 422 from the API, surfaced as a
  ToolError with `aggregation=…, dialect=…` context).
  Fix: Change `dialect`, or rewrite the measure to use a supported
  aggregation. See `list_dialects()` for each dialect's
  `unsupported_aggregations`.
- `UNSUPPORTED_GROUPING`: The selected dialect does not support the
  requested `WITH ROLLUP` / `WITH CUBE` (e.g. MySQL has no CUBE). The
  execute path returns HTTP 422.
  Fix: Change `dialect`, drop the modifier, or rewrite the query
  (e.g. UNION of explicit grain combinations).

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
def write_obsql_query() -> str:
    """OBSQL grammar reference — natural SQL surface against the model."""
    return _fetch_obsql_reference()


@mcp.prompt
def write_query() -> str:
    """How to build the QueryObject (query_json) for the execute_query tool."""
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

    mcp_version = _package_version()
    api_version = None
    with contextlib.suppress(ValueError, AttributeError):
        api_version = resp.json().get("version")
    if mcp_version and api_version:
        mcp_parts = mcp_version.split(".")
        api_parts = api_version.split(".")
        if mcp_parts[:2] != api_parts[:2]:
            logger.error(
                "Incompatible API version: MCP server v%s requires API v%s.%s.x — "
                "found v%s. Patch differences are allowed; major or minor mismatches "
                "are not supported.",
                mcp_version,
                mcp_parts[0],
                mcp_parts[1],
                api_version,
            )
            raise SystemExit(1)


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
    _configure_logging()

    logger.info("=" * 60)
    logger.info("OrionBelt Semantic Layer MCP Server v%s", __version__)
    logger.info("Thin MCP server — delegates to OrionBelt Semantic Layer REST API")
    logger.info("=" * 60)

    if settings.mcp_transport == "stdio":
        # stdio: eager init — connection is local & synchronous, fail-fast is fine.
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
            # Registered count — execute_query is always registered and gated at
            # list time by capability, so this counts everything registered. The
            # *visible* surface is smaller when query_execute is off (−1) or in
            # the design phase (run-only verbs hidden); single-model mode is
            # always run-time. design (5) + single-model run-scoped (12).
            tool_count = 17
        else:
            # design (5) + multi-model run/lifecycle-scoped (15).
            tool_count = 20
        mode_label = "single-model" if _single_model_mode else "multi-model"
    else:
        # HTTP/SSE: defer mode detection to the first request so the container
        # starts immediately (good for Cloud Run cold starts).
        mcp.add_middleware(LazyInitMiddleware())
        tool_count = 0
        mode_label = "deferred (lazy init on first request)"

    # Phase-scoped tool surface (design-time ↔ run-time). Added after LazyInit
    # so its tool-registration on_request runs first on HTTP transports.
    mcp.add_middleware(PhaseMiddleware())

    logger.info("")
    logger.info("Configuration:")
    logger.info("  API URL:    %s", settings.api_base_url)
    logger.info("  Transport:  %s", settings.mcp_transport)
    if settings.mcp_transport != "stdio":
        logger.info("  Host:       %s", settings.mcp_server_host)
        logger.info("  Port:       %s", settings.effective_port)
    logger.info("  Log Level:  %s", settings.log_level)
    logger.info("  Log Format: %s", settings.log_format)
    logger.info("  Timeout:    %ss", settings.api_timeout)
    logger.info("")
    if settings.mcp_transport == "stdio":
        logger.info("Registered %d MCP tools (%s mode)", tool_count, mode_label)
    else:
        logger.info("Tool registration: %s", mode_label)
    logger.info("")

    try:
        if settings.mcp_transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(
                transport=settings.mcp_transport,
                host=settings.mcp_server_host,
                port=settings.effective_port,
                log_level=settings.log_level.lower(),
            )
    except KeyboardInterrupt:
        logger.info("Shutting down…")


if __name__ == "__main__":
    main()
