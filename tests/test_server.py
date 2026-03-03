"""Tests for the thin MCP server using respx to mock API calls."""

from __future__ import annotations

import httpx
import pytest
import respx

import server

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset module-level state before each test."""
    server._api_session_id = None
    server._http_client = None
    yield
    if server._http_client is not None:
        server._http_client.close()
        server._http_client = None
    server._api_session_id = None


@pytest.fixture()
def mock_api():
    """Provide a respx mock router scoped to the API base URL."""
    with respx.mock(base_url=server.settings.api_base_url) as rsps:
        yield rsps


def _mock_create_session(rsps: respx.MockRouter, session_id: str = "test-session-1"):
    """Add a mock for POST /sessions that returns the given session_id."""
    rsps.post("/sessions").mock(
        return_value=httpx.Response(
            201,
            json={
                "session_id": session_id,
                "created_at": "2025-01-01T00:00:00",
                "last_accessed_at": "2025-01-01T00:00:00",
                "model_count": 0,
                "metadata": {"source": "mcp"},
            },
        )
    )


# ---------------------------------------------------------------------------
# get_obml_reference
# ---------------------------------------------------------------------------


def test_get_obml_reference():
    """get_obml_reference returns the static OBML reference text."""
    result = server.get_obml_reference()
    assert "OBML" in result
    assert "dataObjects" in result
    assert "dimensions" in result
    assert "measures" in result


# ---------------------------------------------------------------------------
# load_model
# ---------------------------------------------------------------------------


def test_load_model(mock_api: respx.MockRouter):
    """load_model creates a session then POSTs to /sessions/{id}/models."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            201,
            json={
                "model_id": "m001",
                "data_objects": 2,
                "dimensions": 3,
                "measures": 1,
                "metrics": 0,
                "warnings": [],
            },
        )
    )

    result = server.load_model("version: 1.0\n...")
    assert "m001" in result
    assert "data objects: 2" in result
    assert "dimensions:   3" in result
    assert server._api_session_id == "test-session-1"


def test_load_model_with_warnings(mock_api: respx.MockRouter):
    """load_model includes warnings in the output."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            201,
            json={
                "model_id": "m002",
                "data_objects": 1,
                "dimensions": 1,
                "measures": 1,
                "metrics": 0,
                "warnings": ["SQL validation warning: syntax issue"],
            },
        )
    )

    result = server.load_model("version: 1.0\n...")
    assert "warnings:" in result
    assert "SQL validation warning" in result


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def test_validate_model_valid(mock_api: respx.MockRouter):
    """validate_model returns 'valid' message when model is valid."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/validate").mock(
        return_value=httpx.Response(
            200,
            json={"valid": True, "errors": [], "warnings": []},
        )
    )

    result = server.validate_model("version: 1.0\n...")
    assert "Model is valid" in result


def test_validate_model_with_errors(mock_api: respx.MockRouter):
    """validate_model formats validation errors."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/validate").mock(
        return_value=httpx.Response(
            200,
            json={
                "valid": False,
                "errors": [
                    {
                        "code": "UNKNOWN_COLUMN",
                        "message": "Column 'Foo' not found",
                        "path": "dimensions.Bar",
                    }
                ],
                "warnings": [],
            },
        )
    )

    result = server.validate_model("version: 1.0\n...")
    assert "validation errors" in result
    assert "UNKNOWN_COLUMN" in result
    assert "at dimensions.Bar" in result


# ---------------------------------------------------------------------------
# describe_model
# ---------------------------------------------------------------------------


def test_describe_model(mock_api: respx.MockRouter):
    """describe_model formats the model description."""
    _mock_create_session(mock_api)
    mock_api.get("/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(
            200,
            json={
                "data_objects": [
                    {
                        "label": "Orders",
                        "code": "ORDERS",
                        "columns": ["Order ID", "Amount"],
                        "join_targets": ["Customers"],
                    }
                ],
                "dimensions": [
                    {
                        "name": "Country",
                        "result_type": "string",
                        "data_object": "Customers",
                        "column": "Country",
                        "time_grain": None,
                    }
                ],
                "measures": [
                    {
                        "name": "Total Revenue",
                        "result_type": "float",
                        "aggregation": "sum",
                        "expression": None,
                    }
                ],
                "metrics": [],
            },
        )
    )

    result = server.describe_model("m001")
    assert "DATA OBJECTS:" in result
    assert "Orders" in result
    assert "DIMENSIONS:" in result
    assert "Country" in result
    assert "MEASURES:" in result
    assert "Total Revenue" in result


# ---------------------------------------------------------------------------
# compile_query
# ---------------------------------------------------------------------------


def test_compile_query_simple_mode(mock_api: respx.MockRouter):
    """compile_query simple mode sends dimensions/measures to API."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) FROM orders GROUP BY 1",
                "dialect": "postgres",
                "resolved": {
                    "fact_tables": ["Orders"],
                    "dimensions": ["Country"],
                    "measures": ["Total Revenue"],
                },
                "warnings": [],
                "sql_valid": True,
            },
        )
    )

    result = server.compile_query(
        model_id="m001",
        dialect="postgres",
        dimensions=["Country"],
        measures=["Total Revenue"],
    )
    assert "SELECT country" in result
    assert "Dialect: postgres" in result
    assert "Fact tables: Orders" in result


def test_compile_query_full_mode(mock_api: respx.MockRouter):
    """compile_query full mode sends query JSON to API."""
    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) FROM orders WHERE country = 'US' GROUP BY 1",
                "dialect": "snowflake",
                "resolved": {
                    "fact_tables": ["Orders"],
                    "dimensions": ["Country"],
                    "measures": ["Revenue"],
                },
                "warnings": [],
                "sql_valid": True,
            },
        )
    )

    result = server.compile_query(
        model_id="m001",
        dialect="snowflake",
        query_json='{"select":{"dimensions":["Country"],"measures":["Revenue"]}}',
    )
    assert "Dialect: snowflake" in result
    assert "SELECT country" in result


def test_compile_query_no_args():
    """compile_query raises ToolError when neither mode is provided."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Provide either"):
        server.compile_query(model_id="m001")


def test_compile_query_invalid_json():
    """compile_query raises ToolError on invalid query JSON."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Invalid query JSON"):
        server.compile_query(model_id="m001", query_json="{bad json")


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_empty(mock_api: respx.MockRouter):
    """list_models returns a message when no models are loaded."""
    _mock_create_session(mock_api)
    mock_api.get("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server.list_models()
    assert "No models loaded" in result


def test_list_models_with_models(mock_api: respx.MockRouter):
    """list_models formats the model list."""
    _mock_create_session(mock_api)
    mock_api.get("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "model_id": "m001",
                    "data_objects": 2,
                    "dimensions": 3,
                    "measures": 1,
                    "metrics": 0,
                }
            ],
        )
    )

    result = server.list_models()
    assert "m001" in result
    assert "2 objects" in result


# ---------------------------------------------------------------------------
# list_dialects
# ---------------------------------------------------------------------------


def test_list_dialects(mock_api: respx.MockRouter):
    """list_dialects fetches from /dialects (no session needed)."""
    mock_api.get("/dialects").mock(
        return_value=httpx.Response(
            200,
            json={
                "dialects": [
                    {
                        "name": "postgres",
                        "capabilities": {
                            "union_all_by_name": False,
                            "window_functions": True,
                        },
                    },
                    {
                        "name": "snowflake",
                        "capabilities": {
                            "union_all_by_name": True,
                            "window_functions": True,
                        },
                    },
                ]
            },
        )
    )

    result = server.list_dialects()
    assert "postgres" in result
    assert "snowflake" in result
    assert "union_all_by_name" in result


# ---------------------------------------------------------------------------
# Session auto-creation & caching
# ---------------------------------------------------------------------------


def test_session_created_once(mock_api: respx.MockRouter):
    """Session is created only once and reused for subsequent calls."""
    _mock_create_session(mock_api)
    mock_api.get("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    server.list_models()
    server.list_models()

    # POST /sessions should have been called exactly once
    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/sessions"
    ]
    assert len(session_calls) == 1


# ---------------------------------------------------------------------------
# Session expiry & retry
# ---------------------------------------------------------------------------


def test_session_retry_on_404(mock_api: respx.MockRouter):
    """When session returns 404, a new session is created and the call is retried."""
    # First session creation
    _mock_create_session(mock_api, session_id="session-old")

    # Pre-set the session to an old ID
    server._api_session_id = "session-old"

    # First call to models returns 404 (session expired)
    mock_api.get("/sessions/session-old/models").mock(
        return_value=httpx.Response(404, json={"detail": "Session not found"})
    )

    # New session creation after invalidation
    mock_api.post("/sessions").mock(
        return_value=httpx.Response(
            201,
            json={
                "session_id": "session-new",
                "created_at": "2025-01-01T00:00:00",
                "last_accessed_at": "2025-01-01T00:00:00",
                "model_count": 0,
                "metadata": {},
            },
        )
    )

    # Retry call succeeds with new session
    mock_api.get("/sessions/session-new/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server.list_models()
    assert "No models loaded" in result
    assert server._api_session_id == "session-new"


# ---------------------------------------------------------------------------
# Session safety on non-session 404s
# ---------------------------------------------------------------------------


def test_session_not_invalidated_on_model_404(mock_api: respx.MockRouter):
    """A 404 for a missing model should not invalidate the session."""
    from fastmcp.exceptions import ToolError

    server._api_session_id = "session-old"
    mock_api.get("/sessions/session-old/models/no-such-model").mock(
        return_value=httpx.Response(404, json={"detail": "Model not found"})
    )

    with pytest.raises(ToolError, match="API error.*404"):
        server.describe_model("no-such-model")

    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/sessions"
    ]
    assert len(session_calls) == 0
    assert server._api_session_id == "session-old"


def test_session_not_invalidated_on_plain_text_404(mock_api: respx.MockRouter):
    """A plain-text 404 (e.g. from a reverse proxy) should not invalidate the session."""
    from fastmcp.exceptions import ToolError

    server._api_session_id = "session-old"
    mock_api.get("/sessions/session-old/models/missing").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(ToolError, match="API error.*404"):
        server.describe_model("missing")

    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/sessions"
    ]
    assert len(session_calls) == 0
    assert server._api_session_id == "session-old"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_api_error_raises_tool_error(mock_api: respx.MockRouter):
    """API 4xx/5xx errors are raised as ToolError."""
    from fastmcp.exceptions import ToolError

    _mock_create_session(mock_api)
    mock_api.post("/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            422,
            json={"detail": "Invalid OBML model: parsing or validation failed"},
        )
    )

    with pytest.raises(ToolError, match="API error.*422"):
        server.load_model("bad yaml")


def test_connect_error_raises_tool_error():
    """Connection errors are raised as ToolError."""
    from fastmcp.exceptions import ToolError

    original_url = server.settings.api_base_url

    # Point to a non-existent host
    server.settings.api_base_url = "http://127.0.0.1:1"
    server._http_client = None  # Force new client

    with pytest.raises(ToolError, match="Cannot connect"):
        server.list_dialects()

    # Restore
    server.settings.api_base_url = original_url
    server._http_client = None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check_passes(mock_api: respx.MockRouter):
    """_check_api_health succeeds when /health returns 200."""
    mock_api.get("/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    # Should not raise
    server._check_api_health()


def test_health_check_connect_error():
    """_check_api_health exits on connection error."""
    original_url = server.settings.api_base_url
    server.settings.api_base_url = "http://127.0.0.1:1"
    server._http_client = None

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()

    server.settings.api_base_url = original_url
    server._http_client = None


def test_health_check_timeout(mock_api: respx.MockRouter):
    """_check_api_health exits on timeout."""
    mock_api.get("/health").mock(side_effect=httpx.TimeoutException("timed out"))

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


def test_health_check_server_error(mock_api: respx.MockRouter):
    """_check_api_health exits on 5xx."""
    mock_api.get("/health").mock(
        return_value=httpx.Response(503, text="Service Unavailable")
    )

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


# ---------------------------------------------------------------------------
# Prompts & resource
# ---------------------------------------------------------------------------


def test_write_obml_model_prompt():
    """write_obml_model prompt returns OBML syntax reference."""
    result = server.write_obml_model()
    assert "OBML" in result
    assert "dataObjects" in result


def test_write_query_prompt():
    """write_query prompt returns query compilation guide."""
    result = server.write_query()
    assert "Simple Mode" in result
    assert "Full Mode" in result


def test_debug_validation_prompt():
    """debug_validation prompt returns error codes."""
    result = server.debug_validation()
    assert "YAML_PARSE_ERROR" in result
    assert "UNKNOWN_COLUMN" in result


def test_obml_reference_resource():
    """obml://reference resource returns the OBML reference."""
    result = server.obml_reference()
    assert "OBML" in result
    assert "dataObjects" in result
