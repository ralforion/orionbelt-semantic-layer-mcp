# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## What this is

A **thin** MCP server that wraps the OrionBelt Semantic Layer (OBSL) REST API. It contains no semantic engine and no business logic — every tool delegates over HTTP to the API and formats the response. All meaning lives in the API repo, which is the source of truth for endpoints, schemas, and behavior:

- **API repo:** `../orionbelt-semantic-layer` (GitHub: `ralforion/orionbelt-semantic-layer`)

The entire implementation is a single module, `server.py` (~3k lines). Tests live in `tests/test_server.py`. The hatch build ships only `server.py`.

## Commands

```bash
uv run pytest                              # full suite
uv run pytest tests/test_server.py::NAME   # single test (NAME = test function or Class::method)
uv run pytest -q -k SUBSTRING              # filter by name substring
uv run ruff check .                        # lint  (must be clean before done)
uv run ruff format server.py tests/        # format
./scripts/setup-hooks.sh                   # install pre-commit hooks (runs ruff)
uv run server.py                           # run locally (reads .env; see .env.example)
```

Tests mock the API with `respx` (httpx interception) and `pytest-asyncio` in auto mode — they never hit a live API. When changing an endpoint call, update its respx mock in the same test.

## Architecture (the non-obvious parts)

**Delegation layer.** Tools call thin request helpers, never httpx directly:
`_api_request` (base, with one-shot retry on session expiry) → `_session_request` (multi-model, prefixes `/v1/sessions/{id}/models/{mid}`) and `_shortcut_request` (single-model, top-level `/v1/...`). `_parse_json` / `_raise_api_error` normalize responses and surface API error detail as `ToolError`.

**Query results arrive as Arrow.** `execute_query` requests `format=arrow` (raw JSON output only; `format_values` and TSV stay on the API) and `_decode_arrow_result` rebuilds the API's JSON response, which moves row serialization off the shared API. The cell conversion mirrors the API's `db_executor.arrow_to_rows` + `_serialize_value` + Pydantic JSON encoding. `tests/fixtures/execute_arrow/` holds frames recorded from a real API with its JSON for the same query: re-record them (see the README there) when the API changes how it serializes cells, its response fields, or the frame.

**Two API modes, detected once at startup** via `GET /v1/settings` (`_detect_api_mode`), then frozen:
- **single-model** — the API has one pre-loaded model; tools hit shortcut routes (`/v1/schema`, `/v1/query/execute`, …) and `model_id` is implicit.
- **multi-model** — tools operate on session-scoped routes and take a `model_id`; sessions are auto-created (`_ensure_session`) and deleted on shutdown.

`_register_model_tools` registers the correct signatures for the detected mode (same tool names, different params). This is why `model_id` is optional on most tools — it's ignored/resolved in single-model mode.

**Design-time ↔ run-time phase switching** (see the long comment block near the top of `server.py`, `PHASE_DESIGN`/`PHASE_RUN`). The visible `tools/list` *swaps* (not adds) between two sets based on whether any model is loaded:
- bucket 1 `_ALWAYS_TOOLS` — listed in both phases (lifecycle verbs + self-contained `run_batch` + `get_json_schema`)
- bucket 2 `_DESIGN_TOOLS` — only when no model is loaded (authoring/reference)
- bucket 3 `_RUN_TIME_TOOLS` — only once a model is loaded (query/introspect/execute)

Phase is derived from explicit loaded-model state (`_loaded_model_ids`, flipped by `_mark_model_loaded`/`_mark_model_removed`), never from transport state, to stay stateless-clean. `PhaseMiddleware` filters `tools/list` and rejects run-only verbs called in the design phase with a structured guard error steering the host to `load_model` + re-list. Single-model mode is permanently in the run phase.

**Capability gating is orthogonal to phase.** A tool is visible only if its phase is active *and* its capability is enabled. Currently only `execute_query` is gated (by `query_execute`, resolved from API config at startup). To add another: register a resolver in `_CAPABILITY_RESOLVERS` and map the tool in `_TOOL_CAPABILITY`.

**Transport-dependent init.** The middleware stack (`ApiVersionGateMiddleware` → `LazyInitMiddleware` → `PhaseMiddleware`) is installed at import, so `main()` and the exported `server.py:mcp` (Prefect Horizon, `fastmcp run`, in-memory clients) serve through the same one.
- **stdio via `main()`** — eager: health check + mode detection + tool registration up front, fail-fast.
- **HTTP/SSE, and `server.py:mcp` on any transport** — `LazyInitMiddleware` defers mode detection and registration to the first request after the version gate, so the container starts instantly (Cloud Run cold starts). `PORT` env (injected by Cloud Run) overrides `MCP_SERVER_PORT`.
- The middleware's API calls (the version check, mode detection) run in a worker thread shared by the requests waiting on them (`_SharedRun`), never on the event loop.

**Auth modes (`MCP_AUTH_MODE`, `_auth_mode`).**
- `none`, or `api_key` (`API_KEY` as a static default header; it may be a person's BI token).
- `oidc_client_credentials`: one cached IdP token.
- `oidc_user`: over HTTP, a `_ResourceAuthProvider` (FastMCP `RemoteAuthProvider` + `_OidcUserVerifier`) built **at import**, so the exported `server.py:mcp` is never served open. It checks tokens issued for this server.
- `_ObslAuth` (an `httpx.Auth` on the API client) puts the oidc modes' bearer token on each request. In the user mode that is the current caller's token, exchanged via Entra OBO or RFC 8693 and cached per user. The incoming token is never forwarded; a test asserts it.
- `/health` is marked public (`_do_request(public=True)`), so the version gate needs no sign-in.
- The API session and loaded models live in `_Identity` objects keyed by `_current_identity_key()`: one `_SERVICE_IDENTITY` in the service modes, issuer + subject in `oidc_user`. Read them via `_identity()` / `_identity_or_none()`, never a global.
- Error mapping for the API's auth and sign-in codes is in `_raise_api_error`. The sign-in codes are not `AUTH_*`, so they are matched first.

**Version compatibility gate.** The server serves only an API of its own `major.minor` (MCP 3.2 ↔ API 3.2.x; patches may differ), checked against the API's `/health` (`_verify_api`). On stdio via `main()` it checks at startup and **exits** on a mismatch. Otherwise (HTTP, which starts before it reaches the API, and the exported `server.py:mcp`), `ApiVersionGateMiddleware` refuses every request but the handshake with an MCP error saying why, until the API matches; on both transports a request checks again once the last pass is over a minute old, so an API redeployed to another minor under a running server is refused too. This is why every API minor bump *requires* a matching MCP release even when no tool changes.

## Version tracking & release discipline

This repo's version mirrors the OBSL API version. When the API bumps, adapt here:

1. Diff the API repo's REST surface (`src/orionbelt/api/routers/`, `schemas.py`, `models/query.py`, the OBML schema) — most API changes (pgwire, UI, demo, compiler internals) do **not** touch the MCP surface. Only add/change tools when a wrapped endpoint, request/response shape, or query model actually changes.
2. A version bump must be **complete**: `pyproject.toml`, then `uv lock` (updates `uv.lock`), README badges (the version badge must be **first** in the badge row), the matching `OrionBelt Semantic Layer X.Y` badge, a `CHANGELOG.md` entry, and tool counts in README if tools changed.
3. **Never commit to `main`.** Use a `chore/`, `fix/`, or `feature/` branch → PR → **squash** merge.
4. A **full release** is `./scripts/release.sh` (3 steps: squash-merge PR → GitHub release → PyPI publish). The multi-arch Docker Hub image (`ralforion/orionbelt-semantic-layer-mcp:<ver>` + `:latest`) is built by `.github/workflows/docker-publish.yml`, triggered automatically when step 2 pushes the `v*` tag — it is no longer pushed by the script. Don't replicate the script by hand. It runs its own ruff/format/test/version/changelog pre-flight and refuses to run on `main` or with a dirty tree.

Cloud Run deployment is **not** in this repo — the MCP service is rolled by the API repo's `scripts-infra/deploy-mcp-gcloud.sh`. That directory is gitignored there, so it will not turn up in a clone or a repo-wide grep; ask rather than conclude it is missing. The API and UI have their own scripts beside it (`deploy-gcloud.sh`, `deploy-ui-gcloud.sh`) — three separate rollouts, not one bundled command.

The MCP service runs `MCP_TRANSPORT=http` bound to `0.0.0.0` with `--allow-unauthenticated`, gated by `--ingress internal-and-cloud-load-balancing` plus Cloud Armor on the load balancer. Access control is therefore the LB, not IAM — which is why the two flags look alarming only when read apart.

## Conventions

- Ruff: line-length 100, py312, rule set `E,F,I,N,UP,B,A,SIM`. Resolve all findings before considering work done.
- The `mcp-name:` HTML comment on README line 1 is the canonical MCP-registry namespace (`io.github.ralforion/...`) — changing it is a registry rename, not cosmetic.
- Every `uses:` in `.github/workflows` is pinned to a commit SHA with a `# vX.Y.Z`
  comment naming an exact patch release, and `scripts/check-action-pins.sh`
  (the `pins` CI job) fails if the two disagree. Bumping an action means editing
  the SHA and the comment together; resolve the SHA with
  `git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>'^{}'`. A new
  action's owner must be added to `ALLOWED_OWNERS` in that script deliberately.
- Code is reviewed externally with OpenAI Codex.
