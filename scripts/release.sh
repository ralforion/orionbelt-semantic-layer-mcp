#!/usr/bin/env bash
# Full release pipeline for OrionBelt Semantic Layer MCP.
#
# Usage: ./scripts/release.sh [--yes|-y] [VERSION]
#
# If VERSION is not provided, reads it from pyproject.toml.
# If --yes (or env RELEASE_YES=1) is set, every confirmation auto-accepts —
# the pipeline runs straight through.
#
# Steps (each with confirmation prompt):
#   1. Create & merge PR (fix/ or feature/ branch → main, squash)
#   2. Create GitHub release with changelog
#   3. Publish to PyPI
#   4. Push Docker image to Docker Hub (multi-arch)
#
# Cloud Run deployment is intentionally not part of this script:
# the MCP service is rebuilt and rolled by the OBSL repo's
# scripts/deploy-gcloud.sh as part of the bundled API+UI+MCP rollout.
#
# Prerequisites:
#   - gh CLI authenticated
#   - DOCKERHUB_RALFORION_PAT set (for Docker Hub)
#   - uv, docker, twine available
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
REPO="ralforion/orionbelt-semantic-layer-mcp"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

step() { echo -e "\n${CYAN}═══ $1 ═══${NC}"; }
ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }

AUTO_YES="${RELEASE_YES:-0}"

confirm() {
    local prompt="${1:-Continue?}"
    if [[ "$AUTO_YES" == "1" ]]; then
        echo -e "${YELLOW}${prompt} [y/N]${NC} ${GREEN}y${NC} (auto)"
        return 0
    fi
    echo -en "${YELLOW}${prompt} [y/N] ${NC}"
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Skipped."; return 1; }
}

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
VERSION=""
for arg in "$@"; do
    case "$arg" in
        -y|--yes) AUTO_YES=1 ;;
        -h|--help)
            sed -n '2,25p' "$0"
            exit 0
            ;;
        *) VERSION="$arg" ;;
    esac
done

if [[ -z "$VERSION" ]]; then
    VERSION=$(grep '^version' "$REPO_ROOT/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')
fi
if [[ "$AUTO_YES" == "1" ]]; then
    echo -e "${YELLOW}--yes mode: every confirmation auto-accepts.${NC}"
fi
echo -e "Release version: ${GREEN}v${VERSION}${NC}"

BRANCH=$(git branch --show-current)
echo -e "Current branch:  ${GREEN}${BRANCH}${NC}"

if [[ "$BRANCH" == "main" ]]; then
    fail "You are on main. Switch to a fix/ or feature/ branch first."
fi

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------
step "Pre-flight checks"

command -v gh     >/dev/null || fail "gh CLI not found"
command -v uv     >/dev/null || fail "uv not found"
command -v docker >/dev/null || fail "docker not found"

# Sync tags from origin before any tag-dependent logic. `git describe` only
# sees LOCAL tags, so a stale clone (a tag pushed by gh release create in a
# prior run but never fetched here) makes the changelog range silently span
# extra versions. Fetch first so PREV_TAG below is the true previous release.
echo "Syncing tags from origin..."
git fetch --tags --force --quiet origin || warn "Could not fetch tags from origin"

if ! git diff --quiet; then
    fail "Uncommitted changes. Commit or stash first."
fi

echo "Checking code formatting..."
uv run ruff format --check server.py >/dev/null 2>&1 || fail "Code formatting issues detected. Run 'ruff format server.py' to fix."
ok "Code formatting correct"

echo "Checking linting..."
uv run ruff check server.py || fail "Linting issues detected. Fix them before releasing."
ok "Linting checks pass"

echo "Running tests..."
uv run pytest --tb=short -q || fail "Tests failed"
ok "All tests pass"

echo "Checking CI status..."
# Get the latest CI run status for current branch
CI_STATUS=$(gh run list --branch "$BRANCH" --limit 1 --json conclusion --jq '.[0].conclusion // "pending"' 2>/dev/null || echo "unknown")
if [[ "$CI_STATUS" != "success" ]]; then
    echo -e "${YELLOW}Warning: Latest CI run status is '$CI_STATUS' (not 'success')${NC}"
    echo "Check: https://github.com/$REPO/actions"
    confirm "Continue despite CI not being green?" || fail "Aborted due to CI status"
fi
ok "CI status checked"

echo "Checking version consistency..."
PYPROJECT_VER=$(grep '^version' "$REPO_ROOT/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')
[[ "$PYPROJECT_VER" == "$VERSION" ]] || fail "pyproject.toml version ($PYPROJECT_VER) != $VERSION"
ok "Version consistent (pyproject.toml)"

echo "Checking CHANGELOG..."
grep -q "## \[$VERSION\]" "$REPO_ROOT/CHANGELOG.md" \
    || fail "CHANGELOG.md missing [$VERSION] entry"
ok "CHANGELOG entry present"

# ---------------------------------------------------------------------------
# 1. Create & merge PR
# ---------------------------------------------------------------------------
step "1/4  Create & merge PR"

if ! git ls-remote --exit-code origin "$BRANCH" >/dev/null 2>&1; then
    echo "Pushing branch to origin..."
    git push -u origin "$BRANCH"
fi

EXISTING_PR=$(gh pr list --head "$BRANCH" --json number --jq '.[0].number // empty' 2>/dev/null || true)

if [[ -n "$EXISTING_PR" ]]; then
    echo "PR #${EXISTING_PR} already exists for branch ${BRANCH}"
    PR_URL=$(gh pr view "$EXISTING_PR" --json url --jq '.url')
else
    echo "Creating PR..."
    CHANGELOG=$(git log main..HEAD --oneline --no-merges)
    PR_URL=$(gh pr create \
        --title "Release v${VERSION}" \
        --body "$(cat <<EOF
## Summary
Release v${VERSION}

## Changes
\`\`\`
${CHANGELOG}
\`\`\`

## Release checklist
- [ ] Tests pass
- [ ] Version bumped in pyproject.toml + CHANGELOG.md
EOF
)" 2>&1 | tail -1)
    ok "PR created: $PR_URL"
fi

if confirm "Merge PR into main?"; then
    # Repo policy: merge commits disallowed — squash is the convention
    # (matches the OBSL repo).
    gh pr merge "$BRANCH" --squash --delete-branch
    git checkout main
    git pull origin main
    ok "PR merged (squash) and branch deleted"
    BRANCH="main"
else
    warn "PR not merged — remaining steps require main. Aborting."
    exit 0
fi

# ---------------------------------------------------------------------------
# 2. GitHub release
# ---------------------------------------------------------------------------
step "2/4  Create GitHub release"

TAG="v${VERSION}"
if git tag -l "$TAG" | grep -q "$TAG"; then
    warn "Tag $TAG already exists"
else
    if confirm "Create GitHub release $TAG?"; then
        # Newest tag reachable from HEAD that is NOT the tag we're about to
        # create — i.e. the genuine previous release. Excluding $TAG guards the
        # case where the release tag already exists locally (re-run/backfill).
        PREV_TAG=$(git tag --list --sort=-version:refname --merged HEAD \
            | grep -vFx "$TAG" | head -1)
        [[ -z "$PREV_TAG" ]] && PREV_TAG=$(git rev-list --max-parents=0 HEAD)
        # Sanity: the previous tag should be the immediately preceding version.
        # If it isn't (e.g. v2.9.0 when releasing v2.11.0), the notes range
        # would swallow an intermediate release — warn loudly rather than ship
        # a misleading changelog.
        echo "Previous release tag: ${PREV_TAG} (changelog range ${PREV_TAG}..HEAD)"
        confirm "Generate notes from ${PREV_TAG}..HEAD?" \
            || fail "Aborted: confirm the previous tag, then re-run."
        NOTES=$(git log "${PREV_TAG}"..HEAD --oneline --no-merges | head -20)
        # Build notes via printf into a temp file — avoids the bash heredoc-in-$()
        # apostrophe quirk that triggers "unexpected EOF while looking for `''".
        NOTES_FILE=$(mktemp)
        {
            printf '## Changes\n\n%s\n\n' "$NOTES"
            printf '**Full Changelog**: https://github.com/ralforion/orionbelt-semantic-layer-mcp/compare/%s...%s\n' \
                "$PREV_TAG" "$TAG"
        } >"$NOTES_FILE"
        gh release create "$TAG" \
            --title "v${VERSION}" \
            --notes-file "$NOTES_FILE"
        rm -f "$NOTES_FILE"
        ok "GitHub release $TAG created"
    fi
fi

# ---------------------------------------------------------------------------
# 3. Publish to PyPI
# ---------------------------------------------------------------------------
step "3/4  Publish to PyPI"

if confirm "Build and publish to PyPI?"; then
    rm -rf dist/
    uv build
    uv publish
    ok "Published to PyPI"

    # Smoke-test that PyPI actually serves the new version. PyPI's CDN
    # propagation is usually a few seconds but can take up to ~60s on a
    # cold cache, so retry for up to 2 minutes before giving up. Uses
    # ``uv run --no-project`` so the test ignores the local source tree
    # and only resolves what's on PyPI.
    echo "Smoke-testing pip install from PyPI..."
    SMOKE_OK=0
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        if uv run --no-project --with "orionbelt-semantic-layer-mcp==${VERSION}" -- \
                python -c "import server" \
                >/dev/null 2>&1; then
            SMOKE_OK=1
            break
        fi
        echo "  attempt $i/12: not yet available, retrying in 10s..."
        sleep 10
    done
    if [[ "$SMOKE_OK" == "1" ]]; then
        ok "Smoke test passed: pip install orionbelt-semantic-layer-mcp==${VERSION} resolves and imports cleanly"
    else
        warn "Smoke test failed after 2 minutes — verify manually at https://pypi.org/project/orionbelt-semantic-layer-mcp/${VERSION}/"
    fi
fi

# ---------------------------------------------------------------------------
# 4. Push Docker image to Docker Hub
# ---------------------------------------------------------------------------
step "4/4  Push Docker image to Docker Hub"

if confirm "Build and push MCP Docker image to Docker Hub?"; then
    DOCKER_USER="ralforion"
    if [[ -z "${DOCKERHUB_RALFORION_PAT:-}" ]]; then
        fail "DOCKERHUB_RALFORION_PAT not set"
    fi
    echo "Logging in to Docker Hub as $DOCKER_USER..."
    echo "$DOCKERHUB_RALFORION_PAT" | docker login -u "$DOCKER_USER" --password-stdin

    # Build from a clean archive of HEAD so uncommitted state isn't shipped.
    BUILD_CTX=$(mktemp -d)
    trap 'rm -rf "$BUILD_CTX"' EXIT
    git archive HEAD | tar -x -C "$BUILD_CTX"

    echo "Building and pushing $DOCKER_USER/orionbelt-semantic-layer-mcp:$VERSION ..."
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        --provenance=false \
        --sbom=false \
        -t "$DOCKER_USER/orionbelt-semantic-layer-mcp:$VERSION" \
        -t "$DOCKER_USER/orionbelt-semantic-layer-mcp:latest" \
        --push "$BUILD_CTX"
    ok "Docker Hub deployed"

    # Switch back to default Docker Hub account if configured.
    if [[ -n "${DOCKERHUB_DEFAULT_USER:-}" && -n "${DOCKERHUB_DEFAULT_PAT:-}" ]]; then
        echo ""
        echo "Switching back to $DOCKERHUB_DEFAULT_USER..."
        echo "$DOCKERHUB_DEFAULT_PAT" | docker login -u "$DOCKERHUB_DEFAULT_USER" --password-stdin
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo -e "${GREEN}  Release v${VERSION} complete!${NC}"
echo -e "${GREEN}═══════════════════════════════════════${NC}"
echo ""
echo "Verify:"
echo "  GitHub:    https://github.com/ralforion/orionbelt-semantic-layer-mcp/releases/tag/v${VERSION}"
echo "  PyPI:      https://pypi.org/project/orionbelt-semantic-layer-mcp/${VERSION}/"
echo "  Docker:    https://hub.docker.com/r/ralforion/orionbelt-semantic-layer-mcp"
