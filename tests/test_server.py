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

    result = server.load_model("version: 1.0\n...")
    assert "m001" in result
    assert "data objects: 2" in result
    assert "dimensions:   3" in result
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

    result = server.load_model("version: 1.0\n...")
    assert "warnings:" in result
    assert "SQL validation warning" in result


# ---------------------------------------------------------------------------
# validate_model
# ---------------------------------------------------------------------------


def test_validate_model_valid(mock_api: respx.MockRouter):
    """validate_model returns 'valid' message when model is valid."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/validate").mock(
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
    mock_api.get("/v1/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(
            200,
            json={
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
            },
        )
    )

    result = server.describe_model("m001")
    assert "DATA OBJECTS:" in result
    assert "Orders" in result
    assert "DIMENSIONS:" in result
    assert "Country" in result
    assert "synonyms: nation, region" in result
    assert "MEASURES:" in result
    assert "Total Revenue" in result
    assert "synonyms: sales, income" in result


# ---------------------------------------------------------------------------
# compile_query
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

    result = server.compile_query(
        model_id="m001",
        dialect="snowflake",
        query_json='{"select":{"dimensions":["Country"],"measures":["Revenue"]}}',
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
                    "cfl_legs": 0,
                },
            },
        )
    )

    result = server.compile_query(
        model_id="m001",
        dimensions=["Country"],
        measures=["Revenue"],
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

    result = server.compile_query(
        model_id="m001",
        dimensions=["Country"],
        measures=["Revenue"],
    )
    assert "WARNING: Generated SQL may not be valid" in result


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
    mock_api.get("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server.list_models()
    assert "No models loaded" in result


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

    result = server.list_models()
    assert "m001" in result
    assert "2 objects" in result


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
# remove_model
# ---------------------------------------------------------------------------


def test_remove_model(mock_api: respx.MockRouter):
    """remove_model sends DELETE to the API."""
    _mock_create_session(mock_api)
    mock_api.delete("/v1/sessions/test-session-1/models/m001").mock(
        return_value=httpx.Response(204)
    )

    result = server.remove_model("m001")
    assert "m001" in result
    assert "removed" in result


# ---------------------------------------------------------------------------
# get_model_schema
# ---------------------------------------------------------------------------


def test_get_model_schema(mock_api: respx.MockRouter):
    """get_model_schema returns JSON model structure."""
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

    result = server.get_model_schema("m001")
    assert '"model_id": "m001"' in result
    assert '"Orders"' in result


# ---------------------------------------------------------------------------
# list_dimensions / get_dimension
# ---------------------------------------------------------------------------


def test_list_dimensions(mock_api: respx.MockRouter):
    """list_dimensions formats dimension list."""
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

    result = server.list_dimensions("m001")
    assert "Country" in result
    assert "Customers" in result
    assert "synonyms: nation" in result


def test_list_dimensions_empty(mock_api: respx.MockRouter):
    """list_dimensions handles empty list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/dimensions").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server.list_dimensions("m001")
    assert "No dimensions" in result


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

    result = server.get_dimension("m001", "Country")
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

    result = server.get_dimension("m001", "Customer Country")
    assert '"Customer Country"' in result


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

    result = server.list_measures("m001")
    assert "Total Revenue" in result
    assert "sum" in result
    assert "synonyms: sales" in result


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

    result = server.get_measure("m001", "Total Revenue")
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

    result = server.list_metrics("m001")
    assert "Profit Margin" in result
    assert "components: Profit, Revenue" in result


def test_list_metrics_empty(mock_api: respx.MockRouter):
    """list_metrics handles empty list."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/metrics").mock(
        return_value=httpx.Response(200, json=[])
    )

    result = server.list_metrics("m001")
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

    result = server.get_metric("m001", "Profit Margin")
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

    result = server.explain_artefact("m001", "Total Revenue")
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
                    {"type": "dimension", "name": "Country", "match_field": "name", "score": 1.0},
                    {"type": "measure", "name": "Revenue", "match_field": "synonym", "score": 1.0},
                ]
            },
        )
    )

    result = server.find_artefacts("m001", "rev")
    assert "Country" in result
    assert "Revenue" in result
    assert "matched on synonym" in result


def test_find_artefacts_no_results(mock_api: respx.MockRouter):
    """find_artefacts handles no results."""
    _mock_create_session(mock_api)
    mock_api.post("/v1/sessions/test-session-1/models/m001/find").mock(
        return_value=httpx.Response(200, json={"results": []})
    )

    result = server.find_artefacts("m001", "xyz")
    assert "No artefacts found" in result


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

    result = server.get_join_graph("m001")
    assert "Orders" in result
    assert "Customers" in result
    assert "many-to-one" in result
    assert "Customer ID" in result


def test_get_join_graph_no_edges(mock_api: respx.MockRouter):
    """get_join_graph handles models with no joins."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/join-graph").mock(
        return_value=httpx.Response(
            200, json={"nodes": ["Orders"], "edges": []}
        )
    )

    result = server.get_join_graph("m001")
    assert "Orders" in result
    assert "No joins defined" in result


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
            },
        )
    )

    result = server.get_settings()
    assert "Single-model mode: False" in result
    assert "Session TTL: 1800s" in result


def test_get_settings_single_model(mock_api: respx.MockRouter):
    """get_settings shows pre-loaded model info in single-model mode."""
    mock_api.get("/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "single_model_mode": True,
                "model_yaml": "version: 1.0\ndataObjects: ...",
                "session_ttl_seconds": 3600,
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
    """get_model_diagram returns Mermaid ER diagram."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models/m001/diagram/er").mock(
        return_value=httpx.Response(
            200,
            json={"mermaid": "erDiagram\n  Orders ||--o{ Customers : joins"},
        )
    )

    result = server.get_model_diagram("m001")
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


# ---------------------------------------------------------------------------
# Session auto-creation & caching
# ---------------------------------------------------------------------------


def test_session_created_once(mock_api: respx.MockRouter):
    """Session is created only once and reused for subsequent calls."""
    _mock_create_session(mock_api)
    mock_api.get("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(200, json=[])
    )

    server.list_models()
    server.list_models()

    # POST /v1/sessions should have been called exactly once
    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/v1/sessions"
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
    mock_api.get("/v1/sessions/session-old/models").mock(
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
    mock_api.get("/v1/sessions/session-new/models").mock(
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
    mock_api.get("/v1/sessions/session-old/models/no-such-model").mock(
        return_value=httpx.Response(404, json={"detail": "Model not found"})
    )

    with pytest.raises(ToolError, match="API error.*404"):
        server.describe_model("no-such-model")

    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/v1/sessions"
    ]
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
        server.describe_model("missing")

    session_calls = [
        call for call in mock_api.calls if call.request.url.path == "/v1/sessions"
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
    mock_api.post("/v1/sessions/test-session-1/models").mock(
        return_value=httpx.Response(
            422,
            json={"detail": "Invalid OBML model: parsing or validation failed"},
        )
    )

    with pytest.raises(ToolError, match="API error.*422"):
        server.load_model("bad yaml")


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
# Prompts & resource
# ---------------------------------------------------------------------------


def test_write_obml_model_prompt():
    """write_obml_model prompt returns OBML syntax reference."""
    result = server._WRITE_OBML_MODEL_TEXT
    assert "OBML" in result
    assert "dataObjects" in result


def test_write_query_prompt():
    """write_query prompt returns query compilation guide."""
    result = server._WRITE_QUERY_TEXT
    assert "Simple Mode" in result
    assert "Full Mode" in result


def test_debug_validation_prompt():
    """debug_validation prompt returns error codes."""
    result = server._DEBUG_VALIDATION_TEXT
    assert "YAML_PARSE_ERROR" in result
    assert "UNKNOWN_COLUMN" in result


def test_obml_reference_resource():
    """obml://reference resource returns the OBML reference."""
    result = server.obml_reference()
    assert "OBML" in result
    assert "dataObjects" in result
