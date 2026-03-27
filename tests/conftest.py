"""Shared test configuration."""

import os

# Set API_BASE_URL before server module is imported (it has no default).
os.environ.setdefault("API_BASE_URL", "http://localhost:19999")
