#!/usr/bin/env bash
#
# Regenerate LICENSES-THIRD-PARTY.txt from the locked *runtime* dependency set.
#
# The Docker image generates this file at build time (see Dockerfile), so the
# shipped copy is always in step with uv.lock and nothing can go stale. This
# script exists for the other case: inspecting or auditing the same set locally,
# or producing a copy to hand to a compliance reviewer.
#
# It builds a throwaway venv from `uv export --no-dev` so the development
# dependencies (pytest, ruff, vulture, ...) — which are never distributed — stay
# out of the report.
#
# Usage: ./scripts/gen-third-party-licenses.sh [output-file]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$REPO_ROOT/LICENSES-THIRD-PARTY.txt}"

command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

VERSION="$(grep -m1 '^version = ' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"

echo "==> Exporting locked runtime dependencies"
uv export --directory "$REPO_ROOT" \
    --no-dev --no-hashes --no-emit-project --frozen \
    --format requirements.txt -o "$WORKDIR/requirements.txt" >/dev/null

echo "==> Materializing a runtime-only environment"
uv venv --python 3.12 "$WORKDIR/venv" >/dev/null
uv pip install --python "$WORKDIR/venv/bin/python" -r "$WORKDIR/requirements.txt" >/dev/null

echo "==> Collecting license texts"
"$WORKDIR/venv/bin/python" "$REPO_ROOT/scripts/gen_third_party_licenses.py" \
    --output "$OUTPUT" --project-version "$VERSION"
