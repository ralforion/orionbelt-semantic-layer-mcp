# Recorded `format=arrow` execute frames

Each `<name>.arrow` is the body of a real `POST /v1/query/execute?format=arrow`
(OrionBelt Semantic Layer API 3.0.0, DuckDB, file result cache). `<name>.json` is the
body the same API returned for the same query without `format`. The
`execute_query` tests decode the frame and require the JSON, cell for cell, so
the conversion in `server.py` cannot drift from the API's own.

| Fixture | Model | Query |
|---|---|---|
| `typed_raw` | `typed` (below) | `fields`: every `Typed` column but `Grp`, ordered by `Id` |
| `typed_agg` | `typed` | `Group` × `Total Dec` (sum), `Avg Dbl` (avg) |
| `typed_empty` | `typed` | `fields`: `Dec`, `Ts`, where `Id = 99` (no rows) |
| `commerce_agg` | the API repo's `examples/orionbelt_1_commerce.yaml` | `Country Name` × `Total Sales`, `Distinct Clients`, top 25 |
| `nanos_raw` | `typed` | `fields`: `Nanos.Id`, `Nanos.Tsns`, ordered by `Id` |
| `nested_raw` | `typed` | `fields`: every `Nested` column, ordered by `Id` |
| `union_raw` | `typed` | `fields`: every `Unions` column, ordered by `Id` |

`typed` is DuckDB 2.0 with defaultTimezone `Europe/Zagreb` and
`overrideDatabaseTimezone: true`. Table `t.typed` has three rows covering: `INTEGER`, `VARCHAR`
(non-ASCII, tab, quotes, NULL), `DOUBLE` (3.5, NaN, Infinity),
`DECIMAL(18,2)`, `DATE`, `TIMESTAMP` (with microseconds), `TIMESTAMPTZ`,
`TIME`, `INTERVAL` (months, days + hours), `BLOB` (bytes, empty), `BOOLEAN`.
Table `t.nanos` has `TIMESTAMP_NS` (Arrow `timestamp[ns]`): sub-microsecond
digits with and without microseconds, one before 1970, one whole microsecond, NULL.
Table `t.nested` has a `LIST(TIMESTAMP_NS)` (NULL element, pre-1970), a
`STRUCT(at TIMESTAMP_NS, n INTEGER)`, a `LIST(TIMESTAMP)`, a
`STRUCT(at TIMESTAMPTZ, d DECIMAL(5,2))` and a `LIST(TIME_NS)`, plus a row
with an empty list and NULLs. A nested cell reaches the API's JSON as
Python's `str()` of the value. Table `t.unions` has DuckDB `UNION` columns
(Arrow sparse unions) with a `TIMESTAMP_NS` member: alone with an `INTEGER`,
beside `TIMESTAMP` and `VARCHAR`, beside `INTERVAL` and `DECIMAL`, in a `LIST`
and in a `STRUCT`, plus NULLs. A union cell is its member's value, serialized
as a cell of its own: no column-level step, so the `INTERVAL` member reads
`MonthDayNano(...)` where an interval column reads as a `timedelta`.

The API's JSON for nanosecond values needs pandas in the API's environment
(`pandas.Timestamp`); `nanos_raw`, `nested_raw` and `union_raw` were recorded
from one that has it. Its default image (`flight-duckdb-only`) does not, and there
`format=json` fails on such a value while the Arrow frame still carries it.
DuckDB writes no dictionary-encoded timestamps, dense unions or list views;
`test_decode_arrow_result_dictionaries_unions_list_views` covers those with
pyarrow-built arrays, its expected cells taken from the API's `arrow_to_rows`
with pandas.

Re-record when the API changes how it serializes a cell, its JSON response's
fields, or the frame.
