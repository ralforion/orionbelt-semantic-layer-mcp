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

`typed` is DuckDB 2.0 with defaultTimezone `Europe/Zagreb` and
`overrideDatabaseTimezone: true`. Table `t.typed` has three rows covering: `INTEGER`, `VARCHAR`
(non-ASCII, tab, quotes, NULL), `DOUBLE` (3.5, NaN, Infinity),
`DECIMAL(18,2)`, `DATE`, `TIMESTAMP` (with microseconds), `TIMESTAMPTZ`,
`TIME`, `INTERVAL` (months, days + hours), `BLOB` (bytes, empty), `BOOLEAN`.
Table `t.nanos` has `TIMESTAMP_NS` (Arrow `timestamp[ns]`): sub-microsecond
digits with and without microseconds, one before 1970, one whole microsecond, NULL.

The API's JSON for nanosecond values needs pandas in the API's environment
(`pandas.Timestamp`); `nanos_raw` was recorded from one that has it. Its default
image (`flight-duckdb-only`) does not, and there `format=json` fails on such a
value while the Arrow frame still carries it.

Re-record when the API changes how it serializes a cell, its JSON response's
fields, or the frame.
