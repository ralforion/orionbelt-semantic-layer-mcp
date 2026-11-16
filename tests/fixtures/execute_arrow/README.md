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

`typed` is one DuckDB table `t.typed`, defaultTimezone `Europe/Zagreb` with
`overrideDatabaseTimezone: true`, three rows covering: `INTEGER`, `VARCHAR`
(non-ASCII, tab, quotes, NULL), `DOUBLE` (3.5, NaN, Infinity),
`DECIMAL(18,2)`, `DATE`, `TIMESTAMP` (with microseconds), `TIMESTAMPTZ`,
`TIME`, `INTERVAL` (months, days + hours), `BLOB` (bytes, empty), `BOOLEAN`.

Re-record when the API changes how it serializes a cell, its JSON response's
fields, or the frame.
