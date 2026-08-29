#!/usr/bin/env bash
#
# Regenerate LICENSES-THIRD-PARTY.txt for the *shipped* runtime dependency set.
#
# The Docker image generates this file at build time (see Dockerfile), so the
# copy inside the image is always in step with uv.lock and nothing can go stale.
# This script exists for the other case: inspecting or auditing the same set
# locally, or producing a copy to hand to a compliance reviewer.
#
# It resolves for the *image's* target (Linux / CPython 3.14), not for the host.
# That matters: a host-resolved run on macOS omits jeepney and SecretStorage,
# which the image does ship. Dependencies are installed into a throwaway
# `--target` tree, which the generator scans directly, so no interpreter for the
# target platform is needed. Development dependencies (pytest, ruff, vulture)
# are excluded via `uv export --no-dev` — they are never distributed.
#
# The image itself remains the ground truth. To read the file it actually
# carries:
#
#     docker run --rm --entrypoint cat \
#         ralforion/orionbelt-semantic-layer-mcp:latest /app/LICENSES-THIRD-PARTY.txt
#
# Usage: ./scripts/gen-third-party-licenses.sh [output-file]
#
#   PYTHON_VERSION   target interpreter  (default: 3.14, matching the Dockerfile)
#   PYTHON_PLATFORM  target platform     (default: x86_64-unknown-linux-gnu)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT="${1:-$REPO_ROOT/LICENSES-THIRD-PARTY.txt}"

# Keep in step with the `FROM python:<ver>-slim` lines in the Dockerfile.
PYTHON_VERSION="${PYTHON_VERSION:-3.14}"
PYTHON_PLATFORM="${PYTHON_PLATFORM:-x86_64-unknown-linux-gnu}"

command -v uv >/dev/null 2>&1 || { echo "error: uv is not installed" >&2; exit 1; }

DOCKERFILE_PYTHON="$(sed -n 's/^FROM python:\([0-9.]*\)-slim.*/\1/p' "$REPO_ROOT/Dockerfile" | head -1)"
if [[ -n "$DOCKERFILE_PYTHON" && "$DOCKERFILE_PYTHON" != "$PYTHON_VERSION" ]]; then
    echo "warning: Dockerfile targets Python $DOCKERFILE_PYTHON but this run targets" \
         "$PYTHON_VERSION; set PYTHON_VERSION=$DOCKERFILE_PYTHON to match the image" >&2
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

VERSION="$(grep -m1 '^version = ' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"

# The export keeps environment markers on each line (`; sys_platform == 'linux'`);
# `uv pip install` below evaluates them against the target, not against the host.
echo "==> Exporting locked runtime dependencies"
uv export --directory "$REPO_ROOT" \
    --no-dev --no-hashes --no-emit-project --frozen \
    --format requirements.txt -o "$WORKDIR/requirements.txt" >/dev/null

echo "==> Installing for $PYTHON_PLATFORM / CPython $PYTHON_VERSION"
uv pip install --target "$WORKDIR/site-packages" \
    --python-version "$PYTHON_VERSION" --python-platform "$PYTHON_PLATFORM" \
    --no-deps --no-compile --quiet -r "$WORKDIR/requirements.txt"

echo "==> Collecting license texts"
python3 "$REPO_ROOT/scripts/gen_third_party_licenses.py" \
    --path "$WORKDIR/site-packages" \
    --output "$OUTPUT" \
    --project-version "$VERSION" \
    --platform-note "$PYTHON_PLATFORM / CPython $PYTHON_VERSION (the Docker image target)"
