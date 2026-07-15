# --- Build stage: install dependencies ---
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install dependencies first (cached layer — only reruns when pyproject.toml/uv.lock change)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --no-install-project --frozen

# Copy source and install the project itself
COPY server.py ./
RUN uv sync --no-dev --no-editable --frozen

# --- Runtime stage: minimal image ---
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Create non-root user
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

# Copy installed virtualenv and source from builder
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/server.py ./
ENV PATH="/app/.venv/bin:$PATH"

USER app

# Cloud Run injects PORT (default 9000 for local Docker).
# MCP_SERVER_PORT is a fallback; effective_port prefers PORT when set.
ENV PORT=9000 \
    MCP_TRANSPORT=http \
    MCP_SERVER_HOST=0.0.0.0 \
    MCP_SERVER_PORT=9000 \
    LOG_LEVEL=INFO \
    LOG_FORMAT=cloudrun

EXPOSE ${PORT}

# Local Docker / Compose health probe (Cloud Run uses its own startup/liveness probes).
# FastMCP serves /mcp on GET — the trailing-slash variant returns 307. urllib's
# default redirect handler follows 307 only for safe methods (GET is fine), so a
# bare connect to confirm the listener is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, socket, sys; s=socket.create_connection(('localhost', int(os.environ['PORT'])), timeout=3); s.close()" || exit 1

CMD ["python", "server.py"]
