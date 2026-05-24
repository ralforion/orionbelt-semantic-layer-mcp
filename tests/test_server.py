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
    server._obml_reference_cache = None
    server._dialect_names_cache = None
    server._single_model_mode = False
    server.settings.heartbeat_auth_token = None
    yield
    if server._http_client is not None:
        server._http_client.close()
        server._http_client = None
    server._api_session_id = None
    server._obml_reference_cache = None
    server._dialect_names_cache = None
    server._single_model_mode = False
    server.settings.heartbeat_auth_token = None


@pytest.fixture()
def mock_api():
    """Provide a respx mock router scoped to the API base URL."""
    with respx.mock(base_url=server.settings.api_base_url) as rsps:
        yield rsps


def _mock_create_session(rsps: respx.MockRouter, session_id: str = "test-session-1"):
    """Add a mock for POST /v1/sessions that returns the given session_id."""
    rsps.post("/v1/sessions").mock(
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


_MOCK_OBML_REFERENCE = "# OBML Reference\n\ndataObjects, dimensions, measures, metrics."


def _mock_obml_reference(rsps: respx.MockRouter):
    """Add a mock for GET /v1/reference/obml."""
    rsps.get("/v1/reference/obml").mock(
        return_value=httpx.Response(
            200,
            json={"reference": _MOCK_OBML_REFERENCE},
        )
    )


def _mock_dialects(rsps: respx.MockRouter):
    """Add a mock for GET /v1/dialects."""
    rsps.get("/v1/dialects").mock(
        return_value=httpx.Response(
            200,
            json={
                "dialects": [
                    {"name": "postgres", "capabilities": {}},
                    {"name": "mysql", "capabilities": {}},
                ]
            },
        )
    )


def test_get_obml_reference(mock_api):
    """get_obml_reference fetches the OBML reference from the API."""
    _mock_obml_reference(mock_api)
    result = server.get_obml_reference()
    assert "OBML" in result
    assert "dataObjects" in result
    assert "dimensions" in result
    assert "measures" in result


# ---------------------------------------------------------------------------
# load_model (multi-model mode — via _register_multi_model_tools)
# ---------------------------------------------------------------------------


def test_load_model(mock_api: respx.MockRouter):
    """load_model creates a session then POSTs to /v1/sessions/{id}/models."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models").mock(
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

    server._register_multi_model_tools()

    resp = server._session_request("POST", "/models", json_body={"model_yaml": "version: 1.0\n..."})
    data = server._parse_json(resp)
    assert data["model_id"] == "m001"
    assert server._api_session_id == "test-session-1"


def test_load_model_with_warnings(mock_api: respx.MockRouter):
    """load_model includes warnings in the output."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models").mock(
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

    resp = server._session_request("POST", "/models", json_body={"model_yaml": "version: 1.0\n..."})
    data = server._parse_json(resp)
    assert data["model_id"] == "m002"
    assert "SQL validation warning" in data["warnings"][0]


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def test_validate_model_valid(mock_api: respx.MockRouter):
    """validate_model returns 'valid' message when model is valid (multi-model only)."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/validate").mock(
        return_value=httpx.Response(
            200,
            json={"valid": True, "errors": [], "warnings": []},
        )
    )

    resp = server._session_request(
        "POST", "/validate", json_body={"model_yaml": "version: 1.0\n..."}
    )
    data = server._parse_json(resp)
    assert data["valid"] is True


def test_validate_model_with_errors(mock_api: respx.MockRouter):
    """validate_model returns errors (multi-model only)."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/validate").mock(
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

    resp = server._session_request(
        "POST", "/validate", json_body={"model_yaml": "version: 1.0\n..."}
    )
    data = server._parse_json(resp)
    assert data["valid"] is False
    assert data["errors"][0]["code"] == "UNKNOWN_COLUMN"


# ---------------------------------------------------------------------------
# get_model (single-model mode only)
# ---------------------------------------------------------------------------


def test_get_model_single_model_mode(mock_api: respx.MockRouter):
    """get_model returns pre-loaded OBML YAML from settings."""
    server._single_model_mode = True
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "single_model_mode": True,
                "model_yaml": "version: 1.0\ndataObjects:\n  Orders:\n    code: ORDERS",
                "session_ttl_seconds": 1800,
            },
        )
    )

    server._register_single_model_tools()
    # Call via the settings endpoint directly (same as get_model impl)
    resp = server._api_request("GET", f"{server._API_V1}/settings", retry_on_expired=False)
    data = server._parse_json(resp)
    assert data["model_yaml"].startswith("version: 1.0")
    assert "Orders" in data["model_yaml"]


def test_get_model_no_yaml_raises(mock_api: respx.MockRouter):
    """get_model raises ToolError when no YAML is available."""
    server._single_model_mode = True
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "single_model_mode": True,
                "model_yaml": None,
                "session_ttl_seconds": 1800,
            },
        )
    )

    resp = server._api_request("GET", f"{server._API_V1}/settings", retry_on_expired=False)
    data = server._parse_json(resp)
    assert data.get("model_yaml") is None


# ---------------------------------------------------------------------------
# describe_model (multi-model mode — via _impl_describe_model)
# ---------------------------------------------------------------------------


_DESCRIBE_RESPONSE = {
    "data_objects": [
        {
            "label": "Orders",
            "code": "ORDERS",
            "columns": ["Order ID", "Amount"],
            "join_targets": ["Customers"],
            "synonyms": [],
        }
    ],
    "dimensions": [
        {
            "name": "Country",
            "result_type": "string",
            "data_object": "Customers",
            "column": "Country",
            "time_grain": None,
            "synonyms": ["nation", "region"],
        }
    ],
    "measures": [
        {
            "name": "Total Revenue",
            "result_type": "float",
            "aggregation": "sum",
            "expression": None,
            "synonyms": ["sales", "income"],
        }
    ],
    "metrics": [],
}


def test_describe_model(mock_api: respx.MockRouter):
    """describe_model formats the model description (multi-model mode)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(200, json=_DESCRIBE_RESPONSE)
    )

    result = server._impl_describe_model("m001")
    assert "DATA OBJECTS:" in result
    assert "Orders" in result
    assert "DIMENSIONS:" in result
    assert "Country" in result
    assert "synonyms: nation, region" in result
    assert "MEASURES:" in result
    assert "Total Revenue" in result
    assert "synonyms: sales, income" in result


def test_describe_model_with_data_types_and_settings(mock_api: respx.MockRouter):
    """describe_model shows data_type on measures/metrics and model settings."""
    _mock_create_session(mock_api)
    response = {
        "data_objects": [
            {
                "label": "Orders",
                "code": "ORDERS",
                "columns": ["Amount"],
                "join_targets": [],
                "synonyms": [],
            }
        ],
        "dimensions": [],
        "measures": [
            {
                "name": "Revenue",
                "result_type": "float",
                "aggregation": "sum",
                "expression": None,
                "data_type": "decimal(18, 4)",
                "synonyms": [],
            }
        ],
        "metrics": [
            {
                "name": "Profit Margin",
                "type": "derived",
                "expression": "{[Profit]} / {[Revenue]}",
                "data_type": "decimal(18, 6)",
                "synonyms": [],
            }
        ],
        "settings": {
            "default_numeric_data_type": "decimal(18, 2)",
            "default_timezone": "Europe/Zagreb",
            "override_database_timezone": True,
        },
    }
    mock_api.get("/v1/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(200, json=response)
    )

    result = server._impl_describe_model("m001")
    assert "dataType: decimal(18, 4)" in result
    assert "dataType: decimal(18, 6)" in result
    assert "SETTINGS:" in result
    assert "defaultNumericDataType: decimal(18, 2)" in result
    assert "defaultTimezone: Europe/Zagreb" in result
    assert "overrideDatabaseTimezone: true" in result


# ---------------------------------------------------------------------------
# describe_model (single-model mode — via shortcut /v1/schema)
# ---------------------------------------------------------------------------


_SCHEMA_RESPONSE = {
    "model_id": "default-m001",
    "version": 1.0,
    "data_objects": [
        {
            "name": "Orders",
            "code": "ORDERS",
            "database": "EDW",
            "schema": "SALES",
            "columns": [
                {"name": "Order ID", "code": "ORDER_ID", "abstract_type": "integer"},
                {"name": "Amount", "code": "AMOUNT", "abstract_type": "float"},
            ],
            "join_targets": ["Customers"],
            "synonyms": [],
        }
    ],
    "dimensions": [
        {
            "name": "Country",
            "result_type": "string",
            "data_object": "Customers",
            "column": "Country",
            "time_grain": None,
            "synonyms": ["nation"],
        }
    ],
    "measures": [
        {
            "name": "Total Revenue",
            "result_type": "float",
            "aggregation": "sum",
            "expression": None,
            "synonyms": [],
        }
    ],
    "metrics": [],
}


def test_describe_model_single_model_mode(mock_api: respx.MockRouter):
    """describe_model uses shortcut GET /v1/schema in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/schema").mock(return_value=httpx.Response(200, json=_SCHEMA_RESPONSE))

    result = server._impl_describe_model(None)
    assert "Model default-m001:" in result
    assert "DATA OBJECTS:" in result
    assert "Orders" in result
    assert "columns: Order ID, Amount" in result
    assert "DIMENSIONS:" in result
    assert "Country" in result
    assert "MEASURES:" in result
    assert "Total Revenue" in result


# ---------------------------------------------------------------------------
# compile_query (multi-model mode)
# ---------------------------------------------------------------------------


def test_compile_query_simple_mode(mock_api: respx.MockRouter):
    """compile_query simple mode sends dimensions/measures to API."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
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

    result = server._impl_compile_query(
        model_id="m001",
        dialect="postgres",
        dimensions=["Country"],
        measures=["Total Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert "SELECT country" in result
    assert "Dialect: postgres" in result
    assert "Fact tables: Orders" in result


def test_compile_query_full_mode(mock_api: respx.MockRouter):
    """compile_query full mode sends query JSON to API."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
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

    result = server._impl_compile_query(
        model_id="m001",
        dialect="snowflake",
        dimensions=None,
        measures=None,
        query_json='{"select":{"dimensions":["Country"],"measures":["Revenue"]}}',
        use_path_names=None,
    )
    assert "Dialect: snowflake" in result
    assert "SELECT country" in result


def test_compile_query_with_explain(mock_api: respx.MockRouter):
    """compile_query includes explain plan in output."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT c.country, SUM(o.amount) FROM orders o JOIN customers c ...",
                "dialect": "postgres",
                "resolved": {
                    "fact_tables": ["Orders"],
                    "dimensions": ["Country"],
                    "measures": ["Revenue"],
                },
                "warnings": [],
                "sql_valid": True,
                "explain": {
                    "planner": "star",
                    "planner_reason": "single fact table",
                    "base_object": "Orders",
                    "base_object_reason": "only fact table",
                    "joins": [
                        {
                            "from_object": "Orders",
                            "to_object": "Customers",
                            "join_columns": ["Customer ID"],
                            "reason": "dimension Country",
                        }
                    ],
                    "where_filter_count": 0,
                    "having_filter_count": 0,
                    "has_totals": False,
                    "cfl_legs": [],
                },
            },
        )
    )

    result = server._impl_compile_query(
        model_id="m001",
        dialect="postgres",
        dimensions=["Country"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert "Planner: star" in result
    assert "Base object: Orders" in result
    assert "Join: Orders -> Customers" in result


def test_compile_query_sql_invalid(mock_api: respx.MockRouter):
    """compile_query shows warning when sql_valid is false."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT ...",
                "dialect": "postgres",
                "resolved": {"fact_tables": [], "dimensions": [], "measures": []},
                "warnings": [],
                "sql_valid": False,
            },
        )
    )

    result = server._impl_compile_query(
        model_id="m001",
        dialect="postgres",
        dimensions=["Country"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert "WARNING: Generated SQL may not be valid" in result


def test_compile_query_no_args():
    """compile_query raises ToolError when neither mode is provided."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Provide either"):
        server._impl_compile_query(
            model_id="m001",
            dialect="postgres",
            dimensions=None,
            measures=None,
            query_json=None,
            use_path_names=None,
        )


def test_compile_query_invalid_json():
    """compile_query raises ToolError on invalid query JSON."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="Invalid query JSON"):
        server._impl_compile_query(
            model_id="m001",
            dialect="postgres",
            dimensions=None,
            measures=None,
            query_json="{bad json",
            use_path_names=None,
        )


# ---------------------------------------------------------------------------
# compile_query (single-model mode — shortcut)
# ---------------------------------------------------------------------------


def test_compile_query_single_model_mode(mock_api: respx.MockRouter):
    """compile_query uses shortcut POST /v1/query/sql in single-model mode."""
    server._single_model_mode = True
    mock_api.post("/v1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) FROM orders GROUP BY 1",
                "dialect": "postgres",
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

    result = server._impl_compile_query(
        model_id=None,
        dialect="postgres",
        dimensions=["Country"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert "SELECT country" in result
    assert "Dialect: postgres" in result

    # Verify no session was created
    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
    assert len(session_calls) == 0


# ---------------------------------------------------------------------------
# execute_query (single-model mode — shortcut)
# ---------------------------------------------------------------------------


def test_execute_query_single_model_mode(mock_api: respx.MockRouter):
    """execute_query uses shortcut POST /v1/query/execute in single-model mode."""
    server._single_model_mode = True
    mock_api.post("/v1/query/execute").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) FROM orders GROUP BY 1",
                "dialect": "postgres",
                "columns": [
                    {"name": "country", "type": "string"},
                    {"name": "sum_amount", "type": "float"},
                ],
                "rows": [["US", 1000.0], ["DE", 500.0]],
                "row_count": 2,
                "execution_time_ms": 42,
            },
        )
    )

    result = server._impl_execute_query(
        model_id=None,
        dialect="postgres",
        dimensions=["Country"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert '"sql"' in result
    assert '"rows"' in result

    # Verify no session was created
    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
    assert len(session_calls) == 0


def test_execute_query_with_timezone(mock_api: respx.MockRouter):
    """execute_query passes through the timezone field from the API response."""
    server._single_model_mode = True
    mock_api.post("/v1/query/execute").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT order_date, SUM(amount) FROM orders GROUP BY 1",
                "dialect": "postgres",
                "columns": [
                    {"name": "order_date", "type": "timestamp"},
                    {"name": "sum_amount", "type": "float"},
                ],
                "rows": [["2025-01-01T00:00:00+01:00", 1000.0]],
                "row_count": 1,
                "execution_time_ms": 42,
                "timezone": "Europe/Zagreb",
            },
        )
    )

    result = server._impl_execute_query(
        model_id=None,
        dialect="postgres",
        dimensions=["Order Date"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert '"timezone": "Europe/Zagreb"' in result


def test_execute_query_multi_model_mode(mock_api: respx.MockRouter):
    """execute_query uses session-scoped POST in multi-model mode."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/execute").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) FROM orders GROUP BY 1",
                "dialect": "postgres",
                "columns": [
                    {"name": "country", "type": "string"},
                    {"name": "sum_amount", "type": "float"},
                ],
                "rows": [["US", 1000.0], ["DE", 500.0]],
                "row_count": 2,
                "execution_time_ms": 42,
            },
        )
    )

    result = server._impl_execute_query(
        model_id="m001",
        dialect="postgres",
        dimensions=["Country"],
        measures=["Revenue"],
        query_json=None,
        use_path_names=None,
    )
    assert '"sql"' in result
    assert '"rows"' in result

    # Verify the request body contains model_id and dialect
    execute_calls = [
        call
        for call in mock_api.calls
        if call.request.url.path == "/v1/sessions/test-session-1/query/execute"
    ]
    assert len(execute_calls) == 1
    import json

    body = json.loads(execute_calls[0].request.content)
    assert body["model_id"] == "m001"
    assert body["dialect"] == "postgres"
    assert "select" in body["query"]


# ---------------------------------------------------------------------------
# list_models (multi-model mode)
# ---------------------------------------------------------------------------


def test_list_models_empty(mock_api: respx.MockRouter):
    """list_models returns a message when no models are loaded."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    server._register_multi_model_tools()
    # Call via session request since list_models is registered dynamically
    resp = server._session_request("GET", "/models")
    models = server._parse_json(resp)
    assert models == []


def test_list_models_with_models(mock_api: respx.MockRouter):
    """list_models formats the model list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models").mock(
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

    resp = server._session_request("GET", "/models")
    models = server._parse_json(resp)
    assert len(models) == 1
    assert models[0]["model_id"] == "m001"


# ---------------------------------------------------------------------------
# list_dialects
# ---------------------------------------------------------------------------


def test_list_dialects(mock_api: respx.MockRouter):
    """list_dialects fetches from /v1/dialects (no session needed)."""
    mock_api.get("/v1/dialects").mock(
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
                        "unsupported_aggregations": [],
                    },
                    {
                        "name": "snowflake",
                        "capabilities": {
                            "union_all_by_name": True,
                            "window_functions": True,
                        },
                        "unsupported_aggregations": [],
                    },
                    {
                        "name": "mysql",
                        "capabilities": {
                            "union_all_by_name": False,
                            "window_functions": True,
                        },
                        "unsupported_aggregations": ["median"],
                    },
                ]
            },
        )
    )

    result = server.list_dialects()
    assert "postgres" in result
    assert "snowflake" in result
    assert "mysql" in result
    assert "union_all_by_name" in result
    assert "unsupported aggregations: median" in result


# ---------------------------------------------------------------------------
# remove_model (multi-model mode)
# ---------------------------------------------------------------------------


def test_remove_model(mock_api: respx.MockRouter):
    """remove_model sends DELETE to the API."""
    _mock_create_session(mock_api)
    mock_api.delete("/v1/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(204)
    )

    server._session_request("DELETE", "/models/m001")
    assert server._api_session_id == "test-session-1"


# ---------------------------------------------------------------------------
# get_model_schema
# ---------------------------------------------------------------------------


def test_get_model_schema(mock_api: respx.MockRouter):
    """get_model_schema returns JSON model structure (multi-model mode)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/schema").mock(
        return_value=httpx.Response(
            200,
            json={
                "model_id": "m001",
                "version": 1.0,
                "data_objects": [{"name": "Orders", "code": "ORDERS"}],
                "dimensions": [{"name": "Country"}],
                "measures": [{"name": "Revenue"}],
                "metrics": [],
            },
        )
    )

    result = server._impl_get_model_schema("m001")
    assert '"model_id": "m001"' in result
    assert '"Orders"' in result


def test_get_model_schema_single_model_mode(mock_api: respx.MockRouter):
    """get_model_schema uses shortcut GET /v1/schema in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/schema").mock(return_value=httpx.Response(200, json=_SCHEMA_RESPONSE))

    result = server._impl_get_model_schema(None)
    assert '"model_id": "default-m001"' in result
    assert '"Orders"' in result


# ---------------------------------------------------------------------------
# list_dimensions / get_dimension
# ---------------------------------------------------------------------------


def test_list_dimensions(mock_api: respx.MockRouter):
    """list_dimensions formats dimension list (multi-model mode)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Country",
                    "data_object": "Customers",
                    "column": "Country",
                    "result_type": "string",
                    "time_grain": None,
                    "synonyms": ["nation"],
                }
            ],
        )
    )

    result = server._impl_list_dimensions("m001")
    assert "Country" in result
    assert "Customers" in result
    assert "synonyms: nation" in result


def test_list_dimensions_empty(mock_api: respx.MockRouter):
    """list_dimensions handles empty list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server._impl_list_dimensions("m001")
    assert "No dimensions" in result


def test_list_dimensions_single_model_mode(mock_api: respx.MockRouter):
    """list_dimensions uses shortcut GET /v1/dimensions in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/dimensions").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Country",
                    "data_object": "Customers",
                    "column": "Country",
                    "result_type": "string",
                    "time_grain": None,
                    "synonyms": [],
                }
            ],
        )
    )

    result = server._impl_list_dimensions(None)
    assert "Country" in result


def test_get_dimension(mock_api: respx.MockRouter):
    """get_dimension returns JSON for a single dimension."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions/Country").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Country",
                "data_object": "Customers",
                "column": "Country",
                "result_type": "string",
            },
        )
    )

    result = server._impl_get_dimension("m001", "Country")
    assert '"Country"' in result


def test_get_dimension_url_encodes_name(mock_api: respx.MockRouter):
    """get_dimension URL-encodes names with spaces."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions/Customer%20Country").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Customer Country",
                "data_object": "Customers",
                "column": "Country",
                "result_type": "string",
            },
        )
    )

    result = server._impl_get_dimension("m001", "Customer Country")
    assert '"Customer Country"' in result


def test_get_dimension_single_model_mode(mock_api: respx.MockRouter):
    """get_dimension uses shortcut GET /v1/dimensions/{name} in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/dimensions/Country").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Country",
                "data_object": "Customers",
                "column": "Country",
                "result_type": "string",
            },
        )
    )

    result = server._impl_get_dimension(None, "Country")
    assert '"Country"' in result


# ---------------------------------------------------------------------------
# list_measures / get_measure
# ---------------------------------------------------------------------------


def test_list_measures(mock_api: respx.MockRouter):
    """list_measures formats measure list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/measures").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Total Revenue",
                    "result_type": "float",
                    "aggregation": "sum",
                    "expression": None,
                    "synonyms": ["sales"],
                }
            ],
        )
    )

    result = server._impl_list_measures("m001")
    assert "Total Revenue" in result
    assert "sum" in result
    assert "synonyms: sales" in result


def test_list_measures_with_data_type(mock_api: respx.MockRouter):
    """list_measures shows data_type when present."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/measures").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Revenue",
                    "result_type": "float",
                    "aggregation": "sum",
                    "expression": None,
                    "data_type": "decimal(18, 2)",
                    "synonyms": [],
                }
            ],
        )
    )

    result = server._impl_list_measures("m001")
    assert "Revenue" in result
    assert "dataType: decimal(18, 2)" in result


def test_get_measure(mock_api: respx.MockRouter):
    """get_measure returns JSON for a single measure."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/measures/Total%20Revenue").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Total Revenue",
                "result_type": "float",
                "aggregation": "sum",
            },
        )
    )

    result = server._impl_get_measure("m001", "Total Revenue")
    assert '"Total Revenue"' in result


# ---------------------------------------------------------------------------
# list_metrics / get_metric
# ---------------------------------------------------------------------------


def test_list_metrics(mock_api: respx.MockRouter):
    """list_metrics formats metric list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Profit Margin",
                    "expression": "{[Profit]} / {[Revenue]}",
                    "component_measures": ["Profit", "Revenue"],
                    "synonyms": [],
                }
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "Profit Margin" in result
    assert "components: Profit, Revenue" in result


def test_list_metrics_with_data_type(mock_api: respx.MockRouter):
    """list_metrics shows data_type when present."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Profit Margin",
                    "expression": "{[Profit]} / {[Revenue]}",
                    "data_type": "decimal(18, 6)",
                    "component_measures": ["Profit", "Revenue"],
                    "synonyms": [],
                }
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "Profit Margin" in result
    assert "dataType: decimal(18, 6)" in result


def test_list_metrics_cumulative(mock_api: respx.MockRouter):
    """list_metrics formats cumulative metrics correctly."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Running Revenue",
                    "type": "cumulative",
                    "expression": None,
                    "measure": "Total Revenue",
                    "time_dimension": "Order Date",
                    "component_measures": [],
                    "synonyms": [],
                },
                {
                    "name": "Profit Margin",
                    "type": "derived",
                    "expression": "{[Profit]} / {[Revenue]}",
                    "measure": None,
                    "time_dimension": None,
                    "component_measures": ["Profit", "Revenue"],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "Running Revenue" in result
    assert "type: cumulative" in result
    assert "measure: Total Revenue" in result
    assert "timeDimension: Order Date" in result
    assert "Profit Margin" in result
    assert "expr: {[Profit]} / {[Revenue]}" in result


def test_list_metrics_pop(mock_api: respx.MockRouter):
    """list_metrics formats period-over-period metrics correctly."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Revenue YoY Growth",
                    "type": "period_over_period",
                    "expression": "{[Revenue]}",
                    "measure": None,
                    "time_dimension": None,
                    "period_over_period": {
                        "time_dimension": "Order Date",
                        "grain": "month",
                        "offset": -1,
                        "offset_grain": "year",
                        "comparison": "percentChange",
                    },
                    "component_measures": ["Revenue"],
                    "synonyms": [],
                },
                {
                    "name": "Profit Margin",
                    "type": "derived",
                    "expression": "{[Profit]} / {[Revenue]}",
                    "measure": None,
                    "time_dimension": None,
                    "component_measures": ["Profit", "Revenue"],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "Revenue YoY Growth" in result
    assert "type: period_over_period" in result
    assert "expr: {[Revenue]}" in result
    assert "timeDimension: Order Date" in result
    assert "grain: month" in result
    assert "offsetGrain: year" in result
    assert "comparison: percentChange" in result
    assert "Profit Margin" in result
    assert "expr: {[Profit]} / {[Revenue]}" in result


def test_list_metrics_cumulative_extras(mock_api: respx.MockRouter):
    """list_metrics shows cumulative extras (window, grainToDate, cumulativeType)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "7-Day Rolling Avg",
                    "type": "cumulative",
                    "expression": None,
                    "measure": "Revenue",
                    "time_dimension": "Order Date",
                    "cumulative_type": "avg",
                    "window": 7,
                    "grain_to_date": None,
                    "component_measures": [],
                    "synonyms": [],
                },
                {
                    "name": "MTD Revenue",
                    "type": "cumulative",
                    "expression": None,
                    "measure": "Revenue",
                    "time_dimension": "Order Date",
                    "cumulative_type": "sum",
                    "window": None,
                    "grain_to_date": "month",
                    "component_measures": [],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "7-Day Rolling Avg" in result
    assert "cumulativeType: avg" in result
    assert "window: 7" in result
    assert "MTD Revenue" in result
    assert "grainToDate: month" in result
    # sum is the default, should not be shown
    assert "cumulativeType: sum" not in result


def test_list_metrics_cumulative_with_partition_by(mock_api: respx.MockRouter):
    """list_metrics surfaces partitionBy on cumulative metrics (v2.6+)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Revenue MA12 by Country",
                    "type": "cumulative",
                    "expression": None,
                    "measure": "Revenue",
                    "time_dimension": "order_month",
                    "cumulative_type": "avg",
                    "window": 12,
                    "grain_to_date": None,
                    "partition_by": ["Country"],
                    "component_measures": [],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "Revenue MA12 by Country" in result
    assert "partitionBy: [Country]" in result


def test_list_metrics_window(mock_api: respx.MockRouter):
    """list_metrics formats window metrics with all v2.6 fields."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Revenue Rank by Quarter",
                    "type": "window",
                    "window_function": "dense_rank",
                    "measure": "Revenue",
                    "order_direction": "desc",
                    "partition_by": ["Quarter"],
                    "component_measures": ["Revenue"],
                    "synonyms": [],
                },
                {
                    "name": "Revenue Prior Month",
                    "type": "window",
                    "window_function": "lag",
                    "measure": "Revenue",
                    "offset": 1,
                    "time_dimension": "order_month",
                    "partition_by": ["Country"],
                    "default_value": 0,
                    "component_measures": ["Revenue"],
                    "synonyms": [],
                },
                {
                    "name": "Revenue Quartile",
                    "type": "window",
                    "window_function": "ntile",
                    "measure": "Revenue",
                    "buckets": 4,
                    "partition_by": ["Year"],
                    "order_direction": "asc",
                    "component_measures": ["Revenue"],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_metrics("m001")
    assert "type: window" in result
    assert "windowFunction: dense_rank" in result
    assert "partitionBy: [Quarter]" in result
    assert "windowFunction: lag" in result
    assert "offset: 1" in result
    assert "timeDimension: order_month" in result
    assert "defaultValue: 0" in result
    assert "windowFunction: ntile" in result
    assert "buckets: 4" in result
    # asc is non-default, should be shown; desc is default and should not be
    assert "orderDirection: asc" in result
    assert "orderDirection: desc" not in result


def test_list_measures_two_column_aggregation(mock_api: respx.MockRouter):
    """list_measures surfaces 'columns' for two-column statistical aggregates (v2.6+)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/measures").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Revenue Spend Correlation",
                    "result_type": "float",
                    "aggregation": "corr",
                    "expression": None,
                    "columns": [
                        {"data_object": "Orders", "column": "Revenue"},
                        {"data_object": "Marketing", "column": "Spend"},
                    ],
                    "synonyms": [],
                },
                {
                    "name": "Total Revenue",
                    "result_type": "float",
                    "aggregation": "sum",
                    "expression": None,
                    "columns": [{"data_object": "Orders", "column": "Revenue"}],
                    "synonyms": [],
                },
            ],
        )
    )

    result = server._impl_list_measures("m001")
    assert "Revenue Spend Correlation" in result
    assert "corr" in result
    # Two-column agg surfaces ordered column refs
    assert "columns: [Orders.Revenue, Marketing.Spend]" in result
    # Single-column agg does NOT emit a columns line
    revenue_block = result.split("Total Revenue")[1] if "Total Revenue" in result else ""
    assert "columns:" not in revenue_block


def test_list_metrics_empty(mock_api: respx.MockRouter):
    """list_metrics handles empty list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server._impl_list_metrics("m001")
    assert "No metrics" in result


def test_get_metric(mock_api: respx.MockRouter):
    """get_metric returns JSON for a single metric."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics/Profit%20Margin").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Profit Margin",
                "expression": "{[Profit]} / {[Revenue]}",
                "component_measures": ["Profit", "Revenue"],
            },
        )
    )

    result = server._impl_get_metric("m001", "Profit Margin")
    assert '"Profit Margin"' in result


# ---------------------------------------------------------------------------
# explain_artefact
# ---------------------------------------------------------------------------


def test_explain_artefact(mock_api: respx.MockRouter):
    """explain_artefact formats lineage trace."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/explain/Total%20Revenue").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Total Revenue",
                "type": "measure",
                "lineage": [
                    {
                        "type": "measure",
                        "name": "Total Revenue",
                        "detail": "aggregation=sum, type=float",
                    },
                    {
                        "type": "column",
                        "name": "Orders.Amount",
                        "detail": "source column",
                    },
                    {
                        "type": "data_object",
                        "name": "Orders",
                        "detail": "table=EDW.SALES.ORDERS",
                    },
                ],
            },
        )
    )

    result = server._impl_explain_artefact("m001", "Total Revenue")
    assert "Total Revenue" in result
    assert "measure" in result
    assert "Orders.Amount" in result
    assert "source column" in result


# ---------------------------------------------------------------------------
# find_artefacts
# ---------------------------------------------------------------------------


def test_find_artefacts(mock_api: respx.MockRouter):
    """find_artefacts formats search results."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/find").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "dimension",
                        "name": "Country",
                        "match_field": "name",
                        "score": 1.0,
                    },
                    {
                        "type": "measure",
                        "name": "Revenue",
                        "match_field": "synonym",
                        "score": 1.0,
                    },
                ]
            },
        )
    )

    result = server._impl_find_artefacts("m001", "rev", None)
    assert "Country" in result
    assert "Revenue" in result
    assert "matched on synonym" in result


def test_find_artefacts_no_results(mock_api: respx.MockRouter):
    """find_artefacts handles no results."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/find").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = server._impl_find_artefacts("m001", "xyz", None)
    assert "No artefacts found" in result


def test_find_artefacts_single_model_mode(mock_api: respx.MockRouter):
    """find_artefacts uses shortcut POST /v1/find in single-model mode."""
    server._single_model_mode = True
    mock_api.post("/v1/find").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "type": "measure",
                        "name": "Revenue",
                        "match_field": "name",
                        "score": 1.0,
                    },
                ]
            },
        )
    )

    result = server._impl_find_artefacts(None, "rev", None)
    assert "Revenue" in result


# ---------------------------------------------------------------------------
# get_join_graph
# ---------------------------------------------------------------------------


def test_get_join_graph(mock_api: respx.MockRouter):
    """get_join_graph formats the adjacency list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/join-graph").mock(
        return_value=httpx.Response(
            200,
            json={
                "nodes": ["Orders", "Customers"],
                "edges": [
                    {
                        "from_object": "Orders",
                        "to_object": "Customers",
                        "cardinality": "many-to-one",
                        "columns_from": ["Customer ID"],
                        "columns_to": ["Cust ID"],
                        "secondary": False,
                        "path_name": None,
                    }
                ],
            },
        )
    )

    result = server._impl_get_join_graph("m001")
    assert "Orders" in result
    assert "Customers" in result
    assert "many-to-one" in result
    assert "Customer ID" in result


def test_get_join_graph_no_edges(mock_api: respx.MockRouter):
    """get_join_graph handles models with no joins."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/join-graph").mock(
        return_value=httpx.Response(200, json={"nodes": ["Orders"], "edges": []})
    )

    result = server._impl_get_join_graph("m001")
    assert "Orders" in result
    assert "No joins defined" in result


def test_get_join_graph_single_model_mode(mock_api: respx.MockRouter):
    """get_join_graph uses shortcut GET /v1/join-graph in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/join-graph").mock(
        return_value=httpx.Response(
            200,
            json={
                "nodes": ["Orders", "Customers"],
                "edges": [
                    {
                        "from_object": "Orders",
                        "to_object": "Customers",
                        "cardinality": "many-to-one",
                        "columns_from": ["Customer ID"],
                        "columns_to": ["Cust ID"],
                        "secondary": False,
                        "path_name": None,
                    }
                ],
            },
        )
    )

    result = server._impl_get_join_graph(None)
    assert "Orders" in result
    assert "many-to-one" in result


# ---------------------------------------------------------------------------
# get_settings
# ---------------------------------------------------------------------------


def test_get_settings(mock_api: respx.MockRouter):
    """get_settings returns API configuration."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "single_model_mode": False,
                "model_yaml": None,
                "session_ttl_seconds": 1800,
                "session_max_age_seconds": 86400,
                "max_sessions": 500,
                "max_models_per_session": 10,
            },
        )
    )

    result = server.get_settings()
    assert "Single-model mode: False" in result
    assert "Session TTL: 1800s" in result
    assert "Session max age: 86400s" in result
    assert "Max sessions: 500" in result
    assert "Max models/session: 10" in result


def test_get_settings_single_model(mock_api: respx.MockRouter):
    """get_settings shows pre-loaded model info in single-model mode."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "single_model_mode": True,
                "model_yaml": "version: 1.0\ndataObjects: ...",
                "session_ttl_seconds": 3600,
                "session_max_age_seconds": 86400,
                "max_sessions": 500,
                "max_models_per_session": 10,
            },
        )
    )

    result = server.get_settings()
    assert "Single-model mode: True" in result
    assert "Pre-loaded model: yes" in result


# ---------------------------------------------------------------------------
# get_model_diagram
# ---------------------------------------------------------------------------


def test_get_model_diagram(mock_api: respx.MockRouter):
    """get_model_diagram returns Mermaid ER diagram (multi-model mode)."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/diagram/er").mock(
        return_value=httpx.Response(
            200,
            json={"mermaid": "erDiagram\n  Orders ||--o{ Customers : joins"},
        )
    )

    result = server._impl_get_model_diagram("m001", show_columns=True, theme="default")
    assert "erDiagram" in result
    assert "Orders" in result


def test_get_model_diagram_single_model_mode(mock_api: respx.MockRouter):
    """get_model_diagram uses shortcut GET /v1/diagram/er in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/diagram/er").mock(
        return_value=httpx.Response(
            200,
            json={"mermaid": "erDiagram\n  Orders ||--o{ Customers : joins"},
        )
    )

    result = server._impl_get_model_diagram(None, show_columns=True, theme="default")
    assert "erDiagram" in result
    assert "Orders" in result


# ---------------------------------------------------------------------------
# convert_osi_to_obml
# ---------------------------------------------------------------------------


def test_convert_osi_to_obml(mock_api: respx.MockRouter):
    """convert_osi_to_obml calls /v1/convert/osi-to-obml."""
    mock_api.post("/v1/convert/osi-to-obml").mock(
        return_value=httpx.Response(
            200,
            json={
                "output_yaml": "version: 1.0\ndataObjects: {}",
                "warnings": [],
                "validation": {
                    "schema_valid": True,
                    "semantic_valid": True,
                    "schema_errors": [],
                    "semantic_errors": [],
                    "semantic_warnings": [],
                },
            },
        )
    )

    result = server.convert_osi_to_obml("osi_yaml_content")
    assert "version: 1.0" in result


def test_convert_osi_to_obml_with_validation_errors(mock_api: respx.MockRouter):
    """convert_osi_to_obml appends warnings and validation errors to output."""
    mock_api.post("/v1/convert/osi-to-obml").mock(
        return_value=httpx.Response(
            200,
            json={
                "output_yaml": "version: 1.0\ndataObjects: {}",
                "warnings": ["Column 'foo' unmapped"],
                "validation": {
                    "schema_valid": False,
                    "semantic_valid": False,
                    "schema_errors": ["Missing required field 'code'"],
                    "semantic_errors": ["Unknown column reference 'bar'"],
                    "semantic_warnings": ["Unused dimension 'baz'"],
                },
            },
        )
    )

    result = server.convert_osi_to_obml("osi_yaml_content")
    assert "version: 1.0" in result
    assert "Warnings: Column 'foo' unmapped" in result
    assert "Missing required field 'code'" in result
    assert "Unknown column reference 'bar'" in result
    assert "Validation warnings: Unused dimension 'baz'" in result


def test_convert_osi_to_obml_with_input_validation(mock_api: respx.MockRouter):
    """convert_osi_to_obml surfaces input-side validation (API v2.6+)."""
    mock_api.post("/v1/convert/osi-to-obml").mock(
        return_value=httpx.Response(
            200,
            json={
                "output_yaml": "version: 1.0\ndataObjects: {}",
                "warnings": [],
                "validation": {
                    "schema_valid": True,
                    "semantic_valid": True,
                    "schema_errors": [],
                    "semantic_errors": [],
                    "semantic_warnings": [],
                },
                "input_validation": {
                    "schema_valid": False,
                    "semantic_valid": True,
                    "schema_errors": ["Field 'kind' missing under semanticModels[0]"],
                    "semantic_errors": [],
                    "semantic_warnings": ["Legacy v0.1 shape detected"],
                },
            },
        )
    )

    result = server.convert_osi_to_obml("osi_yaml_content")
    assert "version: 1.0" in result
    assert "Input validation issues (OSI v0.2 schema)" in result
    assert "Field 'kind' missing under semanticModels[0]" in result
    assert "Input validation warnings: Legacy v0.1 shape detected" in result


# ---------------------------------------------------------------------------
# convert_obml_to_osi
# ---------------------------------------------------------------------------


def test_convert_obml_to_osi(mock_api: respx.MockRouter):
    """convert_obml_to_osi calls /v1/convert/obml-to-osi."""
    mock_api.post("/v1/convert/obml-to-osi").mock(
        return_value=httpx.Response(
            200,
            json={
                "output_yaml": "semantic_model:\n  name: test",
                "warnings": [],
                "validation": {
                    "schema_valid": True,
                    "semantic_valid": True,
                    "schema_errors": [],
                    "semantic_errors": [],
                    "semantic_warnings": [],
                },
            },
        )
    )

    result = server.convert_obml_to_osi("obml_yaml_content")
    assert "semantic_model" in result


def test_convert_obml_to_osi_with_validation_errors(mock_api: respx.MockRouter):
    """convert_obml_to_osi appends warnings and validation errors to output."""
    mock_api.post("/v1/convert/obml-to-osi").mock(
        return_value=httpx.Response(
            200,
            json={
                "output_yaml": "semantic_model:\n  name: test",
                "warnings": ["Metric 'ratio' uses filtered measures"],
                "validation": {
                    "schema_valid": True,
                    "semantic_valid": False,
                    "schema_errors": [],
                    "semantic_errors": ["Invalid expression in metric"],
                    "semantic_warnings": ["Deprecated aggregation type"],
                },
            },
        )
    )

    result = server.convert_obml_to_osi("obml_yaml_content")
    assert "semantic_model" in result
    assert "Warnings: Metric 'ratio' uses filtered measures" in result
    assert "Validation errors: Invalid expression in metric" in result
    assert "Validation warnings: Deprecated aggregation type" in result


# ---------------------------------------------------------------------------
# Session auto-creation & caching
# ---------------------------------------------------------------------------


def test_session_created_once(mock_api: respx.MockRouter):
    """Session is created only once and reused for subsequent calls."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions").mock(
        return_value=httpx.Response(200, json=[])
    )

    server._impl_list_dimensions("m001")
    server._impl_list_dimensions("m001")

    # POST /v1/sessions should have been called exactly once
    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
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

    # First call to dimensions returns 404 (session expired)
    mock_api.get("/v1/sessions/session-old/models/m001/dimensions").mock(
        return_value=httpx.Response(404, json={"detail": "Session not found"})
    )

    # New session creation after invalidation
    mock_api.post("/v1/sessions").mock(
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
    mock_api.get("/v1/sessions/session-new/models/m001/dimensions").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server._impl_list_dimensions("m001")
    assert "No dimensions" in result
    assert server._api_session_id == "session-new"


def test_session_retry_on_410(mock_api: respx.MockRouter):
    """When session returns 410 (Gone/expired), a new session is created and retried."""
    _mock_create_session(mock_api, session_id="session-old")

    server._api_session_id = "session-old"

    # First call returns 410 (session expired — new API behavior)
    mock_api.get("/v1/sessions/session-old/models/m001/dimensions").mock(
        return_value=httpx.Response(410, json={"detail": "Session has expired"})
    )

    # New session creation after invalidation
    mock_api.post("/v1/sessions").mock(
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
    mock_api.get("/v1/sessions/session-new/models/m001/dimensions").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server._impl_list_dimensions("m001")
    assert "No dimensions" in result
    assert server._api_session_id == "session-new"


def test_session_create_429_rate_limited(mock_api: respx.MockRouter):
    """429 on session creation surfaces a clear rate-limit error."""
    from fastmcp.exceptions import ToolError

    server._api_session_id = None
    mock_api.post("/v1/sessions").mock(
        return_value=httpx.Response(
            429,
            json={"detail": "Rate limit exceeded: max 10 session creations per 60s"},
            headers={"Retry-After": "60"},
        )
    )

    with pytest.raises(ToolError, match="rate-limited"):
        server._ensure_session()


# ---------------------------------------------------------------------------
# Session safety on non-session 404s
# ---------------------------------------------------------------------------


def test_session_not_invalidated_on_model_404(mock_api: respx.MockRouter):
    """A 404 for a missing model should not invalidate the session."""
    from fastmcp.exceptions import ToolError

    server._api_session_id = "session-old"
    mock_api.get("/v1/sessions/session-old/models/no-such-model").mock(
        return_value=httpx.Response(404, json={"detail": "Model not found"})
    )

    with pytest.raises(ToolError, match="API error.*404"):
        server._impl_describe_model("no-such-model")

    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
    assert len(session_calls) == 0
    assert server._api_session_id == "session-old"


def test_session_not_invalidated_on_plain_text_404(mock_api: respx.MockRouter):
    """A plain-text 404 (e.g. from a reverse proxy) should not invalidate the session."""
    from fastmcp.exceptions import ToolError

    server._api_session_id = "session-old"
    mock_api.get("/v1/sessions/session-old/models/missing").mock(
        return_value=httpx.Response(404, text="Not Found")
    )

    with pytest.raises(ToolError, match="API error.*404"):
        server._impl_describe_model("missing")

    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
    assert len(session_calls) == 0
    assert server._api_session_id == "session-old"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_api_error_raises_tool_error(mock_api: respx.MockRouter):
    """API 4xx/5xx errors are raised as ToolError."""
    from fastmcp.exceptions import ToolError

    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            422,
            json={"detail": "Invalid OBML model: parsing or validation failed"},
        )
    )

    with pytest.raises(ToolError, match="API error.*422"):
        server._session_request("POST", "/models", json_body={"model_yaml": "bad yaml"})


def test_unsupported_aggregation_error(mock_api: respx.MockRouter):
    """422 UnsupportedAggregationError returns readable message with context."""
    from fastmcp.exceptions import ToolError

    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": {
                    "error": "Unsupported aggregation",
                    "message": "Dialect 'mysql' does not support aggregation 'median'",
                    "dialect": "mysql",
                    "aggregation": "median",
                }
            },
        )
    )

    with pytest.raises(ToolError) as exc_info:
        server._impl_compile_query(
            model_id="m001",
            dialect="mysql",
            dimensions=None,
            measures=["Median Revenue"],
            query_json=None,
            use_path_names=None,
        )

    msg = str(exc_info.value)
    assert "does not support aggregation 'median'" in msg
    # Structured context is appended for callers that didn't read the message
    assert "aggregation='median'" in msg
    assert "dialect='mysql'" in msg


def test_unsupported_grouping_error(mock_api: respx.MockRouter):
    """422 UnsupportedGroupingError surfaces dialect + grouping context (v2.4.0+)."""
    from fastmcp.exceptions import ToolError

    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            422,
            json={
                "detail": {
                    "error": "Unsupported grouping",
                    "message": "Dialect 'mysql' does not support grouping 'cube'",
                    "dialect": "mysql",
                    "grouping": "cube",
                }
            },
        )
    )

    query_json = (
        '{"select": {"dimensions": ["Country"], "measures": ["Revenue"]}, "grouping": "cube"}'
    )
    with pytest.raises(ToolError) as exc_info:
        server._impl_compile_query(
            model_id="m001",
            dialect="mysql",
            dimensions=["Country"],
            measures=["Revenue"],
            query_json=query_json,
            use_path_names=None,
        )

    msg = str(exc_info.value)
    assert "does not support grouping 'cube'" in msg
    assert "grouping='cube'" in msg
    assert "dialect='mysql'" in msg


def test_resolution_error_appends_inner_errors():
    """Nested errors list (ResolutionError, OBSQL translation) is appended to detail."""
    body = {
        "detail": {
            "error": "Query resolution failed",
            "errors": [
                {"code": "UNKNOWN_DIMENSION", "message": "No such dimension 'Foo'"},
                {"code": "UNKNOWN_MEASURE", "message": "No such measure 'Bar'"},
            ],
        }
    }
    resp = httpx.Response(422, json=body)
    detail = server._parse_error_detail(resp)
    assert "Query resolution failed" in detail
    assert "UNKNOWN_DIMENSION: No such dimension 'Foo'" in detail
    assert "UNKNOWN_MEASURE: No such measure 'Bar'" in detail


def test_connect_error_raises_tool_error(monkeypatch):
    """Connection errors are raised as ToolError."""
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(server.settings, "api_base_url", "http://127.0.0.1:1")

    with pytest.raises(ToolError, match="Cannot connect"):
        server.list_dialects()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


def test_health_check_passes(mock_api: respx.MockRouter):
    """_check_api_health succeeds when /health returns 200."""
    mock_api.get("/health").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # Should not raise
    server._check_api_health()


def test_health_check_connect_error(monkeypatch):
    """_check_api_health exits on connection error."""
    monkeypatch.setattr(server.settings, "api_base_url", "http://127.0.0.1:1")

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


def test_health_check_timeout(mock_api: respx.MockRouter):
    """_check_api_health exits on timeout."""
    mock_api.get("/health").mock(side_effect=httpx.TimeoutException("timed out"))

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


def test_health_check_server_error(mock_api: respx.MockRouter):
    """_check_api_health exits on 5xx."""
    mock_api.get("/health").mock(return_value=httpx.Response(503, text="Service Unavailable"))

    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


# ---------------------------------------------------------------------------
# Version compatibility check
# ---------------------------------------------------------------------------


def _mock_health_with_version(mock_api: respx.MockRouter, version: str) -> None:
    mock_api.get("/health").mock(
        return_value=httpx.Response(
            200, json={"status": "ok", "version": version, "api_version": "v1"}
        )
    )


def test_version_check_patch_difference_allowed(mock_api, monkeypatch):
    """API patch version higher than MCP is silently accepted."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.0")
    _mock_health_with_version(mock_api, "2.6.1")
    # Should not raise
    server._check_api_health()


def test_version_check_patch_lower_allowed(mock_api, monkeypatch):
    """API patch version lower than MCP is silently accepted."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.5")
    _mock_health_with_version(mock_api, "2.6.0")
    server._check_api_health()


def test_version_check_exact_match_allowed(mock_api, monkeypatch):
    """Exact version match is silently accepted."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.0")
    _mock_health_with_version(mock_api, "2.6.0")
    server._check_api_health()


def test_version_check_minor_mismatch_errors(mock_api, monkeypatch):
    """API minor different from MCP exits with error."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.0")
    _mock_health_with_version(mock_api, "2.7.0")
    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


def test_version_check_minor_behind_errors(mock_api, monkeypatch):
    """API minor behind MCP exits with error."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.0")
    _mock_health_with_version(mock_api, "2.5.0")
    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


def test_version_check_major_mismatch_errors(mock_api, monkeypatch):
    """API major different from MCP exits with error."""
    monkeypatch.setattr("importlib.metadata.version", lambda _n: "2.6.0")
    _mock_health_with_version(mock_api, "3.0.0")
    with pytest.raises(SystemExit, match="1"):
        server._check_api_health()


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def test_detect_api_mode_single_model(mock_api: respx.MockRouter):
    """_detect_api_mode returns (True, False) for single-model without execute."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={"single_model_mode": True, "session_ttl_seconds": 1800},
        )
    )

    single, execute = server._detect_api_mode()
    assert single is True
    assert execute is False


def test_detect_api_mode_multi_model(mock_api: respx.MockRouter):
    """_detect_api_mode returns (False, False) for multi-model without execute."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={"single_model_mode": False, "session_ttl_seconds": 1800},
        )
    )

    single, execute = server._detect_api_mode()
    assert single is False
    assert execute is False


def test_detect_api_mode_query_execute(mock_api: respx.MockRouter):
    """_detect_api_mode returns query_execute=True when QUERY_EXECUTE is set."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={"single_model_mode": False, "query_execute": True},
        )
    )

    single, execute = server._detect_api_mode()
    assert single is False
    assert execute is True


def test_detect_api_mode_flight_alone_not_enough(mock_api: respx.MockRouter):
    """_detect_api_mode returns query_execute=False when only flight is configured."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={"single_model_mode": False, "flight": {"port": 8815}},
        )
    )

    single, execute = server._detect_api_mode()
    assert single is False
    assert execute is False


def test_detect_api_mode_fallback(monkeypatch):
    """_detect_api_mode defaults to (False, False) on error."""
    monkeypatch.setattr(server.settings, "api_base_url", "http://127.0.0.1:1")

    single, execute = server._detect_api_mode()
    assert single is False
    assert execute is False


# ---------------------------------------------------------------------------
# Shortcut request helper
# ---------------------------------------------------------------------------


def test_shortcut_request_no_session(mock_api: respx.MockRouter):
    """_shortcut_request does not create a session."""
    mock_api.get("/v1/dimensions").mock(return_value=httpx.Response(200, json=[]))

    resp = server._shortcut_request("GET", "/dimensions")
    assert resp.status_code == 200

    session_calls = [call for call in mock_api.calls if call.request.url.path == "/v1/sessions"]
    assert len(session_calls) == 0


def test_shortcut_request_with_params(mock_api: respx.MockRouter):
    """_shortcut_request passes query params."""
    mock_api.get("/v1/diagram/er").mock(
        return_value=httpx.Response(200, json={"mermaid": "erDiagram\n  Orders"})
    )

    resp = server._shortcut_request(
        "GET", "/diagram/er", params={"show_columns": "true", "theme": "dark"}
    )
    assert resp.status_code == 200
    # Verify params were sent
    assert "show_columns=true" in str(resp.request.url)


def test_shortcut_request_connect_error(monkeypatch):
    """_shortcut_request raises ToolError on connection error."""
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(server.settings, "api_base_url", "http://127.0.0.1:1")

    with pytest.raises(ToolError, match="Cannot connect"):
        server._shortcut_request("GET", "/dimensions")


# ---------------------------------------------------------------------------
# Prompts & resource
# ---------------------------------------------------------------------------


def test_write_obml_model_prompt(mock_api):
    """write_obml_model prompt fetches OBML reference from the API."""
    _mock_obml_reference(mock_api)
    result = server.write_obml_model()
    assert "OBML" in result
    assert "dataObjects" in result


def test_write_query_prompt(mock_api):
    """write_query prompt fetches dialects and injects them."""
    _mock_dialects(mock_api)
    result = server.write_query()
    assert "Simple Mode" in result
    assert "Full Mode" in result
    assert "`postgres`" in result
    assert "`mysql`" in result


def test_debug_validation_prompt():
    """debug_validation prompt returns error codes."""
    result = server._DEBUG_VALIDATION_TEXT
    assert "YAML_PARSE_ERROR" in result
    assert "UNKNOWN_COLUMN" in result


def test_obml_reference_resource(mock_api):
    """obml://reference resource fetches the OBML reference from the API."""
    _mock_obml_reference(mock_api)
    result = server.obml_reference()
    assert "OBML" in result
    assert "dataObjects" in result


# ---------------------------------------------------------------------------
# Lifecycle tests (review findings #1 and #2)
# ---------------------------------------------------------------------------


def test_get_client_fallback_when_metadata_unavailable(mock_api, monkeypatch):
    """_get_client() returns a usable client when package metadata is missing."""
    import importlib.metadata

    monkeypatch.setattr(
        "importlib.metadata.version",
        lambda _name: (_ for _ in ()).throw(
            importlib.metadata.PackageNotFoundError("orionbelt-semantic-layer-mcp")
        ),
    )
    client = server._get_client()
    assert isinstance(client, httpx.Client)
    assert "OrionBelt-MCP/dev" in client.headers["user-agent"]


def test_shutdown_resets_global_state(mock_api):
    """After shutdown cleanup, _get_client() returns a fresh open client."""
    # Step 1: create a client and a session id
    client_before = server._get_client()
    server._api_session_id = "session-lifecycle"

    # Step 2: simulate shutdown cleanup (the finally block in main())
    mock_api.delete("/v1/sessions/session-lifecycle").mock(return_value=httpx.Response(204))

    # Run the cleanup logic directly
    if server._api_session_id is not None:
        try:
            c = server._get_client()
            c.delete(f"/v1/sessions/{server._api_session_id}")
        except Exception:
            pass
        finally:
            server._api_session_id = None

    if server._http_client is not None:
        server._http_client.close()
        server._http_client = None

    # Step 3: verify state is fully reset
    assert server._http_client is None
    assert server._api_session_id is None

    # Step 4: a new _get_client() call should return a fresh, open client
    client_after = server._get_client()
    assert isinstance(client_after, httpx.Client)
    assert client_after is not client_before


# ---------------------------------------------------------------------------
# get_graph
# ---------------------------------------------------------------------------

_MOCK_TURTLE = """\
@prefix obsl: <https://orionbelt.dev/ontology/obsl-core#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

obsl:Orders a obsl:DataObject ;
    rdfs:label "Orders" .
"""


def test_get_graph(mock_api: respx.MockRouter):
    """get_graph returns RDF Turtle from session-scoped endpoint."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/graph").mock(
        return_value=httpx.Response(200, text=_MOCK_TURTLE, headers={"content-type": "text/turtle"})
    )

    result = server._impl_get_graph("m001")
    assert "@prefix obsl:" in result
    assert "Orders" in result


def test_get_graph_single_model_mode(mock_api: respx.MockRouter):
    """get_graph uses shortcut GET /v1/graph in single-model mode."""
    server._single_model_mode = True
    mock_api.get("/v1/graph").mock(
        return_value=httpx.Response(200, text=_MOCK_TURTLE, headers={"content-type": "text/turtle"})
    )

    result = server._impl_get_graph(None)
    assert "@prefix obsl:" in result
    assert "Orders" in result


# ---------------------------------------------------------------------------
# sparql_query
# ---------------------------------------------------------------------------


def test_sparql_query_select(mock_api: respx.MockRouter):
    """sparql_query formats SELECT results as a table."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/sparql").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "select",
                "variables": ["name", "type"],
                "results": [
                    {"name": "Revenue", "type": "measure"},
                    {"name": "Country", "type": "dimension"},
                ],
                "boolean": None,
            },
        )
    )

    result = server._impl_sparql_query("m001", "SELECT ?name ?type WHERE { ?s ?p ?o }")
    assert "name | type" in result
    assert "Revenue | measure" in result
    assert "Country | dimension" in result


def test_sparql_query_ask(mock_api: respx.MockRouter):
    """sparql_query formats ASK results."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/sparql").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "ask",
                "variables": [],
                "results": [],
                "boolean": True,
            },
        )
    )

    result = server._impl_sparql_query("m001", "ASK { ?s a obsl:DataObject }")
    assert "ASK result: True" in result


def test_sparql_query_no_results(mock_api: respx.MockRouter):
    """sparql_query handles empty SELECT results."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/sparql").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "select",
                "variables": ["name"],
                "results": [],
                "boolean": None,
            },
        )
    )

    result = server._impl_sparql_query("m001", "SELECT ?name WHERE { ?s ?p ?o }")
    assert "no results" in result.lower()


def test_sparql_query_single_model_mode(mock_api: respx.MockRouter):
    """sparql_query uses shortcut POST /v1/sparql in single-model mode."""
    server._single_model_mode = True
    mock_api.post("/v1/sparql").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "select",
                "variables": ["label"],
                "results": [{"label": "Orders"}],
                "boolean": None,
            },
        )
    )

    result = server._impl_sparql_query(None, "SELECT ?label WHERE { ?s rdfs:label ?label }")
    assert "Orders" in result


# ---------------------------------------------------------------------------
# v2.2: load_model dedup + health, structured warnings
# ---------------------------------------------------------------------------


def test_load_model_renders_health_and_reused_state(mock_api: respx.MockRouter):
    """load_model surfaces model_load=reused and the health block."""
    _mock_create_session(mock_api)
    route = mock_api.post("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            201,
            json={
                "model_id": "m001",
                "data_objects": 2,
                "dimensions": 3,
                "measures": 1,
                "metrics": 0,
                "warnings": [],
                "model_load": "reused",
                "health": {
                    "status": "warnings",
                    "data_objects": 2,
                    "joins": 1,
                    "orphan_data_objects": ["Junk"],
                    "fan_trap_risks": [
                        {
                            "tables": ["A", "B"],
                            "reason": "shared dim FK",
                            "suggested_pattern": "composite_fact_layer",
                        }
                    ],
                    "unreachable_dimensions": ["StaleDim"],
                    "warnings_count": 2,
                },
            },
        )
    )

    out = server._impl_load_model(
        model={"version": 1.0, "dataObjects": {}},
        extends=None,
        inherits=None,
        dedup=True,
    )

    assert "Model loaded (reused)" in out
    assert "model_id: m001" in out
    assert "health: warnings" in out
    assert "orphan dataObjects: Junk" in out
    assert "unreachable dimensions: StaleDim" in out
    assert "fan-trap risk on [A, B]" in out
    # default dedup=True must be sent in the request body
    sent = route.calls[0].request.read().decode()
    assert '"dedup": true' in sent or '"dedup":true' in sent


def test_load_model_dedup_false_sent(mock_api: respx.MockRouter):
    """load_model passes dedup=False through to the API."""
    _mock_create_session(mock_api)
    route = mock_api.post("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            201,
            json={
                "model_id": "m002",
                "data_objects": 1,
                "dimensions": 1,
                "measures": 0,
                "metrics": 0,
                "warnings": [],
                "model_load": "fresh",
            },
        )
    )

    out = server._impl_load_model(
        model={"version": 1.0, "dataObjects": {}},
        extends=None,
        inherits=None,
        dedup=False,
    )
    assert "Model loaded (fresh)" in out
    sent = route.calls[0].request.read().decode()
    assert '"dedup": false' in sent or '"dedup":false' in sent


def test_compile_query_renders_structured_warnings(mock_api: respx.MockRouter):
    """compile_query output handles the structured warning shape."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT 1",
                "dialect": "postgres",
                "resolved": {
                    "fact_tables": ["Orders"],
                    "dimensions": ["Country"],
                    "measures": ["Revenue"],
                },
                "warnings": [
                    {
                        "code": "SQL_VALIDATION",
                        "severity": "warning",
                        "message": "Generated SQL may not be valid",
                        "path": "select.measures[0]",
                        "hint": "Try a different dialect",
                    }
                ],
                "sql_valid": True,
            },
        )
    )

    out = server._impl_compile_query("m001", "postgres", ["Country"], ["Revenue"], None, None)
    assert "[warning:SQL_VALIDATION]" in out
    assert "Generated SQL may not be valid" in out
    assert "(at select.measures[0])" in out
    assert "hint: Try a different dialect" in out


# ---------------------------------------------------------------------------
# v2.2: find_artefacts fuzzy buckets
# ---------------------------------------------------------------------------


def test_find_artefacts_renders_fuzzy_buckets(mock_api: respx.MockRouter):
    """find_artefacts renders exact/synonym/fuzzy buckets when present."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/find").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "rev",
                "results": [
                    {"type": "measure", "name": "Revenue", "match_field": "name", "score": 1.0}
                ],
                "exact_matches": [
                    {"type": "measure", "name": "Revenue", "match_field": "name", "score": 1.0}
                ],
                "synonym_matches": [],
                "fuzzy_matches": [
                    {"name": "RevenueByYear", "kind": "measure", "score": 0.78, "reason": "trigram"}
                ],
            },
        )
    )

    out = server._impl_find_artefacts("m001", "rev", None)
    assert "Exact matches:" in out
    assert "[measure] Revenue" in out
    assert "Fuzzy matches" in out
    assert "score=0.78" in out
    assert "trigram" in out


def test_find_artefacts_renders_fuzzy_only(mock_api: respx.MockRouter):
    """When no exact/synonym hits, only fuzzy candidates are shown."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/find").mock(
        return_value=httpx.Response(
            200,
            json={
                "query": "rev",
                "results": [],
                "exact_matches": [],
                "synonym_matches": [],
                "fuzzy_matches": [
                    {"name": "Revenu", "kind": "measure", "score": 0.62, "reason": "levenshtein"}
                ],
            },
        )
    )

    out = server._impl_find_artefacts("m001", "rev", None)
    assert "Fuzzy matches" in out
    assert "Revenu" in out


# ---------------------------------------------------------------------------
# v2.2: plan_query
# ---------------------------------------------------------------------------


def test_plan_query_formats_response(mock_api: respx.MockRouter):
    """plan_query renders planner, physical tables, join path, warnings."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/plan").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "planner": "JoinPlanner",
                "planner_reason": "single fact table",
                "physical_tables": ["public.orders", "public.customers"],
                "join_path": [
                    {
                        "from_object": "Orders",
                        "to_object": "Customers",
                        "cardinality": "many-to-one",
                        "fk": "CustomerKey",
                    }
                ],
                "filters_applied": 1,
                "warnings": [],
                "would_compile": True,
                "compiled_sql_length_estimate": 128,
            },
        )
    )

    out = server._impl_plan_query("m001", "postgres", ["Country"], ["Revenue"], None, None)
    assert "Plan status: ok" in out
    assert "Would compile: True" in out
    assert "Planner: JoinPlanner" in out
    assert "public.orders" in out
    assert "Orders --[many-to-one]--> Customers on CustomerKey" in out
    assert "Filters applied: 1" in out
    assert "Compiled SQL length estimate: 128" in out


# ---------------------------------------------------------------------------
# v2.2: list_examples / get_example
# ---------------------------------------------------------------------------


def test_list_examples_renders_summaries(mock_api: respx.MockRouter):
    """list_examples renders authored examples with intent tags."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/examples").mock(
        return_value=httpx.Response(
            200,
            json={
                "examples": [
                    {
                        "name": "TopCustomers",
                        "description": "Top 10 customers by revenue",
                        "intent_tags": ["ranking", "customers"],
                    }
                ],
                "suggestion": None,
            },
        )
    )

    out = server._impl_list_examples("m001", None)
    assert "TopCustomers" in out
    assert "ranking, customers" in out
    assert "Top 10 customers by revenue" in out


def test_list_examples_with_intent_miss(mock_api: respx.MockRouter):
    """A miss returns the suggestion message."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/examples").mock(
        return_value=httpx.Response(
            200,
            json={
                "examples": [],
                "suggestion": "no examples for 'foo'; available tags: ranking, customers",
            },
        )
    )

    out = server._impl_list_examples("m001", "foo")
    assert "no examples for 'foo'" in out
    assert "available tags" in out


def test_get_example_url_encodes_name(mock_api: respx.MockRouter):
    """get_example URL-encodes the name and returns JSON."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/examples/Top%20Customers").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Top Customers",
                "description": "Ranked",
                "intent_tags": ["ranking"],
                "query": {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}},
                "compiled_sql_preview": "SELECT 1",
            },
        )
    )

    out = server._impl_get_example("m001", "Top Customers")
    assert '"name": "Top Customers"' in out
    assert '"compiled_sql_preview": "SELECT 1"' in out


# ---------------------------------------------------------------------------
# v2.2: run_batch
# ---------------------------------------------------------------------------


def test_run_batch_posts_oneshot(mock_api: respx.MockRouter):
    """run_batch POSTs /v1/oneshot/batch and forwards the response."""
    route = mock_api.post("/v1/oneshot/batch").mock(
        return_value=httpx.Response(
            200,
            json={
                "session_id": "s-1",
                "model_id": "m1",
                "model_persisted": False,
                "model_load": "fresh",
                "results": [
                    {
                        "id": "q0",
                        "status": "ok",
                        "sql": "SELECT 1",
                        "dialect": "postgres",
                        "executed": False,
                        "warnings": [],
                    }
                ],
                "batch_warnings": [],
            },
        )
    )

    out = server._impl_run_batch(
        model_yaml="version: 1.0\n",
        model_id=None,
        queries=[{"query": {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}}}],
        dialect=None,
        execute=False,
        max_parallelism=None,
        fail_fast=False,
        persist_model=False,
        dedup=True,
        session_id=None,
    )
    assert '"model_load": "fresh"' in out
    sent = route.calls[0].request.read().decode()
    assert '"model_yaml"' in sent
    assert '"dedup": true' in sent or '"dedup":true' in sent


def test_run_batch_rejects_empty_queries():
    """run_batch raises a ToolError when queries is empty."""
    with pytest.raises(server.ToolError):
        server._impl_run_batch(
            model_yaml="version: 1.0\n",
            model_id=None,
            queries=[],
            dialect=None,
            execute=False,
            max_parallelism=None,
            fail_fast=False,
            persist_model=False,
            dedup=True,
            session_id=None,
        )


# ---------------------------------------------------------------------------
# v2.2: get_settings shows oneshot_batch limits
# ---------------------------------------------------------------------------


def test_get_settings_shows_oneshot_batch_limits(mock_api: respx.MockRouter):
    """get_settings surfaces server-side oneshot_batch limits when present."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "2.2.1",
                "api_version": "v1",
                "single_model_mode": False,
                "session_ttl_seconds": 1800,
                "query_execute": False,
                "oneshot_batch": {
                    "max_queries": 50,
                    "max_parallelism": 8,
                    "default_timeout_ms": 30000,
                    "batch_timeout_ms": 120000,
                },
            },
        )
    )

    out = server.get_settings()
    assert "Oneshot batch limits:" in out
    assert "max queries:      50" in out
    assert "max parallelism:  8" in out
    assert "per-query timeout: 30000ms" in out
    assert "batch timeout:    120000ms" in out


def test_get_settings_renders_cache_block(mock_api: respx.MockRouter):
    """get_settings surfaces the new /v1/settings.cache block."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "2.2.1",
                "api_version": "v1",
                "single_model_mode": False,
                "session_ttl_seconds": 1800,
                "query_execute": False,
                "cache": {
                    "backend": "file",
                    "enabled": True,
                    "min_ttl_seconds": 30,
                    "max_ttl_seconds": 86400,
                    "max_value_bytes": 10_000_000,
                    "max_disk_bytes": 1_000_000_000,
                    "sweep_interval_seconds": 300,
                    "unknown_freshness_policy": "default_ttl",
                    "unknown_freshness_default_ttl": 60,
                    "heartbeat_endpoint_enabled": True,
                },
            },
        )
    )

    out = server.get_settings()
    assert "Result cache:" in out
    assert "backend:           file" in out
    assert "enabled:           True" in out
    assert "TTL bounds:        30s – 86400s" in out
    assert "unknown freshness: default_ttl" in out
    assert "(default TTL: 60s)" in out
    assert "heartbeat endpoint: live" in out


def test_get_settings_renders_cache_block_disabled(mock_api: respx.MockRouter):
    """When cache is noop and heartbeat is off, render a clear disabled state."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "2.2.1",
                "api_version": "v1",
                "single_model_mode": False,
                "session_ttl_seconds": 1800,
                "query_execute": False,
                "cache": {
                    "backend": "noop",
                    "enabled": False,
                    "min_ttl_seconds": 30,
                    "max_ttl_seconds": 86400,
                    "max_value_bytes": 10_000_000,
                    "max_disk_bytes": 1_000_000_000,
                    "sweep_interval_seconds": 300,
                    "unknown_freshness_policy": "no_cache",
                    "unknown_freshness_default_ttl": 0,
                    "heartbeat_endpoint_enabled": False,
                },
            },
        )
    )

    out = server.get_settings()
    assert "backend:           noop" in out
    assert "enabled:           False" in out
    assert "heartbeat endpoint: disabled (no token set on API)" in out


# ---------------------------------------------------------------------------
# v2.2 follow-up: cache stats, heartbeat, shortcut plan/examples,
# physical_tables in compile_query
# ---------------------------------------------------------------------------


def test_get_cache_stats_noop(mock_api: respx.MockRouter):
    """get_cache_stats short-circuits on noop backend."""
    mock_api.get("/v1/cache/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "backend": "noop",
                "entry_count": 0,
                "total_size_bytes": 0,
                "max_size_bytes": 0,
                "hit_count_total": 0,
                "miss_count_total": 0,
                "hit_rate": 0.0,
                "tracked_physical_tables": 0,
                "heartbeat_invalidations_total": 0,
            },
        )
    )

    out = server.get_cache_stats()
    assert "backend: noop" in out
    assert "cache is disabled" in out


def test_get_cache_stats_file_backend(mock_api: respx.MockRouter):
    """get_cache_stats renders counters for an active backend."""
    mock_api.get("/v1/cache/stats").mock(
        return_value=httpx.Response(
            200,
            json={
                "backend": "file",
                "entry_count": 12,
                "total_size_bytes": 1_234_567,
                "max_size_bytes": 10_000_000,
                "hit_count_total": 8,
                "miss_count_total": 4,
                "hit_rate": 0.6667,
                "oldest_entry": "2026-05-05T10:00:00Z",
                "next_sweep_at": "2026-05-05T11:00:00Z",
                "tracked_physical_tables": 3,
                "heartbeat_invalidations_total": 2,
            },
        )
    )

    out = server.get_cache_stats()
    assert "backend: file" in out
    assert "entries:           12" in out
    assert "1,234,567 / 10,000,000 bytes" in out
    assert "hits / misses:     8 / 4" in out
    assert "hit rate: 66.7%" in out
    assert "tracked tables:    3" in out
    assert "heartbeat invals:  2" in out


def test_heartbeat_requires_token(mock_api: respx.MockRouter):
    """heartbeat raises a clear error when HEARTBEAT_AUTH_TOKEN is unset."""
    server.settings.heartbeat_auth_token = None
    with pytest.raises(server.ToolError, match="HEARTBEAT_AUTH_TOKEN"):
        server.heartbeat("ob_demo", "public", "orders")


def test_heartbeat_sends_bearer_and_renders_response(mock_api: respx.MockRouter):
    """heartbeat sends Authorization: Bearer and renders affected dataObjects."""
    server.settings.heartbeat_auth_token = "secret-token"
    try:
        route = mock_api.post("/v1/heartbeat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "table_ref": "ob_demo.public.orders",
                    "recorded_at": "2026-05-05T18:30:00+00:00",
                    "invalidated_cache_entries": 4,
                    "affected_data_objects": ["Orders", "OrderLines"],
                },
            )
        )

        out = server.heartbeat("ob_demo", "public", "orders")

        assert "ob_demo.public.orders" in out
        assert "cache entries invalidated: 4" in out
        assert "Orders, OrderLines" in out
        sent = route.calls[0].request
        assert sent.headers.get("authorization") == "Bearer secret-token"
        assert b'"database":' in sent.read()
    finally:
        server.settings.heartbeat_auth_token = None


def test_compile_query_renders_physical_tables(mock_api: respx.MockRouter):
    """compile_query output surfaces the physical_tables block."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/sql").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT 1",
                "dialect": "postgres",
                "resolved": {
                    "fact_tables": ["Orders"],
                    "dimensions": ["Country"],
                    "measures": ["Revenue"],
                },
                "physical_tables": ["ob_demo.public.orders", "ob_demo.public.customers"],
                "warnings": [],
                "sql_valid": True,
            },
        )
    )

    out = server._impl_compile_query("m001", "postgres", ["Country"], ["Revenue"], None, None)
    assert "Physical tables: ob_demo.public.orders, ob_demo.public.customers" in out


def test_plan_query_uses_shortcut_in_single_model_mode(mock_api: respx.MockRouter):
    """plan_query falls through to /v1/query/plan when model_id is None."""
    server._single_model_mode = True
    mock_api.post("/v1/query/plan").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "ok",
                "planner": "JoinPlanner",
                "planner_reason": "single fact",
                "physical_tables": ["ob.public.orders"],
                "join_path": [],
                "filters_applied": 0,
                "warnings": [],
                "would_compile": True,
                "compiled_sql_length_estimate": 64,
            },
        )
    )

    out = server._impl_plan_query(None, None, ["Country"], ["Revenue"], None, None)
    assert "Plan status: ok" in out
    assert "ob.public.orders" in out


def test_list_examples_uses_shortcut_in_single_model_mode(mock_api: respx.MockRouter):
    """list_examples falls through to /v1/examples when model_id is None."""
    server._single_model_mode = True
    mock_api.get("/v1/examples").mock(
        return_value=httpx.Response(
            200,
            json={
                "examples": [
                    {
                        "name": "TopCustomers",
                        "description": "Top 10",
                        "intent_tags": ["ranking"],
                    }
                ],
                "suggestion": None,
            },
        )
    )

    out = server._impl_list_examples(None, None)
    assert "TopCustomers" in out
    assert "ranking" in out


def test_get_example_uses_shortcut_in_single_model_mode(mock_api: respx.MockRouter):
    """get_example falls through to /v1/examples/{name} when model_id is None."""
    server._single_model_mode = True
    mock_api.get("/v1/examples/Top%20Customers").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": "Top Customers",
                "description": "Ranked",
                "intent_tags": ["ranking"],
                "query": {"select": {"dimensions": ["Country"], "measures": ["Revenue"]}},
                "compiled_sql_preview": "SELECT 1",
            },
        )
    )

    out = server._impl_get_example(None, "Top Customers")
    assert '"name": "Top Customers"' in out


# ---------------------------------------------------------------------------
# v2.4.0 — OBSQL & reference tools
# ---------------------------------------------------------------------------


def test_get_obsql_reference(mock_api: respx.MockRouter):
    """get_obsql_reference fetches and caches the OBSQL grammar reference."""
    mock_api.get("/v1/reference/obsql").mock(
        return_value=httpx.Response(
            200,
            json={"reference": "# OBSQL\n\nSELECT … FROM <model>"},
        )
    )
    out = server.get_obsql_reference()
    assert "OBSQL" in out
    assert "SELECT" in out
    # Second call should hit the cache (only one HTTP request)
    server.get_obsql_reference()
    assert mock_api.routes[0].call_count == 1


def test_list_references(mock_api: respx.MockRouter):
    """list_references renders the reference index from /v1/reference."""
    mock_api.get("/v1/reference").mock(
        return_value=httpx.Response(
            200,
            json={
                "references": [
                    {
                        "name": "obml",
                        "kind": "markdown",
                        "description": "OBML reference",
                        "path": "/v1/reference/obml",
                    },
                    {
                        "name": "obsql",
                        "kind": "markdown",
                        "description": "OBSQL grammar",
                        "path": "/v1/reference/obsql",
                    },
                    {
                        "name": "query-schema",
                        "kind": "json-schema",
                        "description": "QueryObject schema",
                        "path": "/v1/reference/schemas/query",
                    },
                ]
            },
        )
    )
    out = server.list_references()
    assert "obml" in out
    assert "obsql" in out
    assert "query-schema" in out
    assert "/v1/reference/schemas/query" in out


def test_get_json_schema_query(mock_api: respx.MockRouter):
    """get_json_schema fetches a published JSON Schema by name."""
    mock_api.get("/v1/reference/schemas/query").mock(
        return_value=httpx.Response(
            200,
            json={"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"},
            headers={"Content-Type": "application/schema+json"},
        )
    )
    out = server.get_json_schema("query")
    assert "$schema" in out
    assert "object" in out


def test_compile_obsql_single_model_mode(mock_api: respx.MockRouter):
    """compile_obsql uses the /v1/query/semantic-ql/compile shortcut endpoint."""
    server._single_model_mode = True
    mock_api.post("/v1/query/semantic-ql/compile").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT country, SUM(amount) AS revenue FROM orders GROUP BY country",
                "dialect": "postgres",
                "query": {
                    "select": {"dimensions": ["Country"], "measures": ["Revenue"]},
                },
                "resolved": {
                    "fact_tables": ["orders"],
                    "dimensions": ["Country"],
                    "measures": ["Revenue"],
                },
                "warnings": [],
                "sql_valid": True,
                "physical_tables": ["ob.public.orders"],
            },
        )
    )
    out = server._impl_compile_obsql(
        None,
        "SELECT Country, Revenue FROM orders",
        "postgres",
    )
    assert "SELECT country" in out
    assert "Translated QueryObject" in out
    assert "ob.public.orders" in out


def test_compile_obsql_multi_model_mode(mock_api: respx.MockRouter):
    """compile_obsql uses the session-scoped endpoint when model_id is set."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/query/semantic-ql/compile").mock(
        return_value=httpx.Response(
            200,
            json={
                "sql": "SELECT 1",
                "dialect": "duckdb",
                "query": {},
                "resolved": {"fact_tables": [], "dimensions": [], "measures": []},
                "warnings": [],
                "sql_valid": True,
            },
        )
    )
    out = server._impl_compile_obsql("m001", "SELECT 1", None)
    assert "SELECT 1" in out
    assert "-- Dialect: duckdb" in out


def test_compile_obsql_rejects_empty_sql():
    """compile_obsql refuses empty SQL strings without a round trip."""
    with pytest.raises(server.ToolError, match="non-empty OBSQL"):
        server._impl_compile_obsql(None, "   ", None)


def test_execute_obsql_single_model_mode(mock_api: respx.MockRouter):
    """execute_obsql uses the /v1/query/semantic-ql shortcut endpoint."""
    server._single_model_mode = True
    mock_api.post("/v1/query/semantic-ql").mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [{"Country": "US", "Revenue": 100}],
                "schema": [
                    {"name": "Country", "type": "string"},
                    {"name": "Revenue", "type": "float"},
                ],
                "sql": "SELECT country, SUM(amount) AS revenue FROM orders GROUP BY country",
                "dialect": "postgres",
                "cached": False,
            },
        )
    )
    out = server._impl_execute_obsql(
        None,
        "SELECT Country, Revenue FROM orders",
        "postgres",
    )
    assert '"Country": "US"' in out
    assert '"Revenue": 100' in out


def test_execute_obsql_tsv_passthrough(mock_api: respx.MockRouter):
    """execute_obsql returns raw body text when output_format is tsv."""
    server._single_model_mode = True
    mock_api.post("/v1/query/semantic-ql").mock(
        return_value=httpx.Response(
            200,
            text="Country\tRevenue\nUS\t100\n",
            headers={"Content-Type": "text/tab-separated-values"},
        )
    )
    out = server._impl_execute_obsql(
        None,
        "SELECT Country, Revenue FROM orders",
        None,
        output_format="tsv",
    )
    assert out.startswith("Country\tRevenue")
    assert "US\t100" in out
