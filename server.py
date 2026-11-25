"""Thin MCP server for OrionBelt Semantic Layer.

Delegates all business logic to the OrionBelt Semantic Layer REST API via HTTP.
No embedded engine — pure API pass-through.

Supports two modes:

- **Multi-model mode** (default): LLM loads models via ``load_model``, gets a
  ``model_id``, and passes it to every tool.
- **Single-model mode**: API has a pre-loaded model.  Session/model management
  tools are hidden; discovery and query tools use shortcut endpoints that
  auto-resolve session and model.

Run via::

    uv run python server.py                        # stdio (default)
    MCP_TRANSPORT=http uv run python server.py     # streamable HTTP on port 9000

Entrypoint for Prefect Horizon: ``server.py:mcp``
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import gzip
import importlib.metadata
import ipaddress
import json
import logging
import math
import os
import re
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, NoReturn
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import INTERNAL_ERROR, McpError, ToolError
from fastmcp.prompts import Prompt as _BasePrompt
from fastmcp.server.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.utilities.auth import decode_jwt_header
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logger = logging.getLogger("orionbelt.mcp")


class Settings(BaseSettings):
    """Configuration loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    api_base_url: str
    # How this server authenticates to the API — see "Authentication" below.
    # Unset: api_key when API_KEY is set, none otherwise (the behaviour before
    # the setting existed).
    mcp_auth_mode: Literal["none", "api_key", "oidc_client_credentials", "oidc_user"] | None = None
    # api_key mode: a key from the API's API_KEYS, or a person's BI token
    # (obsl_tok_…, from OBSL's /tokens page), with which the API runs as them.
    api_key: str | None = None
    # Header the credential is sent in; must match the API's API_KEY_HEADER.
    api_key_header: str = "X-API-Key"
    # oidc_user: the public URL of this server's MCP endpoint, as users add it to
    # their MCP client. The resource its RFC 9728 metadata names and, unless
    # MCP_AUDIENCE says otherwise, the audience incoming tokens must carry.
    mcp_resource_url: str | None = None
    # oidc_user: the accepted ``aud`` values (comma-separated) when the IdP puts
    # something else there than the URL (Entra: the MCP app's client ID).
    mcp_audience: str | None = None
    # oidc_user: the scopes an MCP client asks the IdP for to call this server
    # (Entra: api://<MCP app>/<scope>), advertised in the metadata and the 401.
    mcp_scopes: str | None = None
    # Both oidc modes: the IdP. Its discovery document gives the signing keys and
    # the token endpoint (OIDC_TOKEN_ENDPOINT overrides the latter).
    oidc_issuer: str | None = None
    oidc_token_endpoint: str | None = None
    # This server's confidential client at the IdP, for the token requests.
    oidc_client_id: str | None = None
    oidc_client_secret: SecretStr | None = None
    oidc_client_secret_file: str | None = None
    # How the client authenticates at the token endpoint.
    oidc_client_auth: Literal["client_secret_post", "client_secret_basic"] = "client_secret_post"
    # oidc_user: how the user's token is exchanged. Unset: entra_obo for an
    # Entra issuer (login.microsoftonline.com), token_exchange (RFC 8693) else.
    oidc_exchange: Literal["entra_obo", "token_exchange"] | None = None
    # What to ask the IdP for, for OBSL: Entra <OBSL app ID URI>/.default; for
    # RFC 8693 IdPs (Okta, Keycloak) OBSL's audience and/or a scope.
    obsl_scope: str | None = None
    obsl_audience: str | None = None
    # Client-side TLS for the hop to the API, for an API that serves HTTPS
    # itself (API_TLS_* on the API, v2.29+) or a gateway in front of it.
    # API_CLIENT_CERT (+ API_CLIENT_KEY unless the key is in the same PEM)
    # presents a certificate, for mutual TLS (API_TLS_CLIENT_CA on the API).
    # API_CA_CERT *replaces* the default trust store, for a private CA. All
    # unset: httpx's default trust, no client certificate — unchanged.
    api_client_cert: str | None = None
    api_client_key: str | None = None
    api_ca_cert: str | None = None
    mcp_transport: Literal["stdio", "http", "sse"] = "stdio"
    mcp_server_host: str = "localhost"
    mcp_server_port: int = 9000
    # Run the HTTP transport without a per-connection MCP session (no
    # Mcp-Session-Id, no SSE resumability). Safe here — nothing is derived from
    # transport state — and lets instances scale without session affinity.
    # Ignored for stdio; forced off for sse, which requires a session.
    mcp_stateless_http: bool = True
    # Acknowledges an ingress in front of the HTTP transport that terminates
    # TLS and controls who reaches it. Silences the exposure warning only —
    # nothing else reads it, and it grants the server no capability.
    mcp_behind_proxy: bool = False
    # Cloud Run injects PORT; takes precedence over MCP_SERVER_PORT.
    port: int | None = None
    log_level: str = "INFO"
    # "console" (pretty), "json" (structured), or "cloudrun" (JSON, GCP severity).
    log_format: Literal["console", "json", "cloudrun"] = "console"
    api_timeout: int = 30

    @field_validator("mcp_auth_mode", "oidc_exchange", "oidc_client_auth", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: Any, info: ValidationInfo) -> Any:
        """``MCP_AUTH_MODE=`` (as compose's ``${VAR:-}`` writes it) means unset, not invalid."""
        if value == "":
            return "client_secret_post" if info.field_name == "oidc_client_auth" else None
        return value

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else self.mcp_server_port


settings = Settings()

# All API routes (except /health) are under the /v1 prefix since API v1.0.0
_API_V1 = "/v1"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _CloudRunJSONFormatter(logging.Formatter):
    """JSON log formatter compatible with GCP Cloud Logging severity mapping."""

    _SEVERITY = {
        "DEBUG": "DEBUG",
        "INFO": "INFO",
        "WARNING": "WARNING",
        "ERROR": "ERROR",
        "CRITICAL": "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": self._SEVERITY.get(record.levelname, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> None:
    """Configure root logger based on settings.log_format and settings.log_level."""
    level = settings.log_level.upper()
    formatter: logging.Formatter
    if settings.log_format in ("json", "cloudrun"):
        formatter = _CloudRunJSONFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Authentication — who this server is to the API
# ---------------------------------------------------------------------------
#
# ``MCP_AUTH_MODE`` (design/PLAN_mcp_auth.md, PLAN_mcp_new_auth.md in OBSL):
#
#   none                     no credential: an API running AUTH_MODE=none.
#   api_key                  API_KEY in API_KEY_HEADER: a key from the API's
#                            API_KEYS (one shared service identity), or a
#                            person's BI token (obsl_tok_…), with which the API
#                            runs as that person — on data sources that log in
#                            as the user too, when the token holds a warehouse
#                            sign-in. The way a local stdio server acts as you.
#   oidc_client_credentials  a client-credentials token from the IdP, cached and
#                            renewed shortly before it expires: one service
#                            identity managed at the IdP.
#   oidc_user                the caller's own identity, over HTTP. The MCP client
#                            signs the user in at the IdP and sends a token
#                            issued for *this server*; the server checks it
#                            (signature, issuer, audience, expiry) and exchanges
#                            it — Entra on-behalf-of, or RFC 8693 token exchange
#                            — for a token for OBSL, for the same user. The
#                            incoming token is never sent to the API (the MCP
#                            spec forbids passing it through).
#
# The first three are service modes: every caller acts as the one identity the
# environment holds. Only oidc_user tells callers apart, and so only it keys the
# API session and the loaded models by caller (``_Identity``).

AUTH_NONE = "none"
AUTH_API_KEY = "api_key"
AUTH_CLIENT_CREDENTIALS = "oidc_client_credentials"
AUTH_USER = "oidc_user"
_OAUTH_MODES = frozenset({AUTH_CLIENT_CREDENTIALS, AUTH_USER})

# A BI token, as OBSL issues it (``auth/bi_tokens.py``): only used to tailor advice.
_BI_TOKEN_PREFIX = "obsl_tok_"

EXCHANGE_ENTRA_OBO = "entra_obo"
EXCHANGE_TOKEN_EXCHANGE = "token_exchange"
# Entra's sign-in hosts (public cloud, US Government, China): their issuers
# exchange with on-behalf-of rather than RFC 8693, which Entra does not offer.
_ENTRA_HOSTS = frozenset(
    {"login.microsoftonline.com", "login.microsoftonline.us", "login.partner.microsoftonline.cn"}
)

# The incoming token's signing algorithms accepted: asymmetric only, the set
# OBSL accepts for its own tokens.
_JWT_ALGORITHMS = frozenset({"RS256", "PS256", "ES256", "EdDSA"})

# Seconds before expiry a cached OBSL token is renewed: a minute for a user's
# (PLAN_mcp_auth.md §3), 30 s for the service token (§4).
_USER_TOKEN_MARGIN = 60.0
_SERVICE_TOKEN_MARGIN = 30.0


class AuthConfigError(ValueError):
    """The ``MCP_AUTH_MODE`` settings are incomplete or contradict each other."""


def _auth_mode() -> str:
    """The effective ``MCP_AUTH_MODE``."""
    if settings.mcp_auth_mode:
        return settings.mcp_auth_mode
    return AUTH_API_KEY if settings.api_key else AUTH_NONE


def _split_setting(value: str | None) -> list[str]:
    """A comma- or space-separated setting as a list, empty entries dropped."""
    return [part for part in re.split(r"[,\s]+", value or "") if part]


def _exchange_kind() -> str:
    """How ``oidc_user`` exchanges a user's token: ``OIDC_EXCHANGE``, or by issuer."""
    if settings.oidc_exchange:
        return settings.oidc_exchange
    host = (urlsplit(settings.oidc_issuer or "").hostname or "").lower()
    return EXCHANGE_ENTRA_OBO if host in _ENTRA_HOSTS else EXCHANGE_TOKEN_EXCHANGE


def _auth_config_problems() -> list[str]:
    """What the configured ``MCP_AUTH_MODE`` is missing, one line per problem."""
    mode = _auth_mode()
    problems: list[str] = []
    if mode == AUTH_API_KEY and not settings.api_key:
        problems.append(
            "MCP_AUTH_MODE=api_key needs API_KEY: an API key, or your BI token "
            "from OBSL's /tokens page"
        )
    if mode not in _OAUTH_MODES:
        return problems
    if not settings.oidc_client_id:
        problems.append(f"MCP_AUTH_MODE={mode} needs OIDC_CLIENT_ID")
    if not (settings.oidc_client_secret or settings.oidc_client_secret_file):
        problems.append(f"MCP_AUTH_MODE={mode} needs OIDC_CLIENT_SECRET or OIDC_CLIENT_SECRET_FILE")
    if not (settings.obsl_scope or settings.obsl_audience):
        problems.append(
            f"MCP_AUTH_MODE={mode} needs OBSL_SCOPE or OBSL_AUDIENCE: what to ask "
            "the identity provider for (Entra: <OBSL app ID URI>/.default)"
        )
    if mode == AUTH_CLIENT_CREDENTIALS:
        if not (settings.oidc_issuer or settings.oidc_token_endpoint):
            problems.append(
                "MCP_AUTH_MODE=oidc_client_credentials needs OIDC_ISSUER or OIDC_TOKEN_ENDPOINT"
            )
        return problems
    if not settings.oidc_issuer:
        problems.append("MCP_AUTH_MODE=oidc_user needs OIDC_ISSUER")
    resource = urlsplit(settings.mcp_resource_url or "")
    if not settings.mcp_resource_url:
        problems.append(
            "MCP_AUTH_MODE=oidc_user needs MCP_RESOURCE_URL: this server's public "
            "MCP endpoint URL, e.g. https://mcp.example.com/mcp"
        )
    elif resource.scheme not in ("https", "http") or not resource.hostname:
        problems.append(f"MCP_RESOURCE_URL must be an absolute URL: {settings.mcp_resource_url}")
    elif resource.query or resource.fragment:
        problems.append(f"MCP_RESOURCE_URL must have no query or fragment: {resource.geturl()}")
    if _exchange_kind() == EXCHANGE_ENTRA_OBO and not settings.obsl_scope:
        problems.append(
            "Entra on-behalf-of (OIDC_EXCHANGE=entra_obo) needs OBSL_SCOPE: "
            "<OBSL app ID URI>/.default or a named scope"
        )
    return problems


def _check_auth_config() -> None:
    """Raise :class:`AuthConfigError` naming every problem of the auth settings."""
    problems = _auth_config_problems()
    if problems:
        raise AuthConfigError("; ".join(problems))


def _auth_summary() -> str:
    """One line for the startup banner: the auth mode, never a credential."""
    mode = _auth_mode()
    if mode == AUTH_API_KEY:
        kind = "a BI token" if (settings.api_key or "").startswith(_BI_TOKEN_PREFIX) else "a key"
        return f"api_key ({kind} in {settings.api_key_header})"
    if mode == AUTH_CLIENT_CREDENTIALS:
        return f"oidc_client_credentials (client {settings.oidc_client_id})"
    if mode == AUTH_USER:
        return (
            f"oidc_user ({_exchange_kind()} at {settings.oidc_issuer}; "
            f"resource {settings.mcp_resource_url})"
        )
    return "none"


# --- The identity provider --------------------------------------------------

# Reentrant: discovery holds it while it builds the client under it.
_idp_lock = threading.RLock()
_idp_client: httpx.Client | None = None
_oidc_metadata_cache: dict[str, Any] | None = None


def _get_idp_client() -> httpx.Client:
    """The shared client for the IdP: discovery and its token endpoint.

    Separate from the API's client — another host, httpx's default trust (and
    ``SSL_CERT_FILE``), none of the ``API_*`` TLS settings or credentials.
    """
    global _idp_client
    if _idp_client is None:
        with _idp_lock:
            if _idp_client is None:
                _idp_client = httpx.Client(
                    timeout=settings.api_timeout,
                    headers={
                        "User-Agent": f"OrionBelt-MCP/{__version__}",
                        "Accept": "application/json",
                    },
                )
    return _idp_client


def _oidc_metadata() -> dict[str, Any]:
    """The IdP's OpenID discovery document, fetched once.

    Its ``issuer`` must be ``OIDC_ISSUER`` (OpenID Connect Discovery §4.3): a
    document naming another issuer is a misconfiguration, never a fallback.
    Every failure is a :class:`ToolError` — never a ``ValueError``, which
    mode detection would mistake for an unreadable answer and guess past.
    """
    global _oidc_metadata_cache
    if _oidc_metadata_cache is not None:
        return _oidc_metadata_cache
    with _idp_lock:
        if _oidc_metadata_cache is not None:
            return _oidc_metadata_cache
        issuer = (settings.oidc_issuer or "").rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        try:
            resp = _get_idp_client().get(url)
        except httpx.HTTPError as exc:
            raise ToolError(
                f"The identity provider's discovery document could not be fetched "
                f"({type(exc).__name__}): {url}"
            ) from None
        try:
            doc = resp.json() if resp.status_code == 200 else None
        except ValueError:
            doc = None
        if not isinstance(doc, dict) or not all(
            isinstance(doc.get(k), str) for k in ("issuer", "jwks_uri", "token_endpoint")
        ):
            raise ToolError(
                f"The identity provider's discovery document at {url} is unusable "
                f"(HTTP {resp.status_code}; it needs issuer, jwks_uri and token_endpoint)"
            )
        if doc["issuer"].rstrip("/") != issuer:
            raise ToolError(
                f"OIDC_ISSUER ({settings.oidc_issuer}) is not the issuer the identity "
                f"provider names in its discovery document ({doc['issuer']}); set it to that"
            )
        _oidc_metadata_cache = doc
    return doc


def _oidc_client_secret() -> str:
    """``OIDC_CLIENT_SECRET``, or ``OIDC_CLIENT_SECRET_FILE`` read on each use (rotation)."""
    if settings.oidc_client_secret is not None:
        return settings.oidc_client_secret.get_secret_value()
    path = settings.oidc_client_secret_file or ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ToolError(
            f"OIDC_CLIENT_SECRET_FILE could not be read ({exc.strerror or exc}): {path}"
        ) from None


class _IdpRefusedError(Exception):
    """The IdP's token endpoint refused a request (OAuth ``error`` response)."""

    def __init__(self, status: int, error: str | None, description: str | None) -> None:
        self.status = status
        self.error = error
        # The first line only, capped: Entra appends trace and correlation ids.
        first = (description or "").strip().splitlines()
        self.description = first[0][:300] if first else None
        super().__init__(self.summary())

    def summary(self) -> str:
        if not self.error:
            return f"HTTP {self.status}"
        return f"{self.error}: {self.description}" if self.description else self.error


def _unverified_claims(token: str) -> dict[str, Any]:
    """A JWT's payload, unverified: only ever read for a token's own expiry."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}


def _token_expiry(body: dict[str, Any], token: str) -> float | None:
    """When a token from the IdP expires (epoch seconds): ``expires_in``, or its ``exp``."""
    with contextlib.suppress(TypeError, ValueError):
        if body.get("expires_in") is not None:
            return time.time() + float(body["expires_in"])
    exp = _unverified_claims(token).get("exp")
    return float(exp) if isinstance(exp, int | float) else None


def _idp_token_request(form: dict[str, str]) -> tuple[str, float | None]:
    """POST ``form`` to the IdP's token endpoint as this server's client.

    Returns the access token and its expiry. A refusal raises
    :class:`_IdpRefusedError` for the caller to explain; the IdP being
    unreachable raises :class:`ToolError`. No token is ever logged or put in an
    error.
    """
    endpoint = settings.oidc_token_endpoint or _oidc_metadata()["token_endpoint"]
    client_id = settings.oidc_client_id or ""
    secret = _oidc_client_secret()
    data = dict(form)
    auth: httpx.Auth | None = None
    if settings.oidc_client_auth == "client_secret_basic":
        # RFC 6749 §2.3.1: both are form-encoded before the Basic encoding.
        auth = httpx.BasicAuth(quote(client_id, safe=""), quote(secret, safe=""))
    else:
        data["client_id"] = client_id
        data["client_secret"] = secret
    try:
        resp = _get_idp_client().post(endpoint, data=data, auth=auth)
    except httpx.HTTPError as exc:
        raise ToolError(
            f"The identity provider's token endpoint could not be reached "
            f"({type(exc).__name__}); retry later."
        ) from None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    token = body.get("access_token")
    if resp.status_code != 200 or not isinstance(token, str) or not token:
        error = body.get("error")
        description = body.get("error_description")
        raise _IdpRefusedError(
            resp.status_code,
            error if isinstance(error, str) else None,
            description if isinstance(description, str) else None,
        )
    return token, _token_expiry(body, token)


# --- OBSL tokens --------------------------------------------------------------

_token_lock = threading.Lock()
# The one client-credentials token (token, expires at) — renewed under this lock,
# so callers arriving meanwhile wait for the one request rather than each sending one.
_service_token_lock = threading.Lock()
_service_token: tuple[str, float] | None = None
# OBSL tokens obtained for users, by identity (``_user_identity_key``).
_user_tokens: dict[str, tuple[str, float]] = {}


def _service_obsl_token(*, renew: bool = False) -> tuple[str, bool]:
    """The client-credentials token for OBSL, and whether it came from the cache."""
    global _service_token
    with _service_token_lock:
        cached = _service_token
        if not renew and cached and cached[1] - _SERVICE_TOKEN_MARGIN > time.time():
            return cached[0], True
        form = {"grant_type": "client_credentials"}
        if settings.obsl_scope:
            form["scope"] = settings.obsl_scope
        if settings.obsl_audience:
            form["audience"] = settings.obsl_audience
        try:
            token, expires_at = _idp_token_request(form)
        except _IdpRefusedError as exc:
            logger.warning("The identity provider refused the client credentials: %s", exc)
            if exc.status >= 500 and not exc.error:
                raise ToolError(
                    f"The identity provider is unavailable ({exc.summary()}); retry later."
                ) from None
            raise ToolError(
                f"The identity provider refused this server's client credentials "
                f"({exc.summary()}): the operator must check OIDC_CLIENT_ID, "
                "OIDC_CLIENT_SECRET and OBSL_SCOPE / OBSL_AUDIENCE."
            ) from None
        _service_token = (token, expires_at) if expires_at else None
        return token, False


def _user_identity_key(token: AccessToken) -> str:
    """Who an incoming token speaks for: its issuer and subject."""
    claims = token.claims or {}
    return f"{claims.get('iss', '')} {claims.get('sub') or token.subject or ''}"


_NO_SIGN_IN = (
    "This server acts as the signed-in user (MCP_AUTH_MODE=oidc_user), and this "
    "request carries no sign-in: connect through an MCP client that signs you in."
)


def _incoming_token() -> AccessToken:
    """The verified token of the current request (``oidc_user``), or a ToolError."""
    token = get_access_token()
    if token is None:
        raise ToolError(_NO_SIGN_IN)
    return token


def _exchange_form(subject_token: str) -> dict[str, str]:
    """The token request that exchanges a user's token for one for OBSL."""
    if _exchange_kind() == EXCHANGE_ENTRA_OBO:
        return {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "requested_token_use": "on_behalf_of",
            "assertion": subject_token,
            "scope": settings.obsl_scope or "",
        }
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    if settings.obsl_audience:
        form["audience"] = settings.obsl_audience
    if settings.obsl_scope:
        form["scope"] = settings.obsl_scope
    return form


def _exchange_refusal(exc: _IdpRefusedError) -> str:
    """A refused exchange, for the user: what the IdP said and what to do."""
    if exc.status >= 500 and not exc.error:
        return (
            "Your sign-in could not be exchanged for an OBSL token: the identity "
            f"provider is unavailable ({exc.summary()}); retry later."
        )
    if exc.error == "interaction_required":
        advice = (
            "the identity provider wants you to sign in again (MFA or conditional "
            "access): sign in again in your MCP client. Retrying will not help."
        )
    elif exc.error == "consent_required":
        advice = (
            "consent is missing for this server's permission on OBSL: sign in again "
            "in your MCP client and consent, or ask an administrator to grant it."
        )
    elif exc.error in ("invalid_client", "unauthorized_client"):
        advice = (
            "the identity provider refused this server's own client: the operator "
            "must check OIDC_CLIENT_ID, OIDC_CLIENT_SECRET and that the app may "
            "exchange tokens for OBSL."
        )
    elif exc.error in ("invalid_scope", "invalid_target"):
        advice = "the operator must check OBSL_SCOPE / OBSL_AUDIENCE."
    else:
        advice = "sign in again in your MCP client."
    return (
        "Your sign-in could not be exchanged for an OBSL token (the identity "
        f"provider answered {exc.summary()}): {advice}"
    )


def _user_obsl_token(incoming: AccessToken, *, renew: bool = False) -> tuple[str, bool]:
    """An OBSL token for the user of ``incoming``, and whether it came from the cache.

    Cached per user until a minute before it expires; a new one is always
    exchanged from the *current* incoming token, never a stored one.
    """
    key = _user_identity_key(incoming)
    now = time.time()
    if not renew:
        with _token_lock:
            cached = _user_tokens.get(key)
        if cached and cached[1] - _USER_TOKEN_MARGIN > now:
            return cached[0], True
    try:
        token, expires_at = _idp_token_request(_exchange_form(incoming.token))
    except _IdpRefusedError as exc:
        logger.warning(
            "Token exchange refused for sub=%s: %s", (incoming.claims or {}).get("sub"), exc
        )
        raise ToolError(_exchange_refusal(exc)) from None
    with _token_lock:
        for stale in [k for k, (_, exp) in _user_tokens.items() if exp <= now]:
            del _user_tokens[stale]
        if expires_at:
            _user_tokens[key] = (token, expires_at)
        else:
            _user_tokens.pop(key, None)
    return token, False


def _obsl_bearer(*, renew: bool = False) -> tuple[str, bool]:
    """The bearer token for this API request in an oidc mode, and whether it was cached."""
    if _auth_mode() == AUTH_CLIENT_CREDENTIALS:
        return _service_obsl_token(renew=renew)
    return _user_obsl_token(_incoming_token(), renew=renew)


def _www_authenticate_param(response: httpx.Response, name: str) -> str | None:
    """A parameter of the response's ``WWW-Authenticate`` header (RFC 6750 §3)."""
    pattern = rf'(?<![\w-]){re.escape(name)}="([^"]*)"'
    match = re.search(pattern, response.headers.get("www-authenticate", ""))
    return match.group(1) if match else None


# Marks a request the API answers without a credential (``/health``): no token
# is obtained for it, so the version gate needs no user and no IdP round trip.
_PUBLIC_REQUEST = "orionbelt.public"


class _ObslAuth(httpx.Auth):
    """Put the oidc modes' bearer token on each API request.

    Per request, because in ``oidc_user`` the token is the current caller's.
    A request that already carries ``Authorization`` keeps it (the shutdown
    cleanup acts as a stored identity). A *cached* token the API refuses as
    ``invalid_token`` (revoked, clock skew) is replaced once by a fresh one.
    ``api_key`` needs none of this: its key is a default header of the client.
    """

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        if (
            _auth_mode() not in _OAUTH_MODES
            or request.extensions.get(_PUBLIC_REQUEST)
            or "authorization" in request.headers
        ):
            yield request
            return
        token, cached = _obsl_bearer()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if (
            cached
            and response.status_code == 401
            and _www_authenticate_param(response, "error") == "invalid_token"
        ):
            token, _ = _obsl_bearer(renew=True)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


# --- Incoming tokens (oidc_user) -----------------------------------------------


class _IdpUnavailableError(Exception):
    """The IdP's keys could not be fetched: no verdict on the token."""


class _JwksVerifier(JWTVerifier):
    """FastMCP's JWT check, with an unreachable JWKS an error rather than a refusal.

    ``JWTVerifier`` reports a failed key fetch as an invalid token, and a 401
    sends the client round the sign-in loop for what is an outage. Here it
    escapes as :class:`_IdpUnavailableError` (not a ``ValueError``, which the
    base class would swallow), and the failed attempt does not count against
    the refetch throttle, so the next request tries again.
    """

    async def _fetch_jwks(self) -> dict[str, Any]:
        try:
            return await super()._fetch_jwks()
        except Exception as exc:  # noqa: BLE001 — transport, status or JSON: all an outage
            self._jwks_last_refresh_attempt = None
            raise _IdpUnavailableError(
                f"The identity provider's keys could not be fetched "
                f"({type(exc).__name__}): {self.jwks_uri}"
            ) from None


class _OidcUserVerifier(TokenVerifier):
    """Check a token sent to this server: the IdP's signature, issuer, audience, expiry.

    The keys come from the IdP's JWKS (cached; refetched for an unknown key id),
    found through its discovery document on the first token — so importing the
    server needs no network. A token must carry ``exp`` and ``sub``; the
    audience is this server (``MCP_AUDIENCE``, else ``MCP_RESOURCE_URL``), so a
    token issued for another resource — OBSL's included — is refused.
    """

    def __init__(self, audience: list[str]) -> None:
        super().__init__()
        self._audience = audience
        self._verifiers: dict[str, JWTVerifier] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        # Whatever a caller sends is refused (None, a 401), never an exception:
        # the header is untrusted JSON, and may be a list or carry a list alg.
        try:
            header = decode_jwt_header(token)
        except ValueError:
            return None
        alg = header.get("alg") if isinstance(header, dict) else None
        if not isinstance(alg, str) or alg not in _JWT_ALGORITHMS:
            logger.info("Bearer token rejected: signing algorithm %r is not accepted", alg)
            return None
        try:
            metadata = _oidc_metadata_cache or await asyncio.to_thread(_oidc_metadata)
        except ToolError as exc:
            # Not the caller's fault and not fixable by signing in again: an
            # error (a 500), rather than a 401 that sends the client round the
            # sign-in loop.
            logger.error("Cannot verify bearer tokens: %s", exc)
            raise
        verifier = self._verifiers.get(alg)
        if verifier is None:
            verifier = self._verifiers.setdefault(
                alg,
                _JwksVerifier(
                    jwks_uri=metadata["jwks_uri"],
                    issuer=metadata["issuer"],
                    audience=self._audience,
                    algorithm=alg,
                ),
            )
        try:
            access = await verifier.verify_token(token)
        except _IdpUnavailableError as exc:
            logger.error("Cannot verify bearer tokens: %s", exc)
            raise
        if access is None:
            return None
        if not access.claims.get("sub") or access.claims.get("exp") is None:
            logger.warning("Bearer token rejected: it has no sub or no exp claim")
            return None
        return access


class _ResourceAuthProvider(RemoteAuthProvider):
    """RFC 9728 protected resource metadata naming ``MCP_RESOURCE_URL`` as the resource.

    FastMCP derives the resource from a base URL plus the path the endpoint is
    mounted at, and tells the provider that path here; MCP_RESOURCE_URL names
    the whole endpoint, so its base is what remains without that path. A URL
    that does not end in the path cannot be this endpoint's.
    """

    def set_mcp_path(self, mcp_path: str | None) -> None:
        resource = (settings.mcp_resource_url or "").rstrip("/")
        base = resource
        if mcp_path and mcp_path.strip("/"):
            suffix = "/" + mcp_path.strip("/")
            if not urlsplit(resource).path.endswith(suffix):
                raise AuthConfigError(
                    f"MCP_RESOURCE_URL ({resource}) must be the URL of this server's "
                    f"MCP endpoint, ending in {suffix}"
                )
            base = resource[: -len(suffix)]
        self.resource_base_url = AnyHttpUrl(base)
        super().set_mcp_path(mcp_path)


def _build_auth_provider() -> RemoteAuthProvider | None:
    """The HTTP transport's bearer-token check: ``oidc_user`` only, else None.

    Built at import, with the server, so that every entrypoint — ``main()``
    and the exported ``server.py:mcp`` alike — serves the user mode only
    behind it, never open. Unusable settings fail the import, closed.
    """
    _check_auth_config()
    if _auth_mode() != AUTH_USER:
        return None
    resource = (settings.mcp_resource_url or "").rstrip("/")
    # The resource as the metadata advertises it (host lowercased, default port
    # and trailing slash dropped) — what a client asks the IdP to put in
    # ``aud`` — and as configured.
    audience = _split_setting(settings.mcp_audience) or list(
        dict.fromkeys([str(AnyHttpUrl(resource)).rstrip("/"), resource])
    )
    scopes = _split_setting(settings.mcp_scopes)
    return _ResourceAuthProvider(
        token_verifier=_OidcUserVerifier(audience),
        authorization_servers=[AnyHttpUrl(settings.oidc_issuer or "")],
        base_url=settings.mcp_resource_url or "",
        scopes_supported=scopes,
        challenge_scopes=scopes or None,
        resource_name="OrionBelt Semantic Layer",
    )


# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _server_lifespan(server):
    """Clean up the API session when the server stops.

    Tools are registered by ``main()`` before a stdio server starts, and
    otherwise on the first request by :class:`LazyInitMiddleware`, after the
    version gate: on HTTP so the container starts without waiting for the
    API (Cloud Run's startup probe shouldn't depend on the API also being
    warm), and for the exported ``server.py:mcp`` on any transport, which
    never runs ``main()``.
    """
    yield
    # Best-effort session cleanup on shutdown. A user's session is closed as
    # that user, with the OBSL token still cached for them; once that has
    # expired there is no request to act for them, and the API's TTL ends it.
    global _http_client
    with _state_lock:
        open_sessions = [(k, s.session_id) for k, s in _identities.items() if s.session_id]
        _identities.clear()
    for key, session_id in open_sessions:
        headers = None
        if key != _SERVICE_IDENTITY:
            with _token_lock:
                cached = _user_tokens.get(key)
            if not cached or cached[1] <= time.time():
                continue
            headers = {"Authorization": f"Bearer {cached[0]}"}
        try:
            _get_client().delete(f"{_API_V1}/sessions/{session_id}", headers=headers)
            logger.info("Cleaned up API session: %s", session_id)
        except Exception:
            logger.debug("Session cleanup failed (API TTL will handle it)")
    if _http_client is not None:
        _http_client.close()
        _http_client = None


def _package_version() -> str | None:
    """This MCP server's installed version, or None when not installed (dev tree)."""
    try:
        return importlib.metadata.version("orionbelt-semantic-layer-mcp")
    except importlib.metadata.PackageNotFoundError:
        return None


# Reported as serverInfo.version on initialize (NOT the FastMCP library version),
# and used for the User-Agent and startup banner.
__version__ = _package_version() or "dev"

mcp = FastMCP(
    "OrionBelt Semantic Layer",
    version=__version__,
    lifespan=_server_lifespan,
    auth=_build_auth_provider(),
)

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_state_lock = threading.RLock()
_http_client: httpx.Client | None = None
_single_model_mode: bool = False
_query_execute_enabled: bool = False
_tools_registered: bool = False
# When the API last passed the version check (``time.monotonic()``); None: not yet.
_api_verified_at: float | None = None


@dataclass
class _Identity:
    """One caller's API session and the models loaded into it.

    The service modes have one identity, ``_SERVICE_IDENTITY``, shared by every
    caller as before. ``oidc_user`` has one per signed-in user: the API gives a
    session to its creator alone (another identity gets 404), and the loaded
    models, which drive the design-time ↔ run-time tool surface (see "Tool
    phase model" below), are that user's.
    """

    session_id: str | None = None
    # Model ids loaded into ``session_id`` (multi-model mode).
    loaded_model_ids: set[str] = dataclass_field(default_factory=set)
    last_used: float = dataclass_field(default_factory=time.monotonic)
    # Held while the session is created, so one identity's slow creation holds
    # up neither another identity nor the state lock.
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)


_SERVICE_IDENTITY = "service"
# A user unseen this long is forgotten: their API session has long expired
# (the API's TTL ends it), and the map stays as small as the active users.
_IDENTITY_IDLE_SECONDS = 4 * 3600.0
_identities: dict[str, _Identity] = {}


def _current_identity_key() -> str | None:
    """The identity of the current request; None for ``oidc_user`` without a sign-in."""
    if _auth_mode() != AUTH_USER:
        return _SERVICE_IDENTITY
    token = get_access_token()
    return None if token is None else _user_identity_key(token)


def _identity_or_none() -> _Identity | None:
    """The current identity's state, if it has any yet."""
    key = _current_identity_key()
    if key is None:
        return None
    with _state_lock:
        state = _identities.get(key)
        if state is not None:
            state.last_used = time.monotonic()
        return state


def _identity() -> _Identity:
    """The current identity's state, created on first use.

    Raises :class:`ToolError` in ``oidc_user`` when the request has no sign-in:
    never the service identity, which would share one user's session with the next.
    """
    key = _current_identity_key()
    if key is None:
        raise ToolError(_NO_SIGN_IN)
    now = time.monotonic()
    with _state_lock:
        for idle in [
            k
            for k, s in _identities.items()
            if k != key and k != _SERVICE_IDENTITY and now - s.last_used > _IDENTITY_IDLE_SECONDS
        ]:
            del _identities[idle]
        state = _identities.get(key)
        if state is None:
            state = _identities[key] = _Identity()
        state.last_used = now
        return state


# ---------------------------------------------------------------------------
# Tool phase model — design-time vs run-time surface switching
# ---------------------------------------------------------------------------
#
# The surface SWAPS between two phase-scoped sets as the lifecycle moves
# load → query → unload (design/PLAN_tool_phase_switching.md, Option A). Tools
# fall into three buckets:
#
#   * always (bucket 1) — ALWAYS listed, in both phases. The lifecycle/
#     transition verbs (load_model, remove_model), which
#     must stay available in the run phase so a second model can be loaded mid-session
#     (max_models_per_session = 10); plus run_batch, the self-contained one-shot
#     (loads/references a model inline in one call, so it depends on no prior
#     session state and is valid in either phase).
#   * design-only (bucket 2) — listed ONLY in the design phase (no model
#     loaded). Authoring/reference + pure file transforms. HIDDEN in run phase
#     so the run surface isn't polluted with authoring noise.
#   * run-only (bucket 3) — listed ONLY in the run phase (a model is loaded):
#     queries, introspection, execution.
#
#   design phase (no model) → bucket 1 + bucket 2
#   run phase   (model[s])  → bucket 1 + bucket 3   (a SWAP, not additive)
#
# Phase is *derived from explicit loaded-model state* (is a model resolvable for
# this session?), never from hidden per-connection transport state, so it stays
# stateless-clean — which is what lets the HTTP transport run with
# ``stateless_http=True`` (see ``_stateless_http_enabled``) and scale across
# instances without session affinity. ``tools/list`` is filtered per phase by
# ``PhaseMiddleware``;
# a run-only verb invoked in the design phase is rejected with a structured
# guard error steering the host to ``load_model`` + re-list.

PHASE_DESIGN = "design"
PHASE_RUN = "run"

# Bucket 1 — always listed, in both phases: lifecycle/transition verbs plus the
# self-contained one-shot batch and validate_model (both take their model inline,
# so they depend on no prior session state) and the JSON-schema reference (needed
# to author execute_query payloads in either phase).
_ALWAYS_TOOLS: frozenset[str] = frozenset(
    {
        "load_model",
        "remove_model",
        "run_batch",
        "validate_model",
        "get_json_schema",
    }
)

# Bucket 2 — design-only. Authoring/reference + file transforms. Listed only in
# the design phase; hidden once a model is loaded.
_DESIGN_TOOLS: frozenset[str] = frozenset(
    {
        "get_obml_reference",
        "get_function_catalog",
        "list_dialects",
        # configured data sources, for defaultDataSource / dataSource (API 3.0)
        "list_datasources",
        # what a data source holds, from its catalog, to write a model from (API 3.1)
        "list_datasource_schemas",
        "inspect_datasource_schema",
    }
)

# Bucket 3 — run-only. Queries, introspection, execution. Listed only once a
# model is loaded; gated by the structured guard error otherwise. Names span the
# single-model and multi-model registrations (same names, different signatures).
_RUN_TIME_TOOLS: frozenset[str] = frozenset(
    {
        "get_model",
        "export_model_to_osi",
        "describe_model",
        "get_model_diagram",
        "find_artefacts",
        "explain_artefact",
        "execute_query",
        # dimension-value search (API 2.36)
        "search_values",
        "list_examples",
        "get_example",
        "get_model_graph",
        "get_join_graph",
        "find_composables",
        "query_model_graph_by_sparql",
        "list_models",
        # lineage of artefacts and queries (API 2.32)
        "get_lineage",
        # business rules + ontology links (API 2.30)
        "list_rules",
        "explain_rule",
        "evaluate_rule",
        "evaluate_rules",
        "find_concept_mappings",
        "list_concept_namespaces",
        "list_unmapped_artefacts",
    }
)

# ---------------------------------------------------------------------------
# Tool annotations — behavioural hints for hosts (MCP ``ToolAnnotations``)
# ---------------------------------------------------------------------------
#
# Hints only, never access control: hosts use them to decide whether to ask the
# user before a call. Every registered tool carries one of these presets (a test
# enforces it). ``destructiveHint``/``idempotentHint`` are meaningful only when
# ``readOnlyHint`` is false, so the read-only presets leave them unset.
#
# Query execution is read-only: the API compiles to SELECT only and opens
# warehouse connections read-only; its only writes are its own result cache.

# Reference data and model introspection — answered by the API alone.
_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# Reaches past the API: compiled SQL against the live warehouse (execute_query,
# evaluate_rule(s)), a datasource probe (validate_model with ``online``) or the
# warehouse's catalog (list_datasource_schemas, inspect_datasource_schema). SPARQL
# is closed-world: since OBSL 2.33.1 the API rejects ``SERVICE`` / ``FROM``.
_READ_ONLY_OPEN_WORLD = ToolAnnotations(read_only_hint=True, open_world_hint=True)
# Adds a model to the session; never replaces or drops an existing one.
_SESSION_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
# Drops a model from the session; repeating it changes nothing further.
_SESSION_DELETE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
# run_batch: executes against the warehouse and, with ``persist_model``, keeps
# the model in the session. Not destructive: since OBSL 2.33.1 the API's cleanup
# no longer evicts a model it only *reused* via dedup.
_BATCH = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def _mark_model_loaded(model_id: str) -> None:
    """Record a model as loaded — flips the caller's surface to the run-time phase."""
    if not model_id:
        return
    state = _identity()
    with _state_lock:
        state.loaded_model_ids.add(model_id)


def _mark_model_removed(model_id: str) -> None:
    """Forget a model — flips back to design-time once none remain loaded."""
    state = _identity_or_none()
    if state is not None:
        with _state_lock:
            state.loaded_model_ids.discard(model_id)


def _current_phase() -> str:
    """Resolve the caller's active phase from its loaded-model state.

    Single-model mode always has a pre-loaded model, so it is permanently in the
    run-time phase. Multi-model mode is design-time until ``load_model`` succeeds
    and reverts to design-time once every model is removed. Per identity: in
    ``oidc_user`` one user's loaded model does not flip another user's surface.
    """
    if _single_model_mode:
        return PHASE_RUN
    state = _identity_or_none()
    with _state_lock:
        if state is not None and state.loaded_model_ids:
            return PHASE_RUN
        return PHASE_DESIGN


def _is_runtime_tool(name: str) -> bool:
    """True if ``name`` is a run-only verb (bucket 3, gated by loaded-model state)."""
    return name in _RUN_TIME_TOOLS


def _tool_visible_in_phase(name: str, design_phase: bool) -> bool:
    """Phase visibility for ``name``: lifecycle always; otherwise swap by phase.

    design phase → always + design-only; run phase → always + run-only.
    A name in no bucket is treated as run-only (conservative: hidden until a
    model is loaded) — the partition test keeps every registered tool classified.
    """
    if name in _ALWAYS_TOOLS:
        return True
    if design_phase:
        return name in _DESIGN_TOOLS
    return name not in _DESIGN_TOOLS


# ---------------------------------------------------------------------------
# Capability gating — orthogonal to phase
# ---------------------------------------------------------------------------
#
# Some verbs should be omitted not because of lifecycle phase but because the
# server is *configured* not to support them. Unlike phase, a capability flag is
# fixed per server config (resolved once at startup), so the tools are always
# *registered* and simply filtered out of ``tools/list`` (and refused at call
# time) when their capability is disabled. This composes with phase gating: a
# verb is visible only if its phase is active *and* its capability is enabled.
#
# To add a future "the server can't do X here" flag: register a resolver in
# ``_CAPABILITY_RESOLVERS`` and map the affected tool names in ``_TOOL_CAPABILITY``.

CAP_QUERY_EXECUTE = "query_execute"

# Tool name → the capability flag it requires. Tools absent from this map need
# no capability and are always available (subject to phase).
_TOOL_CAPABILITY: dict[str, str] = {
    "execute_query": CAP_QUERY_EXECUTE,
    # ``evaluate_rules`` is deliberately not gated: with ``dry_run=True`` it
    # compiles every rule without executing, which works with execution off.
    "evaluate_rule": CAP_QUERY_EXECUTE,
    # Each value-search step is an executed dimension-only query (API 2.36).
    "search_values": CAP_QUERY_EXECUTE,
    # Schema inspection reads the database's catalog; the API answers 503
    # without QUERY_EXECUTE=true (API 3.1).
    "list_datasource_schemas": CAP_QUERY_EXECUTE,
    "inspect_datasource_schema": CAP_QUERY_EXECUTE,
}

# Capability flag → a resolver reading the current (startup-detected) config.
_CAPABILITY_RESOLVERS: dict[str, Callable[[], bool]] = {
    CAP_QUERY_EXECUTE: lambda: _query_execute_enabled,
}


def _capability_enabled(cap: str) -> bool:
    """True if capability ``cap`` is enabled. Unknown capabilities fail open."""
    resolver = _CAPABILITY_RESOLVERS.get(cap)
    return resolver() if resolver is not None else True


def _tool_capability_ok(name: str) -> bool:
    """True if the tool's required capability (if any) is enabled."""
    cap = _TOOL_CAPABILITY.get(name)
    return cap is None or _capability_enabled(cap)


# ---------------------------------------------------------------------------
# Lazy initialization middleware (HTTP transport only)
# ---------------------------------------------------------------------------


class LazyInitMiddleware(Middleware):
    """Defer API mode detection and tool registration to the first MCP request.

    On Cloud Run, the container starts immediately (Cloud Run startup probe
    only needs the HTTP listener to be up). The first MCP request — typically
    ``initialize`` from the client — triggers mode detection. This absorbs API
    cold-start latency into the first request rather than the container boot,
    so the two services don't have to be warm at the same time.

    Runs after :class:`ApiVersionGateMiddleware`, and like it leaves the
    handshake alone, so the mode is only ever detected on an API of this
    server's ``major.minor``. The detection's request runs in a worker thread,
    one for all the requests waiting on it, so it holds up no other request;
    the tools are then registered here, on the event loop.
    """

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if not _tools_registered and context.method not in _HANDSHAKE_METHODS:
            try:
                detected = await _mode_detection_for_caller()()
            except ToolError as exc:
                # A refused credential (``_detect_api_mode``): an MCP error
                # saying why, as the version gate answers, on any method —
                # and nothing frozen, so the next request detects again.
                raise McpError(INTERNAL_ERROR, str(exc)) from None
            _setup_mode_tools(detected)
            with _state_lock:
                _mode_detections.clear()
        return await call_next(context)


class _SharedRun:
    """A blocking call run in a worker thread, one run at a time.

    Callers arriving while it runs await that run, its result or its error,
    instead of starting their own. A caller cancelled meanwhile leaves the run
    to the others (``shield``). A run belongs to the event loop that started
    it, so a later loop (a test's, a restarted server's) starts its own.
    """

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn
        self._task: asyncio.Task[Any] | None = None

    async def __call__(self) -> Any:
        loop = asyncio.get_running_loop()
        task = self._task
        if task is None or task.done() or task.get_loop() is not loop:
            task = self._task = loop.create_task(asyncio.to_thread(self._fn))
            # Read the error even when every caller was cancelled, so asyncio
            # does not report it as never retrieved.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        return await asyncio.shield(task)


# Answered by this server alone, never the API. The gate lets them through: a
# client refused at ``server/discover`` falls back to ``initialize``, which a
# 2026-07-28 connection then rejects for its protocol, hiding the reason.
_HANDSHAKE_METHODS = frozenset({"initialize", "server/discover", "ping"})


# How long a passed version check holds before a request checks again.
_API_RECHECK_SECONDS = 60.0


class ApiVersionGateMiddleware(Middleware):
    """Serve no request unless the API is of this server's ``major.minor``.

    The check of :func:`_verify_api`, on every transport: stdio runs it at
    startup and exits on a failure, but HTTP starts before it reaches the API
    (see :class:`LazyInitMiddleware`), and either can outlive the API it
    checked: a redeploy to another minor under a running server would
    otherwise be served by tools written for the previous one. So a request
    checks again once the last passed check is older than
    ``_API_RECHECK_SECONDS``, whatever the protocol (clients of the
    2026-07-28 protocol send no ``initialize``). A failed check answers the
    request with an MCP error saying why rather than exiting, which on Cloud
    Run would only restart the container into the same answer; the next
    request checks again, so an API upgraded meanwhile is served without a
    restart. The handshake (``_HANDSHAKE_METHODS``) is let through, so a
    client connects and its first listing or call reads why it is refused.

    The check's request runs in a worker thread, one for all the requests
    that need it at once (:class:`_SharedRun`): a slow API holds up the
    requests waiting on the check, not the server's pings, cancellations or
    other sessions' work.
    """

    async def on_request(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        if context.method not in _HANDSHAKE_METHODS:
            checked = _api_verified_at
            if checked is None or time.monotonic() - checked >= _API_RECHECK_SECONDS:
                try:
                    await _shared_api_check()
                except _ApiUnusableError as exc:
                    raise McpError(INTERNAL_ERROR, str(exc)) from None
        return await call_next(context)


def _verify_api_logged() -> None:
    """:func:`_verify_api` for a serving server, its outcome logged once per check."""
    was_verified = _api_verified_at is not None
    try:
        _verify_api()
    except _ApiUnusableError as exc:
        logger.error("%s", exc)
        raise
    if not was_verified:
        logger.info("API health check passed (%s)", settings.api_base_url)


_shared_api_check = _SharedRun(_verify_api_logged)

# Mode detection reads /v1/settings with the caller's credential, so it is
# shared per identity: in oidc_user, one user's refusal (a missing scope, an
# IdP asking for MFA) is never answered to another user's request. The service
# modes have one identity, hence one shared run, as before.
_mode_detections: dict[str, _SharedRun] = {}


def _mode_detection_for_caller() -> _SharedRun:
    """The shared mode-detection run of the current identity."""
    key = _current_identity_key() or ""
    with _state_lock:
        run = _mode_detections.get(key)
        if run is None:
            run = _mode_detections[key] = _SharedRun(_detect_api_mode)
        return run


class PhaseMiddleware(Middleware):
    """Flip the visible tool surface between design-time and run-time phases.

    ``tools/list`` is filtered to the active phase (Option A, "hide-and-flip"):
    run-time verbs are hidden until a model is loaded. ``tools/call`` carries a
    guard so a stale host that calls a hidden run-time verb gets a structured
    "no model loaded — re-list" error rather than an opaque downstream failure.

    Note: the spec ``ttlMs`` / ``cacheScope`` cache hints on the ``tools/list``
    result (SEP-2549, final 2026-07-28) are not set here — FastMCP's
    ``on_list_tools`` hook only exposes the tool sequence, not the result
    envelope, and the fields are still a release candidate. The explicit
    re-list signal emitted by ``load_model`` / ``remove_model`` is the primary
    transition mechanism (and the plan-preferred one), so this is no loss today.
    """

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        tools = await call_next(context)
        design_phase = _current_phase() == PHASE_DESIGN
        visible = []
        for t in tools:
            # Capability gate (orthogonal to phase): drop verbs the server is
            # configured not to support, in any phase.
            if not _tool_capability_ok(t.name):
                continue
            # Phase gate (a swap): design phase shows lifecycle + design-only;
            # run phase shows lifecycle + run-only (design tools hidden).
            if not _tool_visible_in_phase(t.name, design_phase):
                continue
            visible.append(t)
        return visible

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        name = getattr(context.message, "name", None)
        if name and not _tool_capability_ok(name):
            raise ToolError(
                f"'{name}' is not available — this server is configured without "
                "the required capability (query execution is disabled). Use the "
                "compile_* tools to generate SQL without executing it."
            )
        if name and _is_runtime_tool(name) and _current_phase() == PHASE_DESIGN:
            raise ToolError(
                f"No model loaded — '{name}' is a run-time tool and is not "
                "available yet. Load a model first, then re-list tools: the "
                "run-time tool set (execute_query, describe_model, find_artefacts, "
                "…) becomes available once a model is loaded."
            )
        return await call_next(context)


# One middleware stack for every entrypoint: main() on either transport, and
# the exported ``server.py:mcp`` that Prefect Horizon, ``fastmcp run`` and
# in-memory clients serve without main(). In this order: the version gate,
# so nothing is detected or served on another API minor; tool registration;
# the phase filter, which needs the registered tools.
mcp.add_middleware(ApiVersionGateMiddleware())
mcp.add_middleware(LazyInitMiddleware())
mcp.add_middleware(PhaseMiddleware())


# ---------------------------------------------------------------------------
# HTTP client & session management
# ---------------------------------------------------------------------------


class ApiTLSConfigError(ValueError):
    """``API_CLIENT_CERT`` / ``API_CLIENT_KEY`` / ``API_CA_CERT`` are unusable."""


class _EncryptedKeyError(Exception):
    """Raised by the password callback: the key is encrypted and we never prompt."""


def _refuse_password_prompt() -> NoReturn:
    # Without a callback OpenSSL prompts on the terminal for an encrypted key —
    # and under the stdio transport stdin is the MCP pipe, so the prompt would
    # read protocol bytes or hang. OpenSSL calls this only when a key is
    # encrypted, which is what lets the error below say so.
    raise _EncryptedKeyError


def _ssl_reason(exc: BaseException) -> str:
    """The useful half of an ssl/OS error, without the file path repeated."""
    reason = getattr(exc, "reason", None) or getattr(exc, "strerror", None)
    return str(reason or exc).strip() or exc.__class__.__name__


def _api_tls_verify() -> ssl.SSLContext | bool:
    """The ``verify`` value for the API client, built from the ``API_*`` TLS settings.

    ``True`` when none is set: httpx's default trust (certifi, or
    ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``) and no client certificate, exactly as
    before these settings existed. Never ``False`` — there is deliberately no
    way to switch verification off.

    Every failure names the setting and the file. Otherwise a path that is
    missing, unreadable, or not what it claims surfaces as an SSL error from
    inside httpx on the first request, naming neither.
    """
    if settings.api_client_key and not settings.api_client_cert:
        raise ApiTLSConfigError(
            "API_CLIENT_KEY is set without API_CLIENT_CERT. A private key alone "
            "cannot identify a client; set the certificate too."
        )

    paths: dict[str, str | None] = {}
    for name, value in (
        ("API_CLIENT_CERT", settings.api_client_cert),
        ("API_CLIENT_KEY", settings.api_client_key),
        ("API_CA_CERT", settings.api_ca_cert),
    ):
        if not value:
            paths[name] = None
            continue
        # Validate and use the *expanded* path: checking ~/ca.pem and then
        # handing OpenSSL the literal string would pass here and fail later.
        path = Path(value).expanduser()
        if not path.is_file():
            raise ApiTLSConfigError(f"{name}: no such file: {path}")
        if not os.access(path, os.R_OK):
            raise ApiTLSConfigError(f"{name}: not readable: {path}")
        paths[name] = str(path)

    cert, key, ca = paths["API_CLIENT_CERT"], paths["API_CLIENT_KEY"], paths["API_CA_CERT"]
    if cert is None and ca is None:
        return True

    # Certificate material on a plaintext URL would be silently ignored: no
    # handshake, so nothing presented and nothing verified. A configuration
    # that reads as TLS and is not is worse than one that refuses to start.
    if urlsplit(settings.api_base_url).scheme.lower() != "https":
        configured = ", ".join(name for name in ("API_CLIENT_CERT", "API_CA_CERT") if paths[name])
        raise ApiTLSConfigError(
            f"{configured} set, but API_BASE_URL is not https:// "
            f"({settings.api_base_url}) — the connection would be plaintext and "
            "the certificate settings ignored."
        )

    # Two constructions, and the split matters. Without API_CA_CERT we must
    # land on *httpx's* default trust, not ssl's: ssl.create_default_context()
    # uses OpenSSL's own store, which on some platforms is close to empty, so
    # adding a client certificate would silently change which authorities the
    # server is checked against. With API_CA_CERT we replace the store on
    # purpose — the construction httpx's verify=<str> deprecation points at.
    try:
        if ca:
            context = ssl.create_default_context(cafile=ca)
        else:
            context = httpx.create_ssl_context(verify=True)
    except (ssl.SSLError, OSError) as exc:
        raise ApiTLSConfigError(
            f"API_CA_CERT: not a usable PEM CA bundle ({_ssl_reason(exc)}): {ca}"
        ) from None
    if cert:
        names = "API_CLIENT_CERT/API_CLIENT_KEY" if key else "API_CLIENT_CERT"
        try:
            context.load_cert_chain(cert, key, password=_refuse_password_prompt)
        except _EncryptedKeyError:
            raise ApiTLSConfigError(
                f"{names}: the private key is encrypted. Password-protected keys "
                "are not supported; provide the key decrypted, protected by file "
                "permissions or a secret mount."
            ) from None
        except (ssl.SSLError, OSError) as exc:
            raise ApiTLSConfigError(
                f"{names}: could not load the certificate ({_ssl_reason(exc)})"
            ) from None
    return context


def _api_tls_summary() -> str:
    """One line for the startup banner describing the API hop's TLS."""
    if urlsplit(settings.api_base_url).scheme.lower() != "https":
        return "none (plaintext http://)"
    trust = "API_CA_CERT" if settings.api_ca_cert else "default trust store"
    client = "client certificate" if settings.api_client_cert else "no client certificate"
    return f"verified against {trust}, {client}"


def _get_client() -> httpx.Client:
    """Get or create the shared httpx client.

    Raises :class:`ApiTLSConfigError` when the client-TLS settings are unusable;
    ``main`` calls this before serving so that surfaces as a startup failure.
    """
    global _http_client
    if _http_client is None:
        with _state_lock:
            if _http_client is None:  # double-check under lock
                headers = {"User-Agent": f"OrionBelt-MCP/{__version__}"}
                # api_key: one static credential, a default header. The oidc
                # modes put a bearer token on each request (``_ObslAuth``) —
                # in oidc_user the current caller's.
                if _auth_mode() == AUTH_API_KEY and settings.api_key:
                    headers[settings.api_key_header] = settings.api_key
                _http_client = httpx.Client(
                    base_url=settings.api_base_url,
                    timeout=settings.api_timeout,
                    headers=headers,
                    auth=_ObslAuth(),
                    # Built once, here: the context validated is the one on the
                    # wire. A context rather than cert=, which httpx 0.28
                    # deprecates.
                    verify=_api_tls_verify(),
                )
    return _http_client


def _transport_error_message(exc: httpx.TransportError) -> str:
    """Say why the API could not be reached, naming the setting when it is TLS.

    Against an API that serves TLS itself (v2.29+) the two likeliest failures
    are ours to fix, and neither reads as a TLS problem if left raw: an
    untrusted certificate is a ``ConnectError`` indistinguishable from a
    stopped service, and a refused client certificate arrives under TLS 1.3
    after the handshake has already "succeeded" — as the server dropping the
    connection with no response.
    """
    url = settings.api_base_url
    text = str(exc)
    upper = text.upper()
    if "CERTIFICATE_VERIFY_FAILED" in upper:
        return (
            f"The API's TLS certificate at {url} is not trusted ({text}). If it is "
            "issued by a private CA, set API_CA_CERT to that CA's certificate."
        )
    if "CERTIFICATE_REQUIRED" in upper:
        return (
            f"The API at {url} requires a client certificate and none was "
            "presented. Set API_CLIENT_CERT and API_CLIENT_KEY."
        )
    if "ALERT_UNKNOWN_CA" in upper or "ALERT_BAD_CERTIFICATE" in upper:
        return (
            f"The API at {url} rejected the client certificate ({text}). It must "
            "be signed by the CA the API trusts for clients (API_TLS_CLIENT_CA)."
        )
    if "SSL" in upper or "TLS" in upper:
        return f"TLS handshake with the API at {url} failed: {text}"
    if isinstance(exc, httpx.ConnectError):
        return f"Cannot connect to OrionBelt Semantic Layer API at {url}"
    message = f"Connection to the API at {url} failed: {text.rstrip('.') or type(exc).__name__}"
    if urlsplit(url).scheme.lower() == "https" and not settings.api_client_cert:
        message += (
            ". If the API requires a client certificate (API_TLS_CLIENT_CA on the "
            "API, or a gateway in front), set API_CLIENT_CERT and API_CLIENT_KEY."
        )
    return message


def _create_api_session() -> str:
    """Create a new API session and return its session_id."""
    client = _get_client()
    try:
        resp = client.post(f"{_API_V1}/sessions", json={"metadata": {"source": "mcp"}})
        resp.raise_for_status()
    except httpx.TimeoutException:
        raise ToolError("API request timed out while creating session") from None
    except httpx.TransportError as exc:  # after TimeoutException, its subclass
        raise ToolError(_transport_error_message(exc)) from None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            detail = _parse_error_detail(exc.response)
            retry_after = exc.response.headers.get("Retry-After", "60")
            raise ToolError(
                f"Session creation rate-limited: {detail} (retry after {retry_after}s)"
            ) from None
        _raise_api_error(exc.response)
    data = _parse_json(resp)
    return data["session_id"]


def _ensure_session() -> str:
    """Return the current identity's session ID, creating one if needed."""
    state = _identity()
    session_id = state.session_id
    if session_id is None:
        with state.lock:
            session_id = state.session_id
            if session_id is None:  # double-check under the identity's lock
                session_id = state.session_id = _create_api_session()
                logger.info("Created API session: %s", session_id)
    return session_id


def _invalidate_session() -> None:
    """Clear the current identity's session ID (e.g. on 404)."""
    state = _identity_or_none()
    if state is not None:
        with _state_lock:
            state.session_id = None


# ---------------------------------------------------------------------------
# API client helpers
# ---------------------------------------------------------------------------


def _parse_json(resp: httpx.Response):
    """Parse JSON from a successful API response, raising ToolError on failure."""
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ToolError(f"API returned invalid JSON: {exc}") from None


def _parse_error_detail(response: httpx.Response) -> str:
    """Extract error detail string from an API error response.

    Picks up the ``message`` field from structured detail dicts
    (``UnsupportedAggregationError``, ``UnsupportedGroupingError``, OBSQL
    translation errors, query resolution errors).  When the dict carries
    typed context (``dialect`` + ``aggregation`` / ``grouping``, or a list
    of ``errors`` with ``code`` / ``message``), it is appended to the
    message so the LLM sees the structured fields without parsing JSON.

    Also handles the top-level ``{message, errors, warnings}`` envelope the
    API uses for ``UNKNOWN_PROPERTY`` (Pydantic ``extra="forbid"`` 422s) —
    no ``detail`` wrapper there, so we promote the body itself.

    Each nested error is rendered whole — ``path``, ``context``, ``hint`` and
    the did-you-mean ``suggestions`` — one per line. Since API 3.0 every
    compile refusal and model-load error carries them, so an agent can correct
    a refused query from the error alone instead of guessing.
    """
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" not in body and isinstance(body.get("errors"), list):
            detail: object = body
        else:
            detail = body.get("detail", response.text) if isinstance(body, dict) else response.text
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or str(detail)
            # UnsupportedAggregationError / UnsupportedGroupingError shape
            dialect = detail.get("dialect")
            aggregation = detail.get("aggregation")
            grouping = detail.get("grouping")
            if dialect and (aggregation or grouping):
                tag = aggregation or grouping
                kind = "aggregation" if aggregation else "grouping"
                message = f"{message} ({kind}={tag!r}, dialect={dialect!r})"
            # Nested error list (ResolutionError, FanoutError, SQLTranslationError,
            # model validation, UNKNOWN_PROPERTY).
            errors = detail.get("errors")
            if isinstance(errors, list) and errors:
                rendered = [
                    _format_warning({"severity": "error", **e})
                    for e in errors
                    if isinstance(e, dict)
                ]
                if rendered:
                    header = str(detail.get("error") or message)
                    # A model-load 422 repeats every error on its one-line
                    # ``message`` ("Invalid OBML model: [CODE] ... Hint: ..."),
                    # which the list below renders again: keep the lead-in only.
                    if not detail.get("error") and ": [" in header:
                        header = header.split(": [", 1)[0]
                    message = "\n".join([header, *(f"  {r}" for r in rendered)])
            return str(message)
        return str(detail)
    except (ValueError, json.JSONDecodeError):
        return response.text


def _error_code(response: httpx.Response) -> str | None:
    """The structured ``code`` of an API error (``detail.code`` or top-level), if any."""
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else body.get("code")
    return code if isinstance(code, str) else None


def _holds_bi_token() -> bool:
    """Whether this server's credential is a person's BI token (api_key mode)."""
    return _auth_mode() == AUTH_API_KEY and (settings.api_key or "").startswith(_BI_TOKEN_PREFIX)


# The API's data-source sign-in codes (OBSL 3.2, ``service/user_login.py``): a
# source that logs in to the warehouse *as the user*. Not ``AUTH_*`` codes, so
# they are matched before the "403 without an AUTH_ code is no auth problem" rule.
_SIGN_IN_CODES = frozenset(
    {
        "SIGN_IN_REQUIRED",
        "BI_TOKEN_SIGN_IN_ENDED",
        "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED",
        "DATA_SOURCE_SIGN_IN_REFUSED",
        "DATA_SOURCE_SIGN_IN_UNAVAILABLE",
    }
)


def _sign_in_error(status: int, code: str, detail: str) -> str:
    """A data-source sign-in refusal, with what the caller can do about it."""
    user_mode = _auth_mode() == AUTH_USER
    if code == "SIGN_IN_REQUIRED":
        lead = "This data source queries as the signed-in user"
        if _holds_bi_token():
            advice = (
                "create a BI token with a warehouse sign-in on OBSL's /tokens page "
                "and set it as API_KEY"
            )
        else:
            advice = (
                "use your BI token (with a warehouse sign-in) as API_KEY, or run this "
                "server in the user mode (MCP_AUTH_MODE=oidc_user), not a service key"
            )
    elif code == "BI_TOKEN_SIGN_IN_ENDED":
        lead = "Your BI token's warehouse sign-in has ended"
        advice = "create a new BI token on OBSL's /tokens page and set it as API_KEY"
    elif code == "DATA_SOURCE_SIGN_IN_INTERACTION_REQUIRED":
        lead = "The identity provider wants you to sign in again (MFA or conditional access)"
        advice = (
            "sign in again in your MCP client"
            if user_mode
            else "sign in again on OBSL's /tokens page and create a new BI token"
        ) + "; retrying will not help"
    elif code == "DATA_SOURCE_SIGN_IN_REFUSED":
        # The API's message names the warehouse's reason.
        return f"The data source refused to log you in ({status} {code}): {detail}"
    else:  # DATA_SOURCE_SIGN_IN_UNAVAILABLE
        lead = "The data source's sign-in service is unavailable"
        advice = "retry later"
    return f"{lead} ({status} {code}): {detail} — {advice}"


def _scope_error(response: httpx.Response, detail: str) -> str:
    """A 403 ``AUTH_INSUFFICIENT_SCOPE``, naming the scope the tool needs."""
    scope = _www_authenticate_param(response, "scope")
    if not scope:
        match = re.search(r"requires (\S+)", detail)
        scope = match.group(1) if match else "that scope"
    mode = _auth_mode()
    if mode == AUTH_USER:
        advice = f"ask an administrator to grant you {scope} at the identity provider"
    elif _holds_bi_token():
        advice = f"create a BI token that holds {scope} on OBSL's /tokens page"
    elif mode == AUTH_CLIENT_CREDENTIALS:
        advice = f"grant this server's client {scope} at the identity provider"
    else:
        advice = f"use a credential that holds {scope}"
    return (
        "Your identity lacks the scope this tool needs (403 AUTH_INSUFFICIENT_SCOPE): "
        f"{detail} — {advice}"
    )


def _credential_hint(detail: str) -> str:
    """What to check when the API refuses this server's credential (401/403)."""
    mode = _auth_mode()
    if mode == AUTH_NONE:
        return (
            "set API_KEY to an API key or your BI token from OBSL's /tokens page "
            "(and API_KEY_HEADER if the API uses a custom header) in the MCP environment"
        )
    if mode == AUTH_API_KEY:
        if _holds_bi_token() and "BI token" in detail:
            return "create a new BI token on OBSL's /tokens page and set it as API_KEY"
        return "verify API_KEY matches a key configured on the API, or is a current BI token"
    if mode == AUTH_CLIENT_CREDENTIALS:
        return (
            "the operator must check that OBSL_SCOPE / OBSL_AUDIENCE ask the identity "
            "provider for a token with OBSL's OIDC_ISSUER and OIDC_AUDIENCE"
        )
    return (
        "sign in again in your MCP client; if that does not help, the operator must "
        "check that OBSL_SCOPE / OBSL_AUDIENCE ask for a token with OBSL's OIDC_AUDIENCE"
    )


def _raise_api_error(response: httpx.Response, detail: str | None = None) -> NoReturn:
    """Raise ToolError from an API error response. Never includes a credential."""
    if detail is None:
        detail = _parse_error_detail(response)
    status = response.status_code
    code = _error_code(response)
    if code in _SIGN_IN_CODES:
        raise ToolError(_sign_in_error(status, code, detail))
    if status == 403 and code == "AUTH_INSUFFICIENT_SCOPE":
        raise ToolError(_scope_error(response, detail))
    # Otherwise the API's auth layer always sends an ``AUTH_*`` code. A 403 with
    # any other code is the model or server refusing the request
    # (``VALUE_SEARCH_DISABLED`` for a ``valueSearch: false`` dimension), which
    # no credential fixes.
    if status == 403 and code is not None and not code.startswith("AUTH_"):
        raise ToolError(f"API refused the request (403 {code}): {detail}")
    if status in (401, 403):
        raise ToolError(
            f"API authentication failed ({status}): {detail} — {_credential_hint(detail)}"
        )
    raise ToolError(f"API error ({status}): {detail}")


# "Session 'abc' not found" (current API) or "Session not found" (older ones).
_SESSION_NOT_FOUND_RE = re.compile(r"session(?: '[^']*')? not found", re.IGNORECASE)


def _is_session_expired(response: httpx.Response) -> bool:
    """Return True if the API error indicates an expired/missing session.

    Matches 410 (Gone) for explicitly expired sessions, and 404 with
    session-related detail for backwards compatibility with older API versions.
    """
    # 410 Gone — API >= 1.4 uses this for expired sessions
    if response.status_code == 410:
        return True
    if response.status_code != 404:
        return False
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return False
    # Prefer structured error code when available
    if body.get("code") == "SESSION_NOT_FOUND":
        return True
    # Fallback: the API's own wording for a missing session, and nothing else.
    # A loose "session" + "not found" match misread artefact misses such as
    # "Dimension 'Session Count' not found" and replaced a healthy session.
    return bool(_SESSION_NOT_FOUND_RE.match(str(body.get("detail", "")).strip()))


def _do_request(
    client: httpx.Client,
    method: str,
    path: str,
    json_body: dict | None,
    params: dict[str, Any] | None = None,
    *,
    public: bool = False,
) -> httpx.Response:
    """Execute a single HTTP request, wrapping connection/timeout errors.

    ``public``: the API answers it without a credential (``/health``), so none
    is obtained for it.
    """
    extensions = {_PUBLIC_REQUEST: True} if public else None
    try:
        return client.request(method, path, json=json_body, params=params, extensions=extensions)
    except httpx.TimeoutException:
        raise ToolError("API request timed out") from None
    except httpx.TransportError as exc:  # after TimeoutException, its subclass
        raise ToolError(_transport_error_message(exc)) from None


def _api_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict[str, Any] | None = None,
    retry_on_expired: bool = True,
    path_suffix: str | None = None,
) -> httpx.Response:
    """Make an API request with auto-session retry.

    If the session returns 404/410 and retry_on_expired is True,
    re-create the session and retry once.  When *path_suffix* is provided,
    the retry reconstructs the path from the new session ID.
    """
    client = _get_client()
    resp = _do_request(client, method, path, json_body, params=params)

    if _is_session_expired(resp) and retry_on_expired and path_suffix is not None:
        # Session expired — recreate and retry once
        _invalidate_session()
        sid = _ensure_session()
        new_path = f"{_API_V1}/sessions/{sid}{path_suffix}"
        resp = _do_request(client, method, new_path, json_body, params=params)

    if resp.status_code >= 400:
        _raise_api_error(resp)

    return resp


def _session_request(
    method: str,
    path_suffix: str,
    *,
    json_body: dict | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Make an API request scoped to the current session.

    Automatically ensures a session exists.
    """
    sid = _ensure_session()
    path = f"{_API_V1}/sessions/{sid}{path_suffix}"
    return _api_request(
        method,
        path,
        json_body=json_body,
        params=params,
        path_suffix=path_suffix,
    )


def _shortcut_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """Make an API request to a shortcut endpoint (no session required).

    Used in single-model mode where the API auto-resolves session/model.
    """
    client = _get_client()
    full_path = f"{_API_V1}{path}"
    resp = _do_request(client, method, full_path, json_body, params=params)
    if resp.status_code >= 400:
        _raise_api_error(resp)
    return resp


# ---------------------------------------------------------------------------
# Resources & caches
# ---------------------------------------------------------------------------

_obml_reference_cache: str | None = None
_obsql_reference_cache: str | None = None
_dialect_names_cache: list[str] | None = None
_function_catalog_cache: dict[str, Any] | None = None


def _fetch_obml_reference() -> str:
    """Fetch and cache the OBML reference from the API."""
    global _obml_reference_cache
    if _obml_reference_cache is None:
        with _state_lock:
            if _obml_reference_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/reference/obml", retry_on_expired=False)
                data = _parse_json(resp)
                _obml_reference_cache = data["reference"]
    return _obml_reference_cache


def _fetch_obsql_reference() -> str:
    """Fetch and cache the OBSQL reference from the API."""
    global _obsql_reference_cache
    if _obsql_reference_cache is None:
        with _state_lock:
            if _obsql_reference_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/reference/obsql", retry_on_expired=False)
                data = _parse_json(resp)
                _obsql_reference_cache = data["reference"]
    return _obsql_reference_cache


def _fetch_dialect_names() -> list[str]:
    """Fetch and cache the list of supported dialect names from the API."""
    global _dialect_names_cache
    if _dialect_names_cache is None:
        with _state_lock:
            if _dialect_names_cache is None:  # double-check under lock
                resp = _api_request("GET", f"{_API_V1}/dialects", retry_on_expired=False)
                data = _parse_json(resp)
                _dialect_names_cache = [d["name"] for d in data.get("dialects", [])]
    return _dialect_names_cache


def _fetch_function_catalog() -> dict[str, Any]:
    """Fetch and cache the portable scalar-function catalog from the API.

    The catalog is static for a given API version, so a single fetch per process
    is enough. ``GET /v1/reference/functions`` only exists on API builds carrying
    the function catalog, so a 404 is translated into a legible message instead
    of a bare HTTP error — an older deployment simply predates the endpoint.
    """
    global _function_catalog_cache
    if _function_catalog_cache is None:
        with _state_lock:
            if _function_catalog_cache is None:  # double-check under lock
                resp = _do_request(_get_client(), "GET", f"{_API_V1}/reference/functions", None)
                if resp.status_code == 404:
                    raise ToolError(
                        "The connected OrionBelt Semantic Layer API predates the portable "
                        "function catalog — GET /v1/reference/functions is not available on "
                        "this deployment. Upgrade the API to a build that publishes the "
                        "catalog, or consult get_obml_reference() for expression syntax."
                    )
                if resp.status_code >= 400:
                    _raise_api_error(resp)
                _function_catalog_cache = _parse_json(resp)
    return _function_catalog_cache


def _format_arity(fn: dict[str, Any]) -> str:
    """Render an entry's arity. ``max_args`` is null for variadic entries."""
    min_args = fn.get("min_args")
    max_args = fn.get("max_args")
    if min_args is None:
        return ""
    unit = "arg" if min_args == 1 else "args"
    if max_args is None:
        return f"[{min_args}+ args]"
    if max_args == min_args:
        return f"[{min_args} {unit}]"
    return f"[{min_args}-{max_args} args]"


@mcp.resource("obml://reference")
def obml_reference() -> str:
    """Full OBML format reference — data objects, dimensions, measures, metrics, joins."""
    return _fetch_obml_reference()


@mcp.resource("obsql://reference")
def obsql_reference() -> str:
    """Full OBSQL grammar reference — natural SQL surface against the semantic model."""
    return _fetch_obsql_reference()


# ---------------------------------------------------------------------------
# Mode-independent tools (always registered)
# ---------------------------------------------------------------------------


@mcp.tool(title="OBML Reference", annotations=_READ_ONLY)
def get_obml_reference() -> str:
    """Get the OBML format reference.

    Returns the full specification with examples for dataObjects (including
    joins defined inside each dataObject), dimensions, measures, metrics, and
    expressions. Use this reference to understand the correct OBML syntax
    before composing models.
    """
    return _fetch_obml_reference()


@mcp.tool(title="Function Catalog", annotations=_READ_ONLY)
def get_function_catalog() -> str:
    """Get the portable scalar-function catalog for OBML expressions.

    A call whose name is **in** the catalog is rendered per dialect with pinned
    semantics, so it means the same thing on every warehouse: ``concat``
    propagates NULL (any NULL argument makes the whole result NULL), ``length``
    counts characters rather than bytes, ``round`` breaks ties away from zero,
    and ``greatest``/``least`` propagate NULL. Do not assume your own
    warehouse's rule — read the ``semantics`` line, which is the part that
    differs between engines.

    A call **outside** the catalog is emitted verbatim into the generated SQL.
    That still works, but it pins the model to the engines that happen to have
    that function, so prefer a catalog entry when one covers the need.

    Calling a catalog function with the wrong number of arguments is a model
    validation error (``WRONG_FUNCTION_ARITY``), caught at load time rather
    than at the warehouse.
    """
    data = _fetch_function_catalog()
    lines = ["Portable scalar functions:", ""]
    groups: dict[str, list[dict[str, Any]]] = {}
    for fn in data.get("functions", []):
        groups.setdefault(fn.get("group") or "other", []).append(fn)
    for group, fns in groups.items():
        lines.append(f"{group}:")
        for fn in fns:
            signature = fn.get("signature") or fn.get("name", "?")
            head = f"  {signature}"
            result_type = fn.get("result_type")
            if result_type:
                head += f" -> {result_type}"
            arity = _format_arity(fn)
            if arity:
                head += f"  {arity}"
            summary = fn.get("summary")
            if summary:
                head += f" — {summary}"
            lines.append(head)
            semantics = fn.get("semantics")
            if semantics:
                lines.append(f"      semantics: {semantics}")
            examples = fn.get("examples") or []
            for ex in examples:
                lines.append(f"      e.g. {ex.get('call')} = {json.dumps(ex.get('expect'))}")
        lines.append("")
    escape_hatch = data.get("escape_hatch")
    if escape_hatch:
        lines.append(f"Escape hatch: {escape_hatch}")
    return "\n".join(lines).rstrip()


@mcp.tool(title="JSON Schema", annotations=_READ_ONLY)
def get_json_schema(name: Literal["obml", "query"]) -> str:
    """Get a published JSON Schema by name.

    Returns the raw JSON Schema document as a JSON string so callers can
    validate documents locally without round-tripping them to the API.

    Args:
        name: Either ``"obml"`` (the OBML model schema) or ``"query"``
            (the QueryObject input to ``execute_query``).
    """
    resp = _api_request("GET", f"{_API_V1}/reference/schemas/{name}", retry_on_expired=False)
    return json.dumps(_parse_json(resp), indent=2)


@mcp.tool(title="List Dialects", annotations=_READ_ONLY)
def list_dialects() -> str:
    """List available SQL dialects, their capabilities, and what each can compute.

    ``supported_aggregations`` is every OBML ``aggregation:`` value the dialect
    can render; ``supported_functions`` is every portable-catalog scalar
    function it can render (see ``get_function_catalog()`` for what each entry
    means). Both are stated positively by the API, so a name absent from a
    dialect's list is a name that dialect cannot compute.
    """
    resp = _api_request("GET", f"{_API_V1}/dialects", retry_on_expired=False)
    data = _parse_json(resp)
    lines = ["Available dialects:", ""]
    for d in data.get("dialects", []):
        caps = d.get("capabilities", {})
        enabled = [k for k, v in caps.items() if v]
        cap_str = ", ".join(enabled) if enabled else "(none)"
        lines.append(f"  {d['name']}: {cap_str}")
        # Rendered on their own indented lines rather than appended inline: the
        # positive form lists most of each vocabulary, which is far too long to
        # read at the end of the capability line.
        aggs = d.get("supported_aggregations") or []
        if aggs:
            lines.append(f"      supported aggregations: {', '.join(aggs)}")
        fns = d.get("supported_functions") or []
        if fns:
            lines.append(f"      supported functions: {', '.join(fns)}")
    return "\n".join(lines)


@mcp.tool(title="List Data Sources", annotations=_READ_ONLY)
def list_datasources() -> str:
    """List the configured data sources a model can run on (API 3.0).

    A model picks one with ``settings.defaultDataSource``; a data object may
    name its own with ``dataSource``, and a query reading data objects of
    several sources is then answered across them (federated). A model naming
    none runs on the sole data source of its dialect. ``load_model``'s
    ``datasource`` overrides the model's choice for that load.

    Shows each source's name, dialect, description and the database, catalog,
    schema or project it reads — never a host, user, password or token.
    """
    resp = _api_request("GET", f"{_API_V1}/datasources", retry_on_expired=False)
    sources = _parse_json(resp).get("datasources") or []
    if not sources:
        return "No data sources are configured on the API."
    lines = ["Configured data sources:", ""]
    for s in sources:
        head = f"  {s.get('name', '?')} ({s.get('dialect', '?')})"
        if s.get("description"):
            head += f" — {s['description']}"
        lines.append(head)
        target = s.get("target") or {}
        if target:
            lines.append("      reads: " + ", ".join(f"{k}={v}" for k, v in target.items()))
    return "\n".join(lines)


@mcp.tool(title="List Data Source Schemas", annotations=_READ_ONLY_OPEN_WORLD)
def list_datasource_schemas(datasource: str) -> str:
    """List the schemas a data source holds, read from its catalog (API 3.1).

    The first step in writing a model from an existing database: pick the
    schema its tables are in, then read them with
    ``inspect_datasource_schema``. A schema is a database on MySQL and
    ClickHouse and a dataset on BigQuery; the engine's own
    (``information_schema``, ``pg_catalog``, ``system``, …) are left out.

    Connects to the database, so it needs query execution enabled on the API.

    Args:
        datasource: A data source name from ``list_datasources``.
    """
    resp = _api_request(
        "GET",
        f"{_API_V1}/datasources/{quote(datasource, safe='')}/schemas",
        retry_on_expired=False,
    )
    data = _parse_json(resp)
    schemas = data.get("schemas") or []
    source = f"{data.get('datasource') or datasource} ({data.get('dialect') or '?'})"
    if not schemas:
        return f"Data source {source} has no schemas this user can see."
    return "\n".join([f"Schemas of data source {source}:", "", *(f"  {s}" for s in schemas)])


@mcp.tool(title="Inspect Data Source Schema", annotations=_READ_ONLY_OPEN_WORLD)
def inspect_datasource_schema(
    datasource: str,
    schema: str,
    tables: list[str] | None = None,
    views: bool = True,
    view_definitions: bool = False,
    names_only: bool = False,
) -> str:
    """Describe the tables and views of a data source's schema, from its catalog (API 3.1).

    What a model's data objects are written from: each table's columns with
    the engine's own type, position, nullability and comment, its primary
    key, and its foreign keys. A table becomes a data object (its name the
    ``code``, the schema its ``schema``), its columns the data object's
    ``columns``, and a foreign key a ``joins`` entry (``columnsFrom`` →
    ``columnsTo``, composite keys paired by position).

    Keys come from declared constraints only: none on Dremio, the primary
    key alone on ClickHouse. Keys or view bodies the catalog will not show
    this user are left out and reported as warnings.

    Connects to the database, so it needs query execution enabled on the API.

    Args:
        datasource: A data source name from ``list_datasources``.
        schema: A schema from ``list_datasource_schemas``.
        tables: Only these tables or views, by exact name. Omit for all.
        views: Include views and materialized views. Default true.
        view_definitions: Include each view's SQL. Default false.
        names_only: List each table's name, kind and column count only: a
            first look at a large schema before inspecting some of its tables.
    """
    params: dict[str, Any] = {"views": str(views).lower()}
    if tables:
        params["table"] = list(tables)
    if view_definitions:
        params["view_definitions"] = "true"
    resp = _api_request(
        "GET",
        f"{_API_V1}/datasources/{quote(datasource, safe='')}/schemas/"
        f"{quote(schema, safe='')}/tables",
        params=params,
        retry_on_expired=False,
    )
    return _render_schema_inspection(_parse_json(resp), tables, names_only=names_only)


def _render_schema_inspection(data: dict, requested: list[str] | None, *, names_only: bool) -> str:
    """Render a ``SchemaInspectionResponse``: one block per table, then warnings."""
    found = data.get("tables") or []
    where = (
        f"schema {data.get('schema') or '?'} of data source "
        f"{data.get('datasource') or '?'} ({data.get('dialect') or '?'})"
    )
    lines = [f"{len(found)} table(s) and view(s) in {where}:" if found else f"Nothing in {where}."]
    missing = sorted(set(requested or ()) - {t.get("name") for t in found})
    if missing:
        lines.append(f"Not found: {', '.join(missing)}")
    if names_only and found:
        lines.append("")
    for table in found:
        columns = table.get("columns") or []
        head = f"{table.get('name')} ({table.get('kind') or 'table'}"
        head += f", {len(columns)} columns)" if names_only else ")"
        if table.get("comment"):
            head += f" — {table['comment']}"
        if names_only:
            lines.append(f"  {head}")
            continue
        lines.append("")
        lines.append(head)
        for col in columns:
            line = f"  {col.get('ordinal')}. {col.get('name')}: {col.get('dataType')}"
            if col.get("nullable") is False:
                line += ", not null"
            if col.get("primaryKey"):
                line += ", PK"
            if col.get("comment"):
                line += f" — {col['comment']}"
            lines.append(line)
        if table.get("primaryKey"):
            lines.append(f"  primary key: ({', '.join(table['primaryKey'])})")
        for fk in table.get("foreignKeys") or []:
            target = ".".join(p for p in (fk.get("referredSchema"), fk.get("referredTable")) if p)
            line = (
                f"  foreign key: ({', '.join(fk.get('columns') or [])}) → "
                f"{target} ({', '.join(fk.get('referredColumns') or [])})"
            )
            if fk.get("name"):
                line += f" [{fk['name']}]"
            lines.append(line)
        if table.get("viewDefinition"):
            lines.append("  definition:")
            lines.extend(f"    {sql}" for sql in str(table["viewDefinition"]).splitlines())
    warnings = data.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {w}" for w in warnings)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Implementation functions (shared logic for both modes)
# ---------------------------------------------------------------------------


def _aliased(rec: dict[str, Any], alias: str, snake: str) -> Any:
    """Read one field, tolerating either spelling.

    FastAPI serialises a response model **by alias**, so a Pydantic field
    declared ``data_type: ... = Field(alias="dataType")`` reaches us as
    ``dataType``, while an unaliased sibling like ``result_type`` stays
    snake_case. Only some fields are aliased, so the mixed casing is per-field
    rather than per-payload and cannot be normalised wholesale. Reading the
    alias first with a snake_case fallback also keeps the renderers working
    against a payload dumped by field name (``model_dump()`` without
    ``by_alias``), which is how several tests and the OBML round-trip spell it.
    """
    val = rec.get(alias)
    return rec.get(snake) if val is None else val


def _default_value_tag(rec: dict) -> str:
    """Render a measure's ``defaultValue`` when set, else an empty string.

    The value reported when the aggregate has nothing to add up; unset means the
    SQL-standard NULL. It changes the answer a filtered measure gives, so it
    belongs in any description of the measure. ``0`` and ``False`` are
    meaningful values here, hence the check against ``None`` rather than
    falsiness, and the value is JSON-rendered so ``0`` and ``"0"`` stay
    distinguishable.
    """
    val = _aliased(rec, "defaultValue", "default_value")
    if val is None:
        return ""
    return f"  defaultValue: {json.dumps(val)}"


# A context value at least this long that the message already holds is not repeated.
_CONTEXT_REPEAT_MIN = 80


def _format_warning(w: Any) -> str:
    """Render a single warning. Accepts the structured ``StructuredWarning``
    shape introduced in API v2.2 (object with ``code``/``severity``/
    ``message``/``path``/``hint``) and the legacy plain-string shape.

    Also renders ``suggestions`` and ``context``, which the validation
    endpoints' ``ErrorDetail`` carries and ``StructuredWarning`` does not. They
    are the actionable half of several codes — ``UNKNOWN_COLUMN`` puts the
    candidate names it matched against in ``suggestions``, and the
    ``DATASOURCE_*`` family names the offending table/column in ``context`` —
    so dropping them left an agent to guess at what the API had already worked
    out. Both are emitted only when present, so every existing call site is
    unchanged for a payload that carries neither.
    """
    if isinstance(w, str):
        return w
    if not isinstance(w, dict):
        return str(w)
    parts: list[str] = []
    code = w.get("code")
    severity = w.get("severity") or "warning"
    message = w.get("message", "")
    if code:
        parts.append(f"[{severity}:{code}]")
    elif severity != "warning":
        parts.append(f"[{severity}]")
    if message:
        parts.append(message)
    if w.get("path"):
        parts.append(f"(at {w['path']})")
    context = w.get("context")
    if isinstance(context, dict):
        # A driver's error rides in both the message and ``context.error``
        # (API 3.1's DATASOURCE_* codes); print a long one once.
        shown = {
            k: v
            for k, v in context.items()
            if not (isinstance(v, str) and len(v) > _CONTEXT_REPEAT_MIN and v in message)
        }
        if shown:
            parts.append(f"[{', '.join(f'{k}={v}' for k, v in shown.items())}]")
    if w.get("hint"):
        parts.append(f"— hint: {w['hint']}")
    suggestions = w.get("suggestions")
    if suggestions:
        parts.append(f"— did you mean: {', '.join(str(x) for x in suggestions)}?")
    return " ".join(parts)


def _format_warnings(items: list | None, indent: str = "  warnings: ") -> list[str]:
    """Render a list of warnings as one or more output lines.

    Returns an empty list when ``items`` is falsy. The first line uses
    ``indent``; subsequent warnings get a continuation indent.
    """
    if not items:
        return []
    rendered = [_format_warning(w) for w in items]
    if len(rendered) == 1:
        return [f"{indent}{rendered[0]}"]
    pad = " " * len(indent)
    lines = [f"{indent}{rendered[0]}"]
    lines.extend(f"{pad}{r}" for r in rendered[1:])
    return lines


def _format_metric_summary(met: dict) -> str:
    """Format a one-line summary for a metric (derived, cumulative, PoP, window, reaggregate)."""
    met_type = met.get("type", "derived")
    # Reaggregate (API 2.35): neither schema response carries `per` or the
    # second-stage `aggregation`, so the measure is all there is to show;
    # explain_artefact lists both stages.
    if met_type == "reaggregate":
        return f"type: reaggregate, measure: {met.get('measure') or '?'}"
    # partitionBy is shared between cumulative and window metrics (v2.6+).
    partition_by = met.get("partition_by") or met.get("partitionBy") or []
    if met_type == "cumulative":
        parts = [f"type: cumulative, measure: {met.get('measure', '?')}"]
        time_dim = _aliased(met, "timeDimension", "time_dimension")
        if time_dim:
            parts.append(f"timeDimension: {time_dim}")
        if met.get("cumulative_type") and met["cumulative_type"] != "sum":
            parts.append(f"cumulativeType: {met['cumulative_type']}")
        if met.get("window"):
            parts.append(f"window: {met['window']}")
        if met.get("grain_to_date"):
            parts.append(f"grainToDate: {met['grain_to_date']}")
        if partition_by:
            parts.append(f"partitionBy: [{', '.join(partition_by)}]")
        return ", ".join(parts)
    if met_type == "period_over_period":
        parts = [f"type: period_over_period, expr: {met.get('expression', '?')}"]
        pop = met.get("period_over_period") or {}
        if pop.get("time_dimension"):
            parts.append(f"timeDimension: {pop['time_dimension']}")
        if pop.get("grain"):
            parts.append(f"grain: {pop['grain']}")
        if pop.get("offset_grain"):
            parts.append(f"offsetGrain: {pop['offset_grain']}")
        if pop.get("comparison"):
            parts.append(f"comparison: {pop['comparison']}")
        return ", ".join(parts)
    if met_type == "window":
        fn = met.get("window_function") or met.get("windowFunction") or "?"
        parts = [f"type: window, windowFunction: {fn}"]
        if met.get("measure"):
            parts.append(f"measure: {met['measure']}")
        time_dim = _aliased(met, "timeDimension", "time_dimension")
        if time_dim:
            parts.append(f"timeDimension: {time_dim}")
        if partition_by:
            parts.append(f"partitionBy: [{', '.join(partition_by)}]")
        order_dir = met.get("order_direction") or met.get("orderDirection")
        if order_dir and order_dir != "desc":
            parts.append(f"orderDirection: {order_dir}")
        if met.get("offset") is not None:
            parts.append(f"offset: {met['offset']}")
        if met.get("buckets") is not None:
            parts.append(f"buckets: {met['buckets']}")
        default_val = met.get("default_value")
        if default_val is None:
            default_val = met.get("defaultValue")
        if default_val is not None:
            parts.append(f"defaultValue: {default_val}")
        return ", ".join(parts)
    return f"expr: {met.get('expression', '?')}"


def _fetch_effective_settings(model_id: str | None = None) -> dict[str, Any]:
    """Best-effort fetch of the server-resolved dialect/timezone and the
    model's ``settings:`` block.

    ``dialect.effective`` / ``timezone.effective`` are the agent-relevant fields
    the removed ``get_settings`` tool used to surface; folding them into
    ``describe_model`` / ``load_model`` keeps them available as *data* without a
    standalone settings verb.

    ``model_settings`` comes along because **no schema response carries it**.
    Neither ``ModelDescription`` (the session ``/models/{id}`` summary) nor
    ``SchemaResponse`` (``/schema``, both modes) has a ``settings`` field, so
    ``/v1/settings`` is the only route that reports it — scoped by
    ``session_id`` + ``model_id``, which is what pins it to one model when a
    session holds several.

    Returns a small dict (possibly empty). Enrichment only — any error is
    swallowed so it can never break the calling tool.
    """
    try:
        params: dict[str, str] = {}
        state = _identity_or_none()
        session_id = state.session_id if state is not None else None
        if not _single_model_mode and session_id is not None:
            params["session_id"] = session_id
            # model_id is rejected without a session_id, and the block is
            # omitted rather than guessed when a session holds more than one
            # model — so scoping it is what makes the settings block appear.
            if model_id:
                params["model_id"] = model_id
        resp = _api_request(
            "GET",
            f"{_API_V1}/settings",
            retry_on_expired=False,
            params=params or None,
        )
        data = _parse_json(resp)
    except Exception:  # noqa: BLE001 — enrichment must never break the caller
        return {}
    out: dict[str, Any] = {}
    dialect = data.get("dialect") or {}
    if dialect.get("effective"):
        out["dialect"] = dialect["effective"]
    tz = data.get("timezone") or {}
    if tz.get("effective"):
        out["timezone"] = tz["effective"]
    model_settings = data.get("model_settings")
    if isinstance(model_settings, dict):
        out["model_settings"] = model_settings
    return out


def _impl_describe_model(model_id: str | None = None) -> str:
    """Describe the contents of a loaded model (shared implementation)."""
    if model_id is None:
        # Single-model shortcut — GET /v1/schema returns SchemaResponse
        resp = _shortcut_request("GET", "/schema")
        desc = _parse_json(resp)
        mid = desc.get("model_id", "default")
    else:
        # /schema, not the bare /models/{id}. The latter returns the
        # ``ModelDescription`` *summary* dataclass, whose MeasureInfo carries
        # only name/type/aggregation/expression/synonyms — no dataType, no
        # defaultValue, no columns, no total — and which has no filters,
        # extends or inherits either. /schema returns the same SchemaResponse
        # the single-model shortcut does, so both modes describe a model from
        # one shape and the renderers below need no per-mode branch.
        resp = _session_request("GET", f"/models/{model_id}/schema")
        desc = _parse_json(resp)
        mid = desc.get("model_id", model_id)

    lines: list[str] = [f"Model {mid}:", ""]

    # Composition
    extends = desc.get("extends", [])
    inherits = desc.get("inherits")
    if extends:
        lines.append(f"EXTENDS: {', '.join(extends)}")
    if inherits:
        lines.append(f"INHERITS: {inherits}")
    if extends or inherits:
        lines.append("")

    # Data objects
    lines.append("DATA OBJECTS:")
    for obj in desc.get("data_objects", []):
        # SchemaResponse uses 'name'; ModelDescription uses 'label'
        obj_name = obj.get("label", obj.get("name", "?"))
        lines.append(f"  {obj_name}  (code: {obj.get('code', '?')})")
        if obj.get("description"):
            lines.append(f"    description: {obj['description']}")
        # API 3.0: the data source the object's table is read from, when it
        # names its own (``dataSource``); else the model's defaultDataSource.
        data_source = _aliased(obj, "dataSource", "data_source")
        if data_source:
            lines.append(f"    data source: {data_source}")
        # SchemaResponse: columns is list[ColumnDetail dict]; describe: list[str]
        cols = obj.get("columns", [])
        # A column's held grain (API 2.35, ColumnDetail.grain) refuses finer
        # `dimension:grain` reads, so it is worth showing beside the name.
        col_names = (
            [f"{c['name']} (grain: {c['grain']})" if c.get("grain") else c["name"] for c in cols]
            if cols and isinstance(cols[0], dict)
            else cols
        )
        lines.append(f"    columns: {', '.join(col_names)}")
        if obj.get("join_targets"):
            lines.append(f"    joins to: {', '.join(obj['join_targets'])}")
        if obj.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(obj['synonyms'])}")
        lines.extend(_render_mapping_lines(obj.get("external_concept_mappings"), "    "))
    lines.append("")

    # Dimensions
    lines.append("DIMENSIONS:")
    for dim in desc.get("dimensions", []):
        grain = f"  grain={dim['time_grain']}" if dim.get("time_grain") else ""
        via = f"  via {dim['via']}" if dim.get("via") else ""
        if dim.get("path_name"):
            via += f" path {dim['path_name']}"
        d_name = dim.get("name", "?")
        d_type = dim.get("result_type", "?")
        d_obj = dim.get("data_object", "?")
        d_col = dim.get("column", "?")
        lines.append(f"  {d_name}  ({d_type}, {d_obj}.{d_col}{grain}{via})")
        if dim.get("description"):
            lines.append(f"    description: {dim['description']}")
        if dim.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(dim['synonyms'])}")
        if dim.get("value_search") is False:
            lines.append("    valueSearch: false (values not listable)")
        lines.extend(_render_mapping_lines(dim.get("external_concept_mappings"), "    "))
    lines.append("")

    # Measures
    lines.append("MEASURES:")
    for m in desc.get("measures", []):
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        m_name = m.get("name", "?")
        m_type = m.get("result_type", "?")
        m_agg = m.get("aggregation", "?")
        m_dtype = _aliased(m, "dataType", "data_type")
        dtype = f"  dataType: {m_dtype}" if m_dtype else ""
        default_val = _default_value_tag(m)
        lines.append(f"  {m_name}  ({m_type}, {m_agg}{expr}{dtype}{default_val})")
        # Two-column statistical aggregates (corr, covar_*, regr_*) — column
        # order is significant in the compiled SQL, so surface it explicitly.
        cols = m.get("columns") or []
        if len(cols) > 1 and not m.get("expression"):
            col_refs = [
                f"{c.get('data_object') or c.get('dataObject', '?')}.{c.get('column', '?')}"
                if isinstance(c, dict)
                else str(c)
                for c in cols
            ]
            lines.append(f"    columns: [{', '.join(col_refs)}]")
        if m.get("description"):
            lines.append(f"    description: {m['description']}")
        if m.get("grain"):
            g = m["grain"]
            g_parts = [f"mode: {g.get('mode', 'RELATIVE')}"]
            if g.get("exclude"):
                g_parts.append(f"exclude: {g['exclude']}")
            if g.get("include"):
                g_parts.append(f"include: {g['include']}")
            if g.get("keepOnly") or g.get("keep_only"):
                g_parts.append(f"keepOnly: {g.get('keepOnly') or g.get('keep_only')}")
            lines.append(f"    grain: {', '.join(g_parts)}")
        if m.get("filter_context") or m.get("filterContext"):
            fc = m.get("filter_context") or m.get("filterContext")
            fc_parts = [f"mode: {fc.get('mode', 'RELATIVE')}"]
            if fc.get("exclude"):
                fc_parts.append(f"exclude: {fc['exclude']}")
            if fc.get("include"):
                fc_parts.append(f"include: {len(fc['include'])} filter(s)")
            if fc.get("keepOnly") or fc.get("keep_only"):
                fc_parts.append(f"keepOnly: {fc.get('keepOnly') or fc.get('keep_only')}")
            lines.append(f"    filterContext: {', '.join(fc_parts)}")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
        lines.extend(_render_mapping_lines(m.get("external_concept_mappings"), "    "))
    lines.append("")

    # Metrics
    metrics = desc.get("metrics", [])
    if metrics:
        lines.append("METRICS:")
        for met in metrics:
            lines.append(f"  {met.get('name', '?')}  {_format_metric_summary(met)}")
            met_dtype = _aliased(met, "dataType", "data_type")
            if met_dtype:
                lines.append(f"    dataType: {met_dtype}")
            if met.get("description"):
                lines.append(f"    description: {met['description']}")
            if met.get("synonyms"):
                lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
            lines.extend(_render_mapping_lines(met.get("external_concept_mappings"), "    "))
        lines.append("")

    # Ontology links (API 2.30): the prefixes compact IRIs expand with, and
    # the mappings declared on the model itself (artefact mappings sit under
    # each artefact above).
    prefixes = desc.get("ontology_prefixes") or {}
    model_mappings = desc.get("external_concept_mappings") or []
    if prefixes or model_mappings:
        lines.append("ONTOLOGY:")
        for prefix, namespace in prefixes.items():
            lines.append(f"  prefix {prefix}: {namespace}")
        lines.extend(_render_mapping_lines(model_mappings, "  "))
        lines.append("")

    # Business rules (API 2.30) come from their own endpoint. Best-effort: a
    # model without rules, or an older API, changes nothing here.
    rule_stats = _fetch_rule_statistics(model_id)
    if rule_stats.get("total"):
        by_type = ", ".join(f"{k} {v}" for k, v in (rule_stats.get("by_type") or {}).items())
        executable = rule_stats.get("executable", 0)
        lines.append(
            f"RULES: {rule_stats['total']} ({by_type}; executable {executable})"
            "  -- see list_rules / explain_rule / evaluate_rule"
        )
        lines.append("")

    # Model settings + server-resolved dialect/timezone, both from
    # /v1/settings — no schema response carries a `settings` block, in either
    # mode. Enrichment: omitted entirely on error, never fatal.
    effective = _fetch_effective_settings(model_id)
    model_settings = effective.get("model_settings") or {}
    if model_settings:
        lines.append("SETTINGS:")
        for label, alias, snake in (
            ("defaultDialect", "defaultDialect", "default_dialect"),
            # API 3.0: the configured data source (list_datasources) queries run on.
            ("defaultDataSource", "defaultDataSource", "default_data_source"),
            ("defaultNumericDataType", "defaultNumericDataType", "default_numeric_data_type"),
            ("defaultTimezone", "defaultTimezone", "default_timezone"),
            ("defaultLocale", "defaultLocale", "default_locale"),
            ("queryTimezone", "queryTimezone", "query_timezone"),
            # weekStart and expressionMode carry server-side defaults
            # ('monday', 'permissive'), so the API reports them whether or not
            # the model wrote them — the answer is what applies, not what was
            # stated. Both change results: weekStart moves every weekly bucket,
            # and expressionMode decides whether a function outside the
            # portable catalog is a warning or a hard error (see
            # get_function_catalog()).
            ("weekStart", "weekStart", "week_start"),
            ("expressionMode", "expressionMode", "expression_mode"),
        ):
            val = _aliased(model_settings, alias, snake)
            if val:
                lines.append(f"  {label}: {val}")
        if _aliased(model_settings, "overrideDatabaseTimezone", "override_database_timezone"):
            lines.append("  overrideDatabaseTimezone: true")
        lines.append("")

    # What the server *resolved* after factoring in env / host / database — the
    # SETTINGS block above is what the model *requested*.
    if effective.get("dialect") or effective.get("timezone"):
        lines.append("EFFECTIVE (server-resolved):")
        if effective.get("dialect"):
            lines.append(f"  dialect:  {effective['dialect']}")
        if effective.get("timezone"):
            lines.append(f"  timezone: {effective['timezone']}")
        lines.append("")

    # Static filters
    filters = desc.get("filters", [])
    if filters:
        lines.append("STATIC FILTERS (applied to every query):")
        for f in filters:
            val = f.get("value")
            vals = f.get("values")
            if vals:
                val_str = f"values: {vals}"
            elif val is not None:
                val_str = f"value: {val}"
            else:
                val_str = ""
            # enforce: always (API 2.35) refuses a query that cannot reach the
            # filter instead of skipping it; the default 'relevant' is not shown.
            enforce = f"  [enforce: {f['enforce']}]" if f.get("enforce") == "always" else ""
            lines.append(
                f"  {f.get('data_object', '?')}.{f.get('column', '?')} "
                f"{f.get('operator', '?')} {val_str}{enforce}"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query results: fetched as the API's Arrow frame, returned as its JSON
# ---------------------------------------------------------------------------
#
# ``execute_query`` asks the API for ``format=arrow`` and builds the JSON
# response here, so turning rows into JSON is this server's work, not the
# shared API's. On a result-cache hit the API then sends the stored bytes
# without decoding them, where the JSON shape costs it a gunzip, an Arrow
# decode and a per-cell conversion; the frame is also several times smaller
# on the wire. Value-formatted and TSV results are still rendered by the API,
# which needs the Python rows for those anyway.
#
# The frame is ``[u32 big-endian json_len][JSON envelope][gzip'd Arrow IPC
# stream]``. The envelope is the JSON response minus ``rows``; the cells are
# converted exactly as the API's JSON surface converts them
# (``orionbelt.service.db_executor.arrow_to_rows`` + ``_serialize_value``,
# then Pydantic's JSON encoding), so the tool returns what ``format=json``
# would. ``tests/fixtures/execute_arrow`` holds frames recorded from a real
# API beside its JSON for the same query, and the tests hold the two equal.

_ARROW_RESULT_MEDIA_TYPE = "application/vnd.orionbelt.result+arrow"

# Field order of the API's JSON ``QueryExecuteResponse``, so the output reads
# the same as before (``sql`` first, ``rows`` after ``columns``). An envelope
# field not listed here follows in the envelope's own order.
_EXECUTE_RESPONSE_FIELDS = (
    "sql",
    "dialect",
    "columns",
    "rows",
    "row_count",
    "execution_time_ms",
    "timezone",
    "resolved",
    "warnings",
    "sql_valid",
    "explain",
    "physical_tables",
    "cached",
    "cached_at",
    "ttl_seconds",
    "ttl_source",
    "ttl_limiting_table",
    "federation",
)

# A numeric type name an Arrow extension type stores as text (ADBC's
# PostgreSQL ``NUMERIC``), as the API's ``is_numeric_type_hint`` matches it.
_NUMERIC_TYPE_TOKENS = ("number", "int", "float", "decimal", "numeric", "double", "real")


def _arrow_text_numeric(arrow_type: Any) -> bool:
    """An Arrow extension type wrapping a numeric SQL type as a string.

    ADBC's PostgreSQL driver keeps ``NUMERIC`` beyond ``decimal128`` as
    ``arrow.opaque[storage_type=string, type_name=numeric]``; the API parses
    those cells to ``Decimal`` before serializing them, so this does too.
    """
    import pyarrow as pa

    storage = getattr(arrow_type, "storage_type", None)
    type_name = getattr(arrow_type, "type_name", None)
    if storage is None or type_name is None or not pa.types.is_string(storage):
        return False
    if isinstance(type_name, bytes):
        type_name = type_name.decode("utf-8", "ignore")
    name = str(type_name).lower()
    return any(token in name for token in _NUMERIC_TYPE_TOKENS)


def _arrow_cell(value: Any, tz: ZoneInfo | None) -> Any:
    """One cell as the API's JSON response carries it.

    ``_serialize_value`` followed by Pydantic's JSON encoding: a naive
    timestamp takes the result's zone, UTC is written ``Z``, a ``Decimal`` is
    an exact string, bytes are base64, NaN and infinity are ``null``, and
    anything else (intervals, lists, structs) is its ``str``.
    """
    if value is None or isinstance(value, str | int):  # bool is an int
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return _timestamp_text(value, tz)
    if isinstance(value, dt_time):
        text = value.isoformat()
        return text[:-7] if text.endswith(".000000") else text
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value)  # Decimal, timedelta, list, dict, …


def _timestamp_text(value: datetime, tz: ZoneInfo | None, nanos: int = 0) -> str:
    """A timestamp's ISO text: a naive one takes the result's zone, UTC is ``Z``.

    ``nanos`` (0-999) are the digits below the microsecond, which a Python
    ``datetime`` cannot hold; they follow its fraction, nine digits in all.
    """
    if value.tzinfo is None and tz is not None:
        value = value.replace(tzinfo=tz)
    text = value.isoformat()
    if nanos:
        cut = len(value.replace(tzinfo=None).isoformat())  # the offset follows
        text = text[:cut] + _nano_digits(value.microsecond, nanos) + text[cut:]
    return text[:-6] + "Z" if text.endswith("+00:00") else text


def _nano_digits(microsecond: int, nanos: int) -> str:
    """The text to append to a fraction for 1-999 nanoseconds below ``microsecond``.

    Python writes no fraction at all for a whole second, so then the whole
    nine-digit fraction is added - as ``pandas.Timestamp.isoformat`` writes it.
    """
    return f"{nanos:03d}" if microsecond else f".{nanos:09d}"


def _nanosecond_cells(column: Any, tz: ZoneInfo | None) -> list[Any]:
    """JSON cells of a nanosecond timestamp, time or duration column.

    ``to_pylist()`` turns such values into ``datetime`` / ``time`` /
    ``timedelta`` only when no digit falls below the microsecond, and raises
    otherwise unless pandas is installed (it is not here). So each value is
    split into whole microseconds - floored, so 1969 and negative durations
    keep a positive remainder - and the nanoseconds left over, which are
    appended to the text. A timestamp then reads exactly as the API's JSON
    writes it from a ``pandas.Timestamp`` (``2025-01-02T03:04:05.123456789``).
    A time or duration keeps Python's own text plus the extra digits: the
    API's JSON depends on its environment there (with pandas a time is cut to
    the microsecond and a duration reads ``1 days 02:00:00``, without it the
    API cannot serialize the value at all), and no digit is dropped here.
    """
    import pyarrow as pa

    arrow_type = column.type
    if pa.types.is_timestamp(arrow_type):
        micro_type = pa.timestamp("us", tz=arrow_type.tz)
    elif pa.types.is_time64(arrow_type):
        micro_type = pa.time64("us")
    else:
        micro_type = pa.duration("us")
    split = [None if v is None else divmod(v, 1000) for v in column.cast(pa.int64()).to_pylist()]
    micros = pa.array([None if s is None else s[0] for s in split], micro_type).to_pylist()

    cells: list[Any] = []
    for value, parts in zip(micros, split, strict=True):
        nanos = 0 if parts is None else parts[1]
        if not nanos:
            cells.append(_arrow_cell(value, tz))
        elif isinstance(value, datetime):
            cells.append(_timestamp_text(value, tz, nanos))
        elif isinstance(value, dt_time):
            cells.append(value.isoformat() + _nano_digits(value.microsecond, nanos))
        else:  # timedelta
            cells.append(str(value) + _nano_digits(value.microseconds, nanos))
    return cells


def _is_nano_temporal(arrow_type: Any) -> bool:
    """A timestamp, time or duration counted in nanoseconds."""
    import pyarrow as pa

    types = pa.types
    return (
        types.is_timestamp(arrow_type)
        or types.is_time64(arrow_type)
        or types.is_duration(arrow_type)
    ) and arrow_type.unit == "ns"


def _is_list_like(arrow_type: Any) -> bool:
    """Any of Arrow's list types: list, large, fixed-size, or a list view."""
    import pyarrow as pa

    types = pa.types
    return (
        types.is_list(arrow_type)
        or types.is_large_list(arrow_type)
        or types.is_fixed_size_list(arrow_type)
        or types.is_list_view(arrow_type)
        or types.is_large_list_view(arrow_type)
    )


def _holds_nanos(arrow_type: Any) -> bool:
    """A nanosecond temporal, or a type with one anywhere inside it.

    Looks through lists, maps, structs, unions and dictionaries: a value of
    any of them goes through ``to_pylist()``, which raises on a nanosecond
    value with digits below the microsecond unless pandas is installed.
    """
    import pyarrow as pa

    types = pa.types
    if _is_nano_temporal(arrow_type):
        return True
    if types.is_dictionary(arrow_type):
        return _holds_nanos(arrow_type.value_type)
    if (
        _is_list_like(arrow_type)
        or types.is_map(arrow_type)  # one field: its key/item struct
        or types.is_struct(arrow_type)
        or types.is_union(arrow_type)
    ):
        return any(_holds_nanos(arrow_type.field(i).type) for i in range(arrow_type.num_fields))
    return False


class _PandasRepr:
    """A nested cell's value that prints as the pandas object the API printed.

    A list or struct cell reaches the API's JSON as Python's ``str`` of the
    value, and with pandas installed pyarrow hands a nanosecond timestamp or
    duration inside it over as ``pandas.Timestamp`` / ``Timedelta``. This
    stands in for one so ``str()`` of the cell reads the same, every digit kept.
    """

    __slots__ = ("_text",)

    def __init__(self, text: str) -> None:
        self._text = text

    def __repr__(self) -> str:
        return self._text


_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FIXED_OFFSET_RE = re.compile(r"^([+-])(\d\d):(\d\d)$")


def _nested_nano_value(raw: int, arrow_type: Any) -> Any:
    """One nanosecond temporal inside a nested cell, from its ``int64``.

    As pandas writes it: ``Timestamp('2025-01-02 03:04:05.123456789')``
    (zone-aware ``...+0100', tz='Europe/Berlin')``, a fixed offset labelled
    ``pytz.FixedOffset(60)``), ``Timedelta('-1 days +23:59:59.999999999')``;
    a time is a ``datetime.time`` cut to the microsecond, as pyarrow gives it.
    """
    import pyarrow as pa

    if pa.types.is_time64(arrow_type):
        micros = raw // 1000
        hours, micros = divmod(micros, 3_600_000_000)
        minutes, micros = divmod(micros, 60_000_000)
        seconds, micros = divmod(micros, 1_000_000)
        return dt_time(hours, minutes, seconds, micros)
    if pa.types.is_duration(arrow_type):
        days, rest = divmod(raw, 86_400_000_000_000)
        hours, rest = divmod(rest, 3_600_000_000_000)
        minutes, rest = divmod(rest, 60_000_000_000)
        seconds, rest = divmod(rest, 1_000_000_000)
        text = f"{days} days{' +' if raw < 0 else ' '}{hours:02}:{minutes:02}:{seconds:02}"
        if rest:
            micro, nanos = divmod(rest, 1000)
            text += f".{micro:06d}" + (f"{nanos:03d}" if nanos else "")
        return _PandasRepr(f"Timedelta('{text}')")

    micros, nanos = divmod(raw, 1000)
    moment = _UTC_EPOCH + timedelta(microseconds=micros)
    label = ""
    if arrow_type.tz:
        fixed = _FIXED_OFFSET_RE.match(arrow_type.tz)
        if fixed:
            sign, hh, mm = fixed.groups()
            offset = (int(hh) * 60 + int(mm)) * (-1 if sign == "-" else 1)
            moment = moment.astimezone(timezone(timedelta(minutes=offset)))
            label = "UTC" if offset == 0 else f"pytz.FixedOffset({offset})"
        else:
            with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
                moment = moment.astimezone(ZoneInfo(arrow_type.tz))
            label = arrow_type.tz
    stamp = f"{moment.year}-{moment:%m-%d %H:%M:%S}"
    if nanos:
        stamp += f".{moment.microsecond:06d}{nanos:03d}"
    elif moment.microsecond:
        stamp += f".{moment.microsecond:06d}"
    if not arrow_type.tz:
        return _PandasRepr(f"Timestamp('{stamp}')")
    return _PandasRepr(f"Timestamp('{stamp}{moment:%z}', tz='{label}')")


def _union_values(array: Any, members: list[list[Any]]) -> list[Any]:
    """Each row of a union array: its member's value, taken from ``members``.

    ``members[i]`` holds the values of child ``i``. A sparse union's children
    run alongside the array; a dense union's are indexed by its offsets. The
    type ids and offsets are read from the buffers, because pyarrow's
    ``type_codes`` and ``offsets`` ignore the array's own slice offset.
    """
    import pyarrow as pa

    buffers = array.buffers()
    size, start = len(array), array.offset
    codes = pa.Array.from_buffers(pa.int8(), size, [None, buffers[1]], offset=start)
    member = {code: i for i, code in enumerate(array.type.type_codes)}
    if array.type.mode == "dense":
        offsets = pa.Array.from_buffers(pa.int32(), size, [None, buffers[2]], offset=start)
        return [
            members[member[code]][at]
            for code, at in zip(codes.to_pylist(), offsets.to_pylist(), strict=True)
        ]
    return [members[member[code]][row] for row, code in enumerate(codes.to_pylist())]


def _nested_values(array: Any) -> list[Any]:
    """``array.to_pylist()``, each nanosecond temporal in it as pandas gives it.

    These are the Python values whose ``str()`` the API writes for a nested
    cell (see :class:`_PandasRepr`). An array holding a nanosecond value is
    taken apart down to it, since ``to_pylist()`` would raise on it; the rest
    is left to ``to_pylist()``.
    """
    import pyarrow as pa

    types = pa.types
    arrow_type = array.type
    if not _holds_nanos(arrow_type):
        return array.to_pylist()
    if _is_nano_temporal(arrow_type):
        return [
            None if raw is None else _nested_nano_value(raw, arrow_type)
            for raw in array.cast(pa.int64()).to_pylist()
        ]
    if types.is_dictionary(arrow_type):
        return _nested_values(array.dictionary_decode())
    if types.is_union(arrow_type):
        members = [_nested_values(array.field(i)) for i in range(arrow_type.num_fields)]
        return _union_values(array, members)

    valid = array.is_valid().to_pylist()
    if types.is_struct(arrow_type):
        names = [arrow_type.field(i).name for i in range(arrow_type.num_fields)]
        fields = [_nested_values(array.field(i)) for i in range(arrow_type.num_fields)]
        return [
            dict(zip(names, row, strict=True)) if ok else None
            for ok, row in zip(valid, zip(*fields, strict=True), strict=True)
        ]
    if types.is_map(arrow_type):
        # A map's keys and items span the whole buffer; its offsets are sliced.
        pairs = list(zip(_nested_values(array.keys), _nested_values(array.items), strict=True))
        bounds = array.offsets.to_pylist()
        return [pairs[bounds[i] : bounds[i + 1]] if ok else None for i, ok in enumerate(valid)]

    # A list: flatten() leaves out null rows, even ones backed by values, and
    # keeps the rest in order. (No list_value_length(): pyarrow 16 has no
    # list-view kernel for it.)
    if types.is_fixed_size_list(arrow_type):
        lengths = [arrow_type.list_size] * len(array)
    elif types.is_list_view(arrow_type) or types.is_large_list_view(arrow_type):
        lengths = array.sizes.to_pylist()
    else:
        bounds = array.offsets.to_pylist()  # sliced with the array
        lengths = [bounds[i + 1] - bounds[i] for i in range(len(array))]
    flat = _nested_values(array.flatten())
    values: list[Any] = []
    start = 0
    for ok, length in zip(valid, lengths, strict=True):
        values.append(flat[start : start + length] if ok else None)
        start += length if ok else 0
    return values


def _nanos_cells(array: Any, tz: ZoneInfo | None) -> list[Any]:
    """JSON cells of one chunk of a column that holds nanosecond temporals.

    A dictionary's or a union's cell is the value it stands for, serialized
    as the API serializes any cell. The API's column-level steps (intervals,
    text-stored numerics) key on the column's type, so they do not apply. Any
    other nested cell is ``str()`` of its Python value.
    """
    import pyarrow as pa

    arrow_type = array.type
    if _is_nano_temporal(arrow_type):
        return _nanosecond_cells(array, tz)
    if pa.types.is_dictionary(arrow_type):
        return _nanos_cells(array.dictionary_decode(), tz)
    if pa.types.is_union(arrow_type):
        members = [_nanos_cells(array.field(i), tz) for i in range(arrow_type.num_fields)]
        return _union_values(array, members)
    return [_arrow_cell(value, tz) for value in _nested_values(array)]


def _arrow_column_cells(column: Any, tz: ZoneInfo | None) -> list[Any]:
    """Convert one Arrow column to JSON cells."""
    import pyarrow as pa

    types = pa.types
    arrow_type = column.type
    if _holds_nanos(arrow_type):
        return [cell for chunk in column.chunks for cell in _nanos_cells(chunk, tz)]
    values = column.to_pylist()
    if (
        types.is_integer(arrow_type)
        or types.is_boolean(arrow_type)
        or types.is_string(arrow_type)
        or types.is_large_string(arrow_type)
        or types.is_null(arrow_type)
    ):
        return values  # already what JSON carries
    if _arrow_text_numeric(arrow_type):
        parsed: list[Any] = []
        for value in values:
            if isinstance(value, str):
                with contextlib.suppress(ArithmeticError, ValueError):
                    value = Decimal(value)
            parsed.append(value)
        values = parsed
    elif types.is_interval(arrow_type) or types.is_duration(arrow_type):
        # A month-day-nano interval reads as a ``MonthDayNano`` tuple; the API
        # turns it into a timedelta counting a month as 30 days, as DuckDB does.
        values = [
            timedelta(days=v.months * 30 + v.days, microseconds=v.nanoseconds // 1000)
            if getattr(v, "months", None) is not None
            else v
            for v in values
        ]
    return [_arrow_cell(value, tz) for value in values]


def _decode_arrow_result(body: bytes) -> dict[str, Any]:
    """Decode the API's ``format=arrow`` execute frame into its JSON response."""
    import pyarrow as pa
    import pyarrow.ipc

    if len(body) < 4:
        raise ToolError("API returned a malformed Arrow result: the frame is truncated")
    meta_len = int.from_bytes(body[:4], "big")
    if 4 + meta_len > len(body):
        raise ToolError("API returned a malformed Arrow result: the envelope is truncated")
    try:
        envelope = json.loads(body[4 : 4 + meta_len])
        with pyarrow.ipc.open_stream(pa.BufferReader(gzip.decompress(body[4 + meta_len :]))) as r:
            table = r.read_all()
    except (ValueError, OSError, EOFError, zlib.error, pa.ArrowException) as exc:
        raise ToolError(f"API returned a malformed Arrow result: {exc}") from None
    if not isinstance(envelope, dict):
        raise ToolError("API returned a malformed Arrow result: the envelope is not an object")

    tz: ZoneInfo | None = None
    if envelope.get("timezone"):
        try:
            tz = ZoneInfo(str(envelope["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Unknown result timezone %r; timestamps stay unlabelled", envelope["timezone"]
            )

    columns = [_arrow_column_cells(column, tz) for column in table.columns]
    rows = [list(row) for row in zip(*columns, strict=True)] if columns else []
    if not envelope.get("columns"):
        envelope["columns"] = [{"name": name} for name in table.column_names]

    response: dict[str, Any] = {}
    for field in _EXECUTE_RESPONSE_FIELDS:
        if field == "rows":
            response["rows"] = rows
        elif field in envelope:
            response[field] = envelope[field]
    for field, value in envelope.items():
        response.setdefault(field, value)
    return response


def _execute_response(resp: httpx.Response) -> Any:
    """An execute response as JSON data, decoding the Arrow frame when sent one."""
    media_type = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type == _ARROW_RESULT_MEDIA_TYPE:
        return _decode_arrow_result(resp.content)
    return _parse_json(resp)


def _impl_execute_query(
    model_id: str | None,
    query_json: str,
    *,
    dialect: str | None = None,
    output_format: str = "json",
    format_values: bool | None = None,
    locale: str | None = None,
    timezone: str | None = None,
) -> str:
    """Compile and execute a QueryObject given as a JSON string (shared impl)."""
    logger.info("execute_query called (model_id=%s, dialect=%s)", model_id, dialect)
    try:
        query = json.loads(query_json)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"Invalid query JSON: {exc}. The QueryObject schema is available via "
            "get_json_schema('query') and worked examples via get_example."
        ) from exc

    if output_format not in ("json", "tsv"):
        raise ToolError(f"output_format must be 'json' or 'tsv'; got {output_format!r}")
    # Raw JSON comes over as the Arrow frame and is converted here (see
    # _decode_arrow_result); formatted values and TSV are rendered by the API.
    api_format = "arrow" if output_format == "json" and not format_values else output_format
    extra_params: dict[str, str] = {"format": api_format}
    if format_values is not None:
        extra_params["format_values"] = str(format_values).lower()
    if locale is not None:
        extra_params["locale"] = locale
    if timezone is not None:
        extra_params["timezone"] = timezone

    if model_id is None:
        params: dict[str, str] = {**extra_params}
        if dialect is not None:
            params["dialect"] = dialect
        resp = _shortcut_request("POST", "/query/execute", json_body=query, params=params)
    else:
        body: dict = {"model_id": model_id, "query": query}
        if dialect is not None:
            body["dialect"] = dialect
        resp = _session_request("POST", "/query/execute", json_body=body, params=extra_params)

    if output_format == "tsv":
        return resp.text
    return json.dumps(_execute_response(resp), indent=2)


def _impl_search_values(
    model_id: str | None,
    dimension: str,
    search: str | None,
    where_json: str | None,
    limit: int | None,
    dialect: str | None,
) -> str:
    """List or search a dimension's values (shared implementation, API 2.36)."""
    body: dict[str, Any] = {"dimension": dimension}
    if search is not None:
        body["search"] = search
    if where_json is not None:
        try:
            where = json.loads(where_json)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid where JSON: {exc}") from exc
        if not isinstance(where, list):
            raise ToolError("'where' must be a JSON array of query filters.")
        body["where"] = where
    if limit is not None:
        body["limit"] = limit
    if dialect is not None:
        body["dialect"] = dialect
    if model_id is None:
        resp = _shortcut_request("POST", "/values", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/values", json_body=body)
    data = _parse_json(resp)

    name = data.get("dimension", dimension)
    values = data.get("values", [])
    lines: list[str] = []
    # A date or number dimension answers with its range instead of values.
    if data.get("minimum") is not None or data.get("maximum") is not None:
        lines.append(f"Range of '{name}': {data.get('minimum')} .. {data.get('maximum')}")
    if values:
        header = f"Values of '{name}'" + (f" matching '{search}'" if search is not None else "")
        lines.append(f"{header}:")
        for v in values:
            score = v.get("score")
            score_str = f"  score={score:.2f}" if isinstance(score, int | float) else ""
            lines.append(f"  {json.dumps(v.get('value'))}  ({v.get('match', '?')}{score_str})")
        if data.get("truncated"):
            lines.append("  … more values match; raise 'limit' or narrow 'search'.")
    elif not lines:
        target = f" matching '{search}'" if search is not None else ""
        lines.append(f"No values of '{name}' found{target}.")
    for w in data.get("warnings", []):
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def _impl_get_model_diagram(model_id: str | None, show_columns: bool, theme: str) -> str:
    """Generate a Mermaid ER diagram (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request(
            "GET",
            "/diagram/er",
            params={"show_columns": str(show_columns).lower(), "theme": theme},
        )
    else:
        params = f"?show_columns={str(show_columns).lower()}&theme={quote(theme, safe='')}"
        resp = _session_request("GET", f"/models/{model_id}/diagram/er{params}")
    return _parse_json(resp)["mermaid"]


# Artefact kinds discoverable via find_artefacts.
_ARTEFACT_KINDS: tuple[str, ...] = ("dimension", "measure", "metric")

# kind → (plural REST collection segment, list-section header).
_ARTEFACT_ENDPOINTS: dict[str, tuple[str, str]] = {
    "dimension": ("dimensions", "Dimensions:"),
    "measure": ("measures", "Measures:"),
    "metric": ("metrics", "Metrics:"),
}


def _fetch_artefacts(model_id: str | None, kind: str) -> list[dict]:
    """Fetch the full set of records for one artefact kind."""
    segment = _ARTEFACT_ENDPOINTS[kind][0]
    if model_id is None:
        resp = _shortcut_request("GET", f"/{segment}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/{segment}")
    return _parse_json(resp)


def _render_dimension_lines(dims: list[dict]) -> list[str]:
    """Render dimension records as indented detail lines."""
    lines: list[str] = []
    for d in dims:
        grain = f"  grain={d['time_grain']}" if d.get("time_grain") else ""
        via = f"  via {d['via']}" if d.get("via") else ""
        if d.get("path_name"):
            via += f" path {d['path_name']}"
        d_name = d.get("name", "?")
        d_type = d.get("result_type", "?")
        d_obj = d.get("data_object", "?")
        d_col = d.get("column", "?")
        lines.append(f"  {d_name}  ({d_type}, {d_obj}.{d_col}{grain}{via})")
        if d.get("description"):
            lines.append(f"    description: {d['description']}")
        if d.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(d['synonyms'])}")
        if d.get("value_search") is False:
            lines.append("    valueSearch: false (values not listable)")
    return lines


def _render_measure_lines(measures: list[dict]) -> list[str]:
    """Render measure records as indented detail lines."""
    lines: list[str] = []
    for m in measures:
        expr = f"  expr: {m['expression']}" if m.get("expression") else ""
        m_name = m.get("name", "?")
        m_type = m.get("result_type", "?")
        m_agg = m.get("aggregation", "?")
        m_dtype = _aliased(m, "dataType", "data_type")
        dtype = f"  dataType: {m_dtype}" if m_dtype else ""
        total = "  total" if m.get("total") else ""
        grain_tag = "  grain" if m.get("grain") else ""
        fc_tag = "  filterContext" if m.get("filter_context") or m.get("filterContext") else ""
        default_val = _default_value_tag(m)
        lines.append(
            f"  {m_name}  ({m_type}, {m_agg}{expr}{dtype}{default_val}{total}{grain_tag}{fc_tag})"
        )
        cols = m.get("columns") or []
        if len(cols) > 1 and not m.get("expression"):
            col_refs = [
                f"{c.get('data_object') or c.get('dataObject', '?')}.{c.get('column', '?')}"
                if isinstance(c, dict)
                else str(c)
                for c in cols
            ]
            lines.append(f"    columns: [{', '.join(col_refs)}]")
        if m.get("description"):
            lines.append(f"    description: {m['description']}")
        if m.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(m['synonyms'])}")
    return lines


def _render_metric_lines(metrics: list[dict]) -> list[str]:
    """Render metric records as indented detail lines."""
    lines: list[str] = []
    for met in metrics:
        components = ", ".join(met.get("component_measures", []))
        lines.append(f"  {met['name']}  {_format_metric_summary(met)}")
        met_dtype = _aliased(met, "dataType", "data_type")
        if met_dtype:
            lines.append(f"    dataType: {met_dtype}")
        if met.get("description"):
            lines.append(f"    description: {met['description']}")
        if components:
            lines.append(f"    components: {components}")
        if met.get("synonyms"):
            lines.append(f"    synonyms: {', '.join(met['synonyms'])}")
    return lines


_ARTEFACT_RENDERERS = {
    "dimension": _render_dimension_lines,
    "measure": _render_measure_lines,
    "metric": _render_metric_lines,
}


def _impl_list_artefacts(
    model_id: str | None,
    kind: str | None = None,
    name: str | None = None,
) -> str:
    """Deterministic artefact lookup (shared implementation).

    Exact, complete enumeration — the authoritative set, not ranked candidates
    (that is ``find_artefacts``). With no ``name``, returns every artefact
    (optionally narrowed to one ``kind``); with ``name``, returns that exact
    artefact. Always renders full records at whatever cardinality results.
    """
    kinds = [kind] if kind else list(_ARTEFACT_KINDS)
    sections: list[str] = []
    for k in kinds:
        records = _fetch_artefacts(model_id, k)
        if name is not None:
            records = [r for r in records if r.get("name") == name]
        if not records:
            continue
        header = _ARTEFACT_ENDPOINTS[k][1]
        sections.append("\n".join([header, "", *_ARTEFACT_RENDERERS[k](records)]))
    if sections:
        return "\n\n".join(sections)
    # Nothing matched — phrase the empty case for the narrowing that was applied.
    if name is not None:
        of_kind = f" {kind}" if kind else " artefact"
        return f"No{of_kind} named '{name}' found in this model."
    if kind:
        return f"No {kind}s in this model."
    return "No artefacts in this model."


def _impl_explain_artefact(model_id: str | None, name: str) -> str:
    """Explain the lineage of a dimension, measure, or metric (shared impl)."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/explain/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/explain/{encoded}")
    data = _parse_json(resp)
    lines = [f"Explain: {data['name']}  (type: {data['type']})", ""]
    for item in data.get("lineage", []):
        detail = f"  — {item['detail']}" if item.get("detail") else ""
        lines.append(f"  [{item['type']}] {item['name']}{detail}")
    return "\n".join(lines)


def _impl_find_artefacts(model_id: str | None, query: str, kind: str | None) -> str:
    """Fuzzy, ranked search across model artefacts (shared implementation)."""
    body: dict = {"query": query}
    if kind is not None:
        body["types"] = [kind]
    if model_id is None:
        resp = _shortcut_request("POST", "/find", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/find", json_body=body)
    data = _parse_json(resp)
    results = data.get("results", [])
    fuzzy = data.get("fuzzy_matches", [])
    exact = data.get("exact_matches")
    synonym = data.get("synonym_matches")
    if not results and not fuzzy:
        return f"No artefacts found matching '{query}'."
    lines = [f"Search results for '{query}':", ""]
    if exact or synonym:
        if exact:
            lines.append("Exact matches:")
            for r in exact:
                lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
        if synonym:
            if exact:
                lines.append("")
            lines.append("Synonym matches:")
            for r in synonym:
                lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    elif results:
        for r in results:
            lines.append(f"  [{r['type']}] {r['name']}  (matched on {r['match_field']})")
    if fuzzy:
        if results or exact or synonym:
            lines.append("")
        lines.append("Fuzzy matches (no exact or synonym hit):")
        for f in fuzzy:
            score = f.get("score")
            score_str = f"  score={score:.2f}" if isinstance(score, int | float) else ""
            reason = f"  ({f['reason']})" if f.get("reason") else ""
            lines.append(f"  [{f.get('kind', '?')}] {f.get('name', '?')}{score_str}{reason}")
    return "\n".join(lines)


def _impl_get_graph(model_id: str | None) -> str:
    """Return the OBSL-Core RDF graph as Turtle (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/graph")
    else:
        resp = _session_request("GET", f"/models/{model_id}/graph")
    return resp.text


def _impl_sparql_query(model_id: str | None, query: str) -> str:
    """Execute a read-only SPARQL query (shared implementation)."""
    body: dict = {"query": query}
    if model_id is None:
        resp = _shortcut_request("POST", "/sparql", json_body=body)
    else:
        resp = _session_request("POST", f"/models/{model_id}/sparql", json_body=body)
    data = _parse_json(resp)

    query_type = data.get("type", "select")
    if query_type == "ask":
        return f"ASK result: {data.get('boolean', False)}"

    variables = data.get("variables", [])
    results = data.get("results", [])
    # API 2.30 reports variables that are used but never bound (a typo in
    # ORDER BY or SELECT) as warnings instead of silently ignoring them.
    warning_lines = _format_warnings(data.get("warnings"), indent="warning: ")
    if not results:
        return "\n".join(["SPARQL query returned no results.", *warning_lines])

    # Format as a readable table
    lines = [" | ".join(variables)]
    lines.append(" | ".join("---" for _ in variables))
    for row in results:
        lines.append(" | ".join(str(row.get(v, "")) for v in variables))
    if warning_lines:
        lines.append("")
        lines.extend(warning_lines)
    return "\n".join(lines)


def _impl_get_join_graph(model_id: str | None) -> str:
    """Return the join graph as an adjacency list (shared implementation)."""
    if model_id is None:
        resp = _shortcut_request("GET", "/join-graph")
    else:
        resp = _session_request("GET", f"/models/{model_id}/join-graph")
    data = _parse_json(resp)
    lines = [f"Nodes: {', '.join(data.get('nodes', []))}", ""]
    edges = data.get("edges", [])
    if edges:
        lines.append("Edges:")
        for e in edges:
            cols = (
                f"  on ({', '.join(e['columns_from'])}) = ({', '.join(e['columns_to'])})"
                if e.get("columns_from")
                else ""
            )
            secondary = " [secondary]" if e.get("secondary") else ""
            # A required join compiles to INNER rather than the default LEFT, so
            # an unmatched row disappears from every query crossing this edge —
            # not something to leave implicit in a join graph.
            required = " [required: INNER]" if e.get("required") else ""
            path = f" path={e['path_name']}" if e.get("path_name") else ""
            lines.append(
                f"  {e['from_object']} --[{e['cardinality']}]--> "
                f"{e['to_object']}{cols}{secondary}{required}{path}"
            )
    else:
        lines.append("No joins defined.")
    return "\n".join(lines)


def _coerce_anchor_list(anchors: list[str] | str | None) -> list[str]:
    """Normalise the ``anchors`` arg into a list of names.

    Accepts a real list, a JSON-array string (``'["A", "B"]'``), or a single
    bare name. Returns ``[]`` for ``None``/empty.
    """
    if anchors is None:
        return []
    if isinstance(anchors, list):
        return anchors
    text = anchors.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid anchors JSON string: {exc}") from exc
        if not isinstance(parsed, list):
            raise ToolError("anchors JSON must be an array of artefact names.")
        return [str(a) for a in parsed]
    return [text]


def _render_composables(data: dict) -> str:
    """Render an ACR ComposablesResponse as readable text (shared impl)."""
    anchor = data.get("anchorObjects") or []
    header = (
        f"Composable with anchor on [{', '.join(anchor)}]:"
        if anchor
        else "Composable artefacts (empty anchor — everything composes):"
    )
    lines = [header, ""]

    def _section(label: str, items: list[str]) -> None:
        if items:
            lines.append(f"{label}: {', '.join(items)}")

    _section("Dimensions", data.get("dimensions") or [])
    _section("Measures", data.get("measures") or [])
    _section("Metrics", data.get("metrics") or [])

    cfl_measures = data.get("cflMeasures") or []
    cfl_metrics = data.get("cflMetrics") or []
    if cfl_measures or cfl_metrics:
        lines.append("")
        lines.append("Via Composite Fact Layer (independent fact, unioned at the shared grain):")
        _section("  CFL measures", cfl_measures)
        _section("  CFL metrics", cfl_metrics)

    directly_composable = any(data.get(k) for k in ("dimensions", "measures", "metrics"))
    if not directly_composable and not (cfl_measures or cfl_metrics):
        lines.append("Nothing else composes with this anchor.")
    return "\n".join(lines)


def _impl_find_composables(
    model_id: str | None,
    query_json: str | None,
    anchors: list[str],
    anchor_type: str | None,
) -> str:
    """Resolve artefacts composable with an anchor (shared implementation)."""
    if query_json is not None:
        try:
            query = json.loads(query_json)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"Invalid query JSON: {exc}. The QueryObject schema is available via "
                "get_json_schema('query') and worked examples via get_example."
            ) from exc
        if model_id is None:
            resp = _shortcut_request("POST", "/composables", json_body=query)
        else:
            resp = _session_request("POST", f"/models/{model_id}/composables", json_body=query)
    else:
        params: dict[str, Any] = {"anchor": anchors}
        if anchor_type is not None:
            params["anchorType"] = anchor_type
        if model_id is None:
            resp = _shortcut_request("GET", "/composables", params=params)
        else:
            resp = _session_request("GET", f"/models/{model_id}/composables", params=params)
    return _render_composables(_parse_json(resp))


def _impl_load_model(
    model: dict | str | None,
    extends: list[dict] | str | None,
    inherits: str | None,
    dedup: bool,
    datasource: str | None = None,
) -> str:
    """Load a model and render the load summary (shared implementation)."""
    if not model:
        raise ToolError(
            "model is mandatory — provide the OBML model as a JSON object. "
            "The OBML reference is available as a separate tool to learn the structure."
        )
    if isinstance(model, str):
        try:
            model = json.loads(model)
        except json.JSONDecodeError as exc:
            raise ToolError(f"Invalid model JSON string: {exc}") from exc
    logger.info("load_model called")
    body: dict = {"model_json": model, "dedup": dedup}
    if extends:
        if isinstance(extends, str):
            try:
                extends = json.loads(extends)
            except json.JSONDecodeError as exc:
                raise ToolError(f"Invalid extends JSON string: {exc}") from exc
        body["extends_json"] = extends
    if inherits:
        body["inherits"] = inherits
    # Sent whenever given, "" included: the API refuses an empty name (422), where
    # dropping it would load on the model's own data source instead.
    if datasource is not None:
        body["datasource"] = datasource
    resp = _session_request("POST", "/models", json_body=body)
    data = _parse_json(resp)
    return _render_load_result(data)


def _render_load_result(data: dict, extra_lines: list[str] | None = None) -> str:
    """Render a model-load summary and run the design → run transition.

    Shared by ``_impl_load_model`` and ``_impl_load_model_from_osi``. ``data`` is
    the parsed ``ModelLoadResponse``. ``extra_lines`` are appended before the
    re-list footer (used to surface OSI conversion warnings).
    """
    load_state = data.get("model_load") or "fresh"
    header = (
        f"Model loaded ({load_state}).  model_id: {data['model_id']}"
        if load_state in ("fresh", "reused")
        else f"Model loaded successfully.  model_id: {data['model_id']}"
    )
    parts = [
        header,
        f"  data objects: {data['data_objects']}",
        f"  dimensions:   {data['dimensions']}",
        f"  measures:     {data['measures']}",
        f"  metrics:      {data['metrics']}",
    ]
    health = data.get("health")
    if health:
        parts.append(
            f"  health: {health.get('status', 'ok')}  "
            f"(joins: {health.get('joins', 0)}, "
            f"warnings: {health.get('warnings_count', 0)})"
        )
        orphans = health.get("orphan_data_objects") or []
        if orphans:
            parts.append(f"    orphan dataObjects: {', '.join(orphans)}")
        unreachable = health.get("unreachable_dimensions") or []
        if unreachable:
            parts.append(f"    unreachable dimensions: {', '.join(unreachable)}")
        for risk in health.get("fan_trap_risks") or []:
            tables = ", ".join(risk.get("tables", []))
            parts.append(f"    fan-trap risk on [{tables}]: {risk.get('reason', '')}")
    parts.extend(_format_warnings(data.get("warnings")))

    # Surface server-resolved dialect/timezone as data (see
    # _fetch_effective_settings). Scoped to the model just loaded: the dialect
    # and timezone resolution chains start at that model's own settings, and a
    # session holding more than one model resolves neither without the scope.
    effective = _fetch_effective_settings(data.get("model_id"))
    if effective.get("dialect"):
        parts.append(f"  effective dialect:  {effective['dialect']}")
    if effective.get("timezone"):
        parts.append(f"  effective timezone: {effective['timezone']}")

    if extra_lines:
        parts.extend(extra_lines)

    # Forward transition (design → run): record the model and prompt a re-list so
    # the host discovers the now-available run-time tool set.
    _mark_model_loaded(data["model_id"])
    parts.append("")
    parts.append(
        "Run-time tools are now available (execute_query, describe_model, "
        "find_artefacts, …). Re-list tools to discover them."
    )
    return "\n".join(parts)


def _format_osi_input_validation(input_validation: dict | None) -> list[str]:
    """Render advisory OSI input-validation lines (OSI v0.2 schema check).

    Legacy OSI v0.1 inputs may produce spurious schema errors the converter's
    compat shim absorbs, so these are surfaced as advisory, not failure.
    """
    iv = input_validation or {}
    lines: list[str] = []
    in_errors = iv.get("schema_errors", []) + iv.get("semantic_errors", [])
    if in_errors:
        lines.append(f"  input validation issues (OSI v0.2 schema): {'; '.join(in_errors)}")
    if iv.get("semantic_warnings"):
        lines.append(f"  input validation warnings: {'; '.join(iv['semantic_warnings'])}")
    return lines


def _impl_load_model_from_osi(osi_yaml: str | None, dedup: bool) -> str:
    """Convert an OSI YAML model to OBML, load it, and render the summary."""
    if not osi_yaml or not osi_yaml.strip():
        raise ToolError("osi_yaml is mandatory — provide the OSI model as a YAML string.")
    logger.info("load_model_from_osi called")
    resp = _session_request(
        "POST", "/models/from-osi", json_body={"osi_yaml": osi_yaml, "dedup": dedup}
    )
    data = _parse_json(resp)

    extra: list[str] = []
    conversion_warnings = data.get("conversion_warnings") or []
    if conversion_warnings:
        extra.append(f"  OSI → OBML conversion warnings: {'; '.join(conversion_warnings)}")
    extra.extend(_format_osi_input_validation(data.get("input_validation")))
    return _render_load_result(data, extra_lines=extra)


def _impl_validate_model(
    model: dict | str | None,
    model_yaml: str | None,
    extends: list[str] | str | None,
    inherits: str | None,
    online: bool,
    dialect: str | None,
    sample: int | str | None = None,
) -> str:
    """Validate a model without loading it, and render the report.

    Routes to the session-scoped ``/validate`` in multi-model mode, because
    ``inherits`` names a parent model that only exists inside a session, and to
    the stateless shortcut in single-model mode, where there is no session to
    scope to and ``inherits`` is meaningless.
    """
    if model is None and not model_yaml:
        raise ToolError(
            "Provide exactly one model source: 'model' (OBML JSON) or 'model_yaml' "
            "(OBML YAML). The OBML reference is available as a separate tool."
        )
    if model is not None and model_yaml:
        raise ToolError("Provide exactly one model source: 'model' or 'model_yaml', not both.")
    # The stateless shortcut single-model mode routes to reads neither
    # ``extends`` nor ``inherits`` off the request body — it validates the
    # document alone. Sending either would answer about the *unmerged* model
    # while the caller believes the fragments were checked, so refuse both here
    # rather than let a silent partial validation come back `valid: true`.
    if _single_model_mode and (extends or inherits):
        unsupported = " and ".join(
            n for n, v in (("'extends'", extends), ("'inherits'", inherits)) if v
        )
        raise ToolError(
            f"{unsupported} cannot be used in single-model mode: the stateless "
            "/validate route ignores both, so the fragments would go unchecked "
            "while the result reported on the base model alone. Validate the "
            "already-merged document instead."
        )

    body: dict = {}
    if model is not None:
        if isinstance(model, str):
            try:
                model = json.loads(model)
            except json.JSONDecodeError as exc:
                raise ToolError(f"Invalid model JSON string: {exc}") from exc
        body["model_json"] = model
    else:
        body["model_yaml"] = model_yaml
    if extends:
        if isinstance(extends, str):
            try:
                extends = json.loads(extends)
            except json.JSONDecodeError as exc:
                raise ToolError(f"Invalid extends JSON string: {exc}") from exc
        body["extends"] = extends
    if inherits:
        body["inherits"] = inherits

    # ``online``, ``dialect`` and ``sample`` are query parameters, not body
    # fields. ``sample`` implies the online check (API 3.1). Send ``dialect``
    # only alongside it: offline validation opens no connection, so naming one
    # there would read as if it selected the SQL to generate, which this
    # dialect does not do (it names a datasource to probe).
    sample = str(sample).strip() if sample is not None and str(sample).strip() else None
    online = online or sample is not None
    params: dict[str, Any] = {}
    if online:
        params["online"] = "true"
        if dialect:
            params["dialect"] = dialect
    if sample is not None:
        params["sample"] = sample

    logger.info("validate_model called (online=%s, sample=%s)", online, sample)
    if _single_model_mode:
        resp = _shortcut_request("POST", "/validate", json_body=body, params=params or None)
    else:
        resp = _session_request("POST", "/validate", json_body=body, params=params or None)
    return _render_validate_result(_parse_json(resp), online=online, sample=sample)


def _render_validate_result(data: dict, *, online: bool, sample: str | None = None) -> str:
    """Render a ``ValidateResponse`` as a report.

    Errors and warnings share the ``StructuredWarning`` shape, so both go
    through :func:`_format_warning` and a caller reading one reads the other.
    """
    errors = data.get("errors") or []
    warnings = data.get("warnings") or []
    scope = "offline + datasource" if online else "offline only"
    if sample is not None:
        scope += f" + sample of {sample}{'' if sample.endswith('%') else ' rows'}"
    if data.get("valid"):
        header = f"Model is valid ({scope})."
    else:
        header = f"Model is INVALID ({scope}) — {len(errors)} error(s)."
    lines = [header]
    lines.extend(_format_warnings(errors, indent="  errors:   "))
    lines.extend(_format_warnings(warnings, indent="  warnings: "))
    if not online:
        lines.append(
            "\nStructure only — no table or column was checked against a "
            "warehouse. Pass online=true to probe the datasource."
        )
    return "\n".join(lines)


def _render_osi_validation(validation: dict, label: str) -> list[str]:
    """Render conversion validation errors/warnings into report lines."""
    parts: list[str] = []
    if not validation.get("schema_valid", True) or not validation.get("semantic_valid", True):
        errors = validation.get("schema_errors", []) + validation.get("semantic_errors", [])
        parts.append(f"\n{label} validation errors: {'; '.join(errors)}")
    if validation.get("semantic_warnings"):
        parts.append(f"\n{label} validation warnings: {'; '.join(validation['semantic_warnings'])}")
    return parts


def _impl_export_model_to_osi(
    model_id: str,
    model_name: str,
    model_description: str,
    ai_instructions: str,
    include_ontology: bool = False,
) -> str:
    """Export a loaded model as OSI YAML (multi-model only — model_id required).

    The API's OSI ontology emit was removed in OBSL 2.24 — the route answers
    410 to ``include_ontology=true`` and no longer returns ``ontology_yaml`` /
    ``ontology_validation``. ``include_ontology`` is kept as a deprecated
    parameter that mirrors the API's own stance: accepted (and omitted from
    the request) when false, so callers still passing the old default are not
    rejected by schema validation, and refused with a clear message when true
    rather than round-tripping to a 410.
    """
    if include_ontology:
        raise ToolError(
            "include_ontology is no longer supported: the OSI ontology emit was "
            "removed in OrionBelt Semantic Layer 2.24 (generating an ontology from "
            "a logical model is out of scope). Omit the parameter — the core-spec "
            "OSI export is unchanged. For the OBSL RDF ontology graph, which is a "
            "separate feature, use get_model_graph or query_model_graph_by_sparql."
        )
    params = {
        "model_name": model_name,
        "model_description": model_description,
        "ai_instructions": ai_instructions,
    }
    resp = _session_request("GET", f"/models/{model_id}/osi", params=params)
    data = _parse_json(resp)

    parts = [data["output_yaml"]]
    if data.get("warnings"):
        parts.append(f"\nWarnings: {'; '.join(data['warnings'])}")
    parts.extend(_render_osi_validation(data.get("validation") or {}, "Core-spec"))
    return "\n".join(parts)


def _impl_remove_model(model_id: str) -> str:
    """Remove a model and render the reverse-transition summary."""
    _session_request("DELETE", f"/models/{model_id}")
    # Reverse transition (run → design): forget the model; if none remain,
    # prompt a re-list so the host drops the now-invalid run-time verbs.
    _mark_model_removed(model_id)
    msg = f"Model {model_id} removed."
    if _current_phase() == PHASE_DESIGN:
        msg += (
            "\n\nNo models remain loaded — back to the design-time tool set. "
            "Re-list tools: run-time verbs are unavailable until a model "
            "is loaded again."
        )
    return msg


def _impl_list_examples(model_id: str | None, intent: str | None) -> str:
    """List canonical example queries authored alongside the model."""
    params: dict[str, str] | None = {"intent": intent} if intent else None
    if model_id is None:
        resp = _shortcut_request("GET", "/examples", params=params)
    else:
        resp = _session_request("GET", f"/models/{model_id}/examples", params=params)
    data = _parse_json(resp)
    examples = data.get("examples") or []
    suggestion = data.get("suggestion")
    if not examples:
        if suggestion:
            return suggestion
        return "No examples authored on this model."
    lines = ["Examples:", ""]
    for ex in examples:
        tags = ex.get("intent_tags") or []
        tag_str = f"  [tags: {', '.join(tags)}]" if tags else ""
        lines.append(f"  {ex['name']}{tag_str}")
        if ex.get("description"):
            lines.append(f"    {ex['description']}")
    if suggestion:
        lines.append("")
        lines.append(f"Note: {suggestion}")
    return "\n".join(lines)


def _impl_get_example(model_id: str | None, name: str) -> str:
    """Return a single example with its query and compiled SQL preview."""
    encoded = quote(name, safe="")
    if model_id is None:
        resp = _shortcut_request("GET", f"/examples/{encoded}")
    else:
        resp = _session_request("GET", f"/models/{model_id}/examples/{encoded}")
    return json.dumps(_parse_json(resp), indent=2)


def _impl_run_batch(
    model_yaml: str | None,
    model_id: str | None,
    queries: list[dict],
    dialect: str | None,
    execute: bool,
    max_parallelism: int | None,
    fail_fast: bool,
    persist_model: bool,
    dedup: bool,
    session_id: str | None,
) -> str:
    """POST /v1/oneshot/batch and return a JSON-formatted result."""
    if not queries:
        raise ToolError("queries must contain at least one item")
    body: dict = {
        "queries": queries,
        "execute": execute,
        "fail_fast": fail_fast,
        "persist_model": persist_model,
        "dedup": dedup,
    }
    if model_yaml:
        body["model_yaml"] = model_yaml
    if model_id:
        body["model_id"] = model_id
    if dialect is not None:
        body["dialect"] = dialect
    if max_parallelism is not None:
        body["max_parallelism"] = max_parallelism
    if session_id is not None:
        body["session_id"] = session_id
    client = _get_client()
    resp = _do_request(client, "POST", f"{_API_V1}/oneshot/batch", body)
    if resp.status_code >= 400:
        _raise_api_error(resp)
    return json.dumps(_parse_json(resp), indent=2)


# ---------------------------------------------------------------------------
# Business rules + ontology links (API 2.30)
# ---------------------------------------------------------------------------


def _model_request(
    model_id: str | None,
    method: str,
    path: str,
    json_body: dict | None = None,
    params: dict[str, Any] | None = None,
):
    """Route a model-scoped call to the shortcut or the session endpoint."""
    if model_id is None:
        return _shortcut_request(method, path, json_body=json_body, params=params)
    return _session_request(method, f"/models/{model_id}{path}", json_body=json_body, params=params)


def _query_string(**params: Any) -> str:
    """``?k=v&...`` for the parameters that are set (lists joined by comma)."""
    items = {
        k: (",".join(v) if isinstance(v, list) else v)
        for k, v in params.items()
        if v not in (None, "")
    }
    return f"?{urlencode(items)}" if items else ""


def _render_mapping_lines(mappings: list | None, indent: str) -> list[str]:
    """One line per external concept mapping: relation, concept, provenance."""
    lines: list[str] = []
    for m in mappings or []:
        if not isinstance(m, dict):
            continue
        head = f"{indent}maps to: {m.get('relation', '?')} {m.get('concept', '?')}"
        if m.get("expanded_iri") and m["expanded_iri"] != m.get("concept"):
            head += f"  ({m['expanded_iri']})"
        provenance = [
            f"{label} {m[key]}"
            for key, label in (
                ("justification", "justification"),
                ("source", "source"),
                ("ontology_version", "version"),
                ("confidence", "confidence"),
                ("comment", "comment"),
            )
            if m.get(key) not in (None, "")
        ]
        if provenance:
            head += f"  [{'; '.join(provenance)}]"
        lines.append(head)
    return lines


def _fetch_rule_statistics(model_id: str | None) -> dict[str, Any]:
    """Best-effort ``GET .../rules`` statistics for ``describe_model``."""
    try:
        data = _parse_json(_model_request(model_id, "GET", "/rules"))
    except Exception:  # noqa: BLE001 — enrichment must never break the caller
        return {}
    stats = data.get("statistics") if isinstance(data, dict) else None
    return stats if isinstance(stats, dict) else {}


def _rule_headline(rule: dict) -> str:
    """``Name  (type, level, findings[, severity][, grain: ...])``."""
    tags = [rule.get("type", "?"), rule.get("level", "?"), rule.get("findings", "?")]
    if rule.get("severity"):
        tags.append(f"severity {rule['severity']}")
    if rule.get("grain"):
        tags.append(f"grain {', '.join(rule['grain'])}")
    return f"{rule.get('name', '?')}  ({', '.join(tags)})"


def _impl_list_rules(model_id: str | None) -> str:
    """List the model's business rules with statistics (shared implementation)."""
    data = _parse_json(_model_request(model_id, "GET", "/rules"))
    rules = data.get("rules") or []
    if not rules:
        return "This model declares no business rules (no `rules:` block)."
    stats = data.get("statistics") or {}

    def counts(key: str) -> str:
        return ", ".join(f"{k} {v}" for k, v in (stats.get(key) or {}).items()) or "-"

    lines = [
        f"{stats.get('total', len(rules))} rules on {data.get('dialect', '?')}:",
        f"  by type: {counts('by_type')}",
        f"  by level: {counts('by_level')}",
        f"  by severity: {counts('by_severity')}",
        f"  executable: {stats.get('executable', 0)}, not executable: "
        f"{stats.get('not_executable', 0)}",
        "",
    ]
    for rule in rules:
        lines.append(f"  {_rule_headline(rule)}")
        if rule.get("description"):
            lines.append(f"    description: {rule['description']}")
        reads = [*(rule.get("dimensions") or []), *(rule.get("measures") or [])]
        if reads:
            lines.append(f"    reads: {', '.join(reads)}")
        if rule.get("depends_on"):
            lines.append(f"    depends on: {', '.join(rule['depends_on'])}")
        if not rule.get("executable", True):
            lines.append(f"    not executable: {rule.get('error') or 'unknown reason'}")
    return "\n".join(lines)


def _impl_explain_rule(model_id: str | None, name: str) -> str:
    """One rule in full, then the SQL it compiles to for the session's dialect."""
    encoded = quote(name, safe="")
    rule = _parse_json(_model_request(model_id, "GET", f"/rules/{encoded}"))
    lines = [f"Rule: {_rule_headline(rule)}"]
    if rule.get("description"):
        lines.append(f"  description: {rule['description']}")
    if rule.get("owner"):
        lines.append(f"  owner: {rule['owner']}")
    if rule.get("synonyms"):
        lines.append(f"  synonyms: {', '.join(rule['synonyms'])}")
    reads = [*(rule.get("dimensions") or []), *(rule.get("measures") or [])]
    if reads:
        lines.append(f"  reads: {', '.join(reads)}")
    if rule.get("depends_on"):
        lines.append(f"  depends on: {', '.join(rule['depends_on'])}")
    # API 2.32 lineage: the tables behind what the rule reads. Best-effort.
    tables = _lineage_tables(model_id, "rule", rule.get("name", name))
    if tables:
        lines.append(f"  reads tables: {', '.join(tables)}")
    lines.extend(_render_mapping_lines(rule.get("external_concept_mappings"), "  "))
    if not rule.get("executable", True):
        lines.append(f"  not executable: {rule.get('error') or 'unknown reason'}")
    lines.append("")
    lines.append("Condition (OBML):")
    lines.append(json.dumps(rule.get("condition") or {}, indent=2))
    # The compiled SQL as a trailing block, the way get_example shows a
    # compiled preview. Best-effort: a rule that does not compile says why.
    lines.append("")
    try:
        compiled = _parse_json(_model_request(model_id, "POST", f"/rules/{encoded}/compile", {}))
    except (ToolError, httpx.HTTPError) as exc:
        lines.append(f"Compiled SQL: not available ({exc})")
        return "\n".join(lines)
    lines.append(f"Compiled SQL ({compiled.get('dialect', '?')}):")
    lines.append(compiled.get("sql") or "")
    lines.extend(_format_warnings(compiled.get("warnings"), indent="warning: "))
    return "\n".join(lines)


def _render_rows(columns: list[str], rows: list[list], indent: str = "  ") -> list[str]:
    """A compact pipe-separated table of result rows."""
    if not columns:
        return []
    lines = [f"{indent}{' | '.join(columns)}", f"{indent}{' | '.join('---' for _ in columns)}"]
    for row in rows:
        lines.append(f"{indent}{' | '.join('' if v is None else str(v) for v in row)}")
    return lines


def _impl_evaluate_rule(
    model_id: str | None,
    name: str,
    limit: int | None,
    dialect: str | None,
    format_values: bool,
) -> str:
    """Run one rule and list its findings (shared implementation)."""
    body: dict = {"format_values": format_values}
    if limit is not None:
        body["limit"] = limit
    if dialect is not None:
        body["dialect"] = dialect
    encoded = quote(name, safe="")
    data = _parse_json(_model_request(model_id, "POST", f"/rules/{encoded}/evaluate", body))
    count = data.get("row_count", 0)
    what = data.get("findings", "findings")
    cached = ", cached" if data.get("cached") else ""
    lines = [
        f"{data.get('name', name)}: {count} {what}  "
        f"({data.get('type', '?')}, {data.get('level', '?')}, "
        f"{data.get('dialect', '?')}, {data.get('execution_time_ms', 0):.0f} ms{cached})"
    ]
    if data.get("severity"):
        lines[0] += f"  severity {data['severity']}"
    if data.get("limit") and count >= data["limit"]:
        lines.append(f"  (showing the first {data['limit']}; pass a higher limit for more)")
    columns = [
        c.get("name", "?") if isinstance(c, dict) else str(c) for c in data.get("columns") or []
    ]
    if count:
        lines.append("")
        lines.extend(_render_rows(columns, data.get("rows") or []))
    lines.extend(_format_warnings(data.get("warnings"), indent="  warning: "))
    return "\n".join(lines)


def _impl_evaluate_rules(
    model_id: str | None,
    types: list[str] | None,
    severities: list[str] | None,
    executable_only: bool,
    max_rules: int | None,
    limit: int,
    dry_run: bool,
    dialect: str | None,
) -> str:
    """Run every rule (or a subset) into a report (shared implementation)."""
    body: dict = {
        "executable_only": executable_only,
        "limit": limit,
        "dry_run": dry_run,
        "include_sql": False,
    }
    if types:
        body["types"] = types
    if severities:
        body["severities"] = severities
    if max_rules is not None:
        body["max_rules"] = max_rules
    if dialect is not None:
        body["dialect"] = dialect
    data = _parse_json(_model_request(model_id, "POST", "/rules/evaluate", body))
    summary = data.get("summary") or {}
    mode = "dry run: compiled only" if dry_run else "executed"
    lines = [
        f"Rule report ({data.get('dialect', '?')}, {mode}, {data.get('elapsed_ms', 0):.0f} ms): "
        f"{summary.get('total', 0)} rules, {summary.get('executed', 0)} executed, "
        f"{summary.get('compiled', 0)} compiled, {summary.get('with_findings', 0)} with findings, "
        f"{summary.get('failed', 0)} failed, {summary.get('skipped', 0)} skipped",
        "",
    ]
    for item in data.get("results") or []:
        status = item.get("status", "?")
        detail = [status]
        if item.get("finding_count") is not None:
            detail.append(f"{item['finding_count']} {item.get('findings', 'findings')}")
        if item.get("severity"):
            detail.append(f"severity {item['severity']}")
        if item.get("cached"):
            detail.append("cached")
        lines.append(
            f"  {item.get('name', '?')}  ({item.get('type', '?')}, {item.get('level', '?')}): "
            f"{', '.join(detail)}"
        )
        if item.get("error"):
            lines.append(f"    error: {item['error']}")
        rows = item.get("rows") or []
        if rows:
            lines.extend(_render_rows(item.get("columns") or [], rows, indent="    "))
    return "\n".join(lines)


def _impl_find_concept_mappings(
    model_id: str | None,
    concept: str | None,
    namespace: str | None,
    relation: str | None,
    types: list[str] | None,
) -> str:
    """List external concept mappings, filtered (shared implementation)."""
    path = "/concept-mappings" + _query_string(
        concept=concept, namespace=namespace, relation=relation, types=types
    )
    data = _parse_json(_model_request(model_id, "GET", path))
    mappings = data.get("mappings") or []
    if not mappings:
        return "No external concept mappings match (see list_unmapped_artefacts for the gaps)."
    scope = f" for concept {data['concept']}" if data.get("concept") else ""
    lines = [f"{data.get('total', len(mappings))} concept mappings{scope}:"]
    for m in mappings:
        obj = m.get("object") or {}
        rendered = _render_mapping_lines([m], "")
        detail = rendered[0].removeprefix("maps to: ") if rendered else "?"
        lines.append(f"  [{obj.get('type', '?')}] {obj.get('name', '?')}  ->  {detail}")
    return "\n".join(lines)


def _impl_list_concept_namespaces(model_id: str | None) -> str:
    """Which external namespaces a model links into (shared implementation)."""
    data = _parse_json(_model_request(model_id, "GET", "/concept-mappings/namespaces"))
    namespaces = data.get("namespaces") or []
    prefixes = data.get("prefixes") or {}
    if not namespaces and not prefixes:
        return "This model declares no ontology prefixes and no external concept mappings."
    lines: list[str] = []
    if namespaces:
        lines.append("Namespaces linked into (most used first):")
        for ns in namespaces:
            label = f"{ns['prefix']}: " if ns.get("prefix") else ""
            lines.append(
                f"  {label}{ns.get('namespace', '?')}  "
                f"(mappings {ns.get('mapping_count', 0)}, objects {ns.get('object_count', 0)})"
            )
    unused = {
        k: v for k, v in prefixes.items() if not any(n.get("prefix") == k for n in namespaces)
    }
    if unused:
        lines.append("Declared prefixes without a mapping:")
        for prefix, namespace in unused.items():
            lines.append(f"  {prefix}: {namespace}")
    return "\n".join(lines)


def _impl_list_unmapped_artefacts(model_id: str | None, types: list[str] | None) -> str:
    """Artefacts in the mappable scope without a mapping (shared implementation)."""
    path = "/concept-mappings/unmapped" + _query_string(types=types)
    data = _parse_json(_model_request(model_id, "GET", path))
    objects = data.get("objects") or []
    scope = ", ".join(data.get("types") or []) or "all mappable types"
    if not objects:
        return f"Every artefact is mapped ({scope})."
    lines = [f"{data.get('total', len(objects))} unmapped artefacts ({scope}):"]
    for obj in objects:
        lines.append(f"  [{obj.get('type', '?')}] {obj.get('name', '?')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Lineage (API 2.32)
# ---------------------------------------------------------------------------

# Artefact kind → REST collection segment of ``GET .../{plural}/{name}/lineage``.
# Looked up per kind because a rule may share its name with a measure.
_LINEAGE_KINDS: dict[str, str] = {
    "dimension": "dimensions",
    "measure": "measures",
    "metric": "metrics",
    "rule": "rules",
}

_LINEAGE_FORMATS: tuple[str, ...] = ("text", "mermaid", "turtle", "json")

# Node kinds in the order the text rendering lists them: what is asked about
# first, the tables last.
_LINEAGE_NODE_ORDER: tuple[tuple[str, str], ...] = (
    ("query", "Query"),
    ("union", "Union (UNION ALL of a multi-fact query's legs)"),
    ("rule", "Rules"),
    ("metric", "Metrics"),
    ("measure", "Measures"),
    ("dimension", "Dimensions"),
    ("column", "Columns"),
    ("data_object", "Data objects (tables read)"),
)


def _lineage_api_format(fmt: str) -> str:
    """Map the tool's ``format`` to the API's ``?format=`` (text renders from JSON)."""
    if fmt not in _LINEAGE_FORMATS:
        raise ToolError(f"format must be one of {', '.join(_LINEAGE_FORMATS)}; got '{fmt}'")
    return "json" if fmt in ("text", "json") else fmt


def _render_lineage(data: dict, title: str) -> str:
    """Nodes grouped by kind, then the edges, from a ``LineageResponse``."""
    nodes = data.get("nodes") or []
    edges = data.get("edges") or []
    lines = [f"{title}  ({len(nodes)} nodes, {len(edges)} edges; root {data.get('root', '?')})"]
    known = {kind for kind, _ in _LINEAGE_NODE_ORDER}
    order = [*_LINEAGE_NODE_ORDER]
    order.extend((k, k) for k in dict.fromkeys(n.get("kind") for n in nodes) if k not in known)
    for kind, header in order:
        members = [n for n in nodes if n.get("kind") == kind]
        if not members:
            continue
        lines.append("")
        lines.append(f"{header}:")
        for n in members:
            detail = f"  ({n['detail']})" if n.get("detail") else ""
            # API 3.0: a data object names the data source its table is read
            # from, so a query across sources shows which table comes from where.
            source = f"  [source: {n['data_source']}]" if n.get("data_source") else ""
            lines.append(f"  {n.get('name', '?')}{detail}{source}")
    if edges:
        lines.append("")
        lines.append("Edges (source feeds target):")
        for e in edges:
            label = f"  [{e['label']}]" if e.get("label") else ""
            path = f"  (pathName {e['path_name']})" if e.get("path_name") else ""
            lines.append(f"  {e.get('source', '?')} -> {e.get('target', '?')}{label}{path}")
    return "\n".join(lines)


def _lineage_result(resp: httpx.Response, fmt: str, title: str) -> str:
    """Shape one lineage response for the requested ``format``."""
    if fmt in ("mermaid", "turtle"):
        return resp.text
    data = _parse_json(resp)
    if fmt == "json":
        return json.dumps(data, indent=2)
    return _render_lineage(data, title)


def _impl_artefact_lineage(model_id: str | None, name: str, kind: str | None, fmt: str) -> str:
    """Lineage of a named dimension, measure, metric or rule.

    Without ``kind``, every kind is tried: a name that resolves to more than
    one (a rule named like a measure) is reported for each in ``text``/``json``,
    and must be disambiguated with ``kind`` for ``mermaid``/``turtle``.
    """
    if kind is not None and kind not in _LINEAGE_KINDS:
        raise ToolError(f"kind must be one of {', '.join(_LINEAGE_KINDS)}; got '{kind}'")
    params = {"format": _lineage_api_format(fmt)}
    encoded = quote(name, safe="")
    kinds = [kind] if kind else list(_LINEAGE_KINDS)
    found: list[tuple[str, httpx.Response]] = []
    for k in kinds:
        path = f"/{_LINEAGE_KINDS[k]}/{encoded}/lineage"
        try:
            found.append((k, _model_request(model_id, "GET", path, params=params)))
        except ToolError as exc:
            # Probing: only the API's own "no such artefact of this kind" is a
            # miss. Anything else (auth, 5xx, a missing model or session, no
            # model loaded in single-model mode) is a real error.
            miss = f"API error (404): {k.capitalize()} '{name}' not found"
            if kind is not None or str(exc) != miss:
                raise
    if not found:
        raise ToolError(
            f"No dimension, measure, metric or rule named '{name}' in this model. "
            "Use find_artefacts or list_rules to find the exact name."
        )
    if len(found) > 1 and fmt in ("mermaid", "turtle"):
        raise ToolError(
            f"'{name}' names a {' and a '.join(k for k, _ in found)}; pass kind to pick one."
        )
    if fmt == "json":
        payload = {k: _parse_json(r) for k, r in found}
        return json.dumps(payload[found[0][0]] if len(found) == 1 else payload, indent=2)
    return "\n\n".join(_lineage_result(r, fmt, f"Lineage of {k} '{name}'") for k, r in found)


def _impl_query_lineage(
    model_id: str | None, query_json: str, dialect: str | None, fmt: str
) -> str:
    """Lineage of a QueryObject, including the joins the planner chose."""
    try:
        query = json.loads(query_json)
    except json.JSONDecodeError as exc:
        raise ToolError(
            f"Invalid query JSON: {exc}. The QueryObject schema is available via "
            "get_json_schema('query')."
        ) from exc
    params: dict[str, str] = {"format": _lineage_api_format(fmt)}
    if model_id is None:
        if dialect is not None:
            params["dialect"] = dialect
        resp = _shortcut_request("POST", "/query/lineage", json_body=query, params=params)
    else:
        body: dict = {"model_id": model_id, "query": query}
        if dialect is not None:
            body["dialect"] = dialect
        resp = _session_request("POST", "/query/lineage", json_body=body, params=params)
    return _lineage_result(resp, fmt, "Lineage of the query")


def _impl_get_lineage(
    model_id: str | None,
    name: str | None,
    kind: str | None,
    query_json: str | None,
    dialect: str | None,
    fmt: str,
) -> str:
    """Dispatch to artefact or query lineage (shared implementation)."""
    if (name is None) == (query_json is None):
        raise ToolError("Pass exactly one of name (an artefact) or query_json (a query).")
    if query_json is not None:
        if kind is not None:
            raise ToolError("kind applies to a named artefact, not to query_json.")
        return _impl_query_lineage(model_id, query_json, dialect, fmt)
    assert name is not None
    return _impl_artefact_lineage(model_id, name, kind, fmt)


def _lineage_tables(model_id: str | None, kind: str, name: str) -> list[str]:
    """Best-effort: the data objects an artefact reads, from its lineage."""
    path = f"/{_LINEAGE_KINDS[kind]}/{quote(name, safe='')}/lineage"
    try:
        data = _parse_json(_model_request(model_id, "GET", path))
    except Exception:  # noqa: BLE001 — enrichment must never break the caller
        return []
    return [
        f"{n.get('name', '?')} ({n['detail']})" if n.get("detail") else n.get("name", "?")
        for n in data.get("nodes") or []
        if n.get("kind") == "data_object"
    ]


# ---------------------------------------------------------------------------
# Tool registration (mode-dependent)
# ---------------------------------------------------------------------------


def _resolve_model_id(model_id: str | None) -> str | None:
    """Normalize ``model_id`` for the active API mode.

    Single-model mode has one pre-loaded model, so any passed id is ignored and
    ``None`` is returned (routing the shared ``_impl_*`` to the shortcut
    endpoints). Multi-model mode requires an explicit id — a missing one is a
    clear error rather than a silent fall-through to the wrong endpoint.
    """
    if _single_model_mode:
        return None
    if not model_id:
        raise ToolError(
            "model_id is required (multi-model mode) — load a model first "
            "and pass the id it returns."
        )
    return model_id


def _register_model_tools() -> None:
    """Register the model-scoped tool surface — one definition per tool.

    Each tool takes an optional ``model_id`` normalized by ``_resolve_model_id``:
    ignored in single-model mode (one pre-loaded model), required at call time in
    multi-model mode. Tools that exist in only one mode (``get_model`` for
    single-model; ``load_model`` / ``remove_model`` / ``list_models`` /
    ``run_batch`` for multi-model) are registered conditionally. ``execute_query``
    is always registered and gated by the ``query_execute`` capability at
    list/call time (see ``PhaseMiddleware``).

    ``model_id`` is a loaded model's id in multi-model mode; omit it
    in single-model mode, where the one pre-loaded model is always used.
    """

    # ----- introspection -----

    @mcp.tool(title="Describe Model", annotations=_READ_ONLY)
    def describe_model(model_id: str | None = None) -> str:
        """Describe the contents of the model.

        Shows data objects (with columns and joins), dimensions, measures, and
        metrics.  In multi-model mode, use this after loading a model to explore
        its structure.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_describe_model(_resolve_model_id(model_id))

    @mcp.tool(title="Model Diagram", annotations=_READ_ONLY)
    def get_model_diagram(
        model_id: str | None = None,
        show_columns: bool = True,
        theme: Literal["default", "dark", "forest", "neutral", "base"] = "default",
    ) -> str:
        """Generate a Mermaid ER diagram for the model.

        Returns a Mermaid diagram script that visualises the data objects,
        columns, and join relationships in the model.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
            show_columns: Whether to include column details in the diagram.
            theme: Mermaid diagram theme — one of the built-in themes
                "default", "dark", "forest", "neutral", or "base".
        """
        return _impl_get_model_diagram(_resolve_model_id(model_id), show_columns, theme)

    @mcp.tool(title="Join Graph", annotations=_READ_ONLY)
    def get_join_graph(model_id: str | None = None) -> str:
        """Return the join graph as an adjacency list.

        Shows the data object nodes and join edges (with cardinality and join
        columns) in the model.  Useful for understanding table relationships.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_join_graph(_resolve_model_id(model_id))

    @mcp.tool(title="Find Composables", annotations=_READ_ONLY)
    def find_composables(
        query_json: str | None = None,
        anchors: list[str] | str | None = None,
        anchor_type: Literal["dimension", "measure", "metric", "dataObject"] | None = None,
        model_id: str | None = None,
    ) -> str:
        """Resolve which artefacts can still be added to a query (ACR).

        Artefacts Composability Resolution: given an anchor — your in-progress
        query, or one or more named artefacts — returns the dimensions, measures,
        and metrics that remain composable with it and are guaranteed to compile
        into a valid, fanout-free result. It walks the same join graph the
        compiler uses, so anything it reports is safe to add.

        Supply the anchor exactly one of two ways:

        - ``query_json`` — an in-progress QueryObject as a JSON string
          (recommended). Its ``select`` dimensions and measures form the anchor.
        - ``anchors`` — one or more artefact names. Repeated names are
          intersected. Omit both for a fresh query, where everything composes.

        Directly composable artefacts share a fanout-safe root with the anchor.
        CFL measures/metrics come from an independent fact and compose only
        through the Composite Fact Layer (UNION ALL at the shared grain); they
        are reported separately.

        Args:
            query_json: In-progress QueryObject as a JSON string (the anchor).
                Mutually exclusive with ``anchors``.
            anchors: One or more named anchors — a list, a JSON-array string, or
                a single name. Mutually exclusive with ``query_json``.
            anchor_type: Disambiguate named anchors to one kind (dimension,
                measure, metric, dataObject). Only applies with ``anchors``.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        if query_json is not None and anchors is not None:
            raise ToolError(
                "Provide exactly one anchor source: 'query_json' (a QueryObject) "
                "or 'anchors' (named artefacts), not both."
            )
        return _impl_find_composables(
            _resolve_model_id(model_id),
            query_json,
            _coerce_anchor_list(anchors),
            anchor_type,
        )

    @mcp.tool(title="Model Graph", annotations=_READ_ONLY)
    def get_model_graph(model_id: str | None = None) -> str:
        """Get the OBSL-Core RDF graph for the model as Turtle.

        Returns the semantic model's RDF graph serialized in Turtle format.
        The graph follows the OBSL-Core ontology and can be used for semantic
        web integration or further analysis.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_graph(_resolve_model_id(model_id))

    @mcp.tool(title="SPARQL Model Graph Query", annotations=_READ_ONLY)
    def query_model_graph_by_sparql(query: str, model_id: str | None = None) -> str:
        """Execute a read-only SPARQL query against the model's RDF graph.

        Supports SELECT and ASK queries only (no INSERT/DELETE/UPDATE).
        The graph uses the OBSL-Core ontology.

        Args:
            query: SPARQL query string (SELECT or ASK).
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_sparql_query(_resolve_model_id(model_id), query)

    # ----- discovery -----

    @mcp.tool(title="Find Artefacts", annotations=_READ_ONLY)
    def find_artefacts(
        query: str | None = None,
        kind: Literal["dimension", "measure", "metric"] | None = None,
        name: str | None = None,
        model_id: str | None = None,
    ) -> str:
        """Look up model artefacts (dimensions, measures, metrics).

        Two modes, selected by whether you pass ``query``:

        - ``query`` set → fuzzy, ranked search. Matches names and synonyms
          (exact, synonym, and fuzzy/partial) and returns ranked candidates —
          for "I don't know the exact name". ``name`` is ignored in this mode.
        - ``query`` omitted → exact, deterministic, complete enumeration (the
          authoritative set). No args → every dimension, measure, and metric;
          ``kind`` only → the complete set of that one kind; ``name`` → that
          exact artefact (optionally constrained to ``kind``).

        ``kind`` narrows either mode to one artefact kind.

        Args:
            query: Search term (matched against names and synonyms). Omit for
                deterministic enumeration instead of ranked search.
            kind: Restrict to one artefact kind (dimension, measure, metric).
            name: In enumeration mode, return only the artefact with this exact
                name. Ignored when ``query`` is set.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        resolved = _resolve_model_id(model_id)
        if query is not None:
            return _impl_find_artefacts(resolved, query, kind)
        return _impl_list_artefacts(resolved, kind, name)

    @mcp.tool(title="Explain Artefact", annotations=_READ_ONLY)
    def explain_artefact(name: str, model_id: str | None = None) -> str:
        """Explain the lineage of a dimension, measure, or metric.

        Traces the composition chain from the named artefact down to the
        underlying data objects and columns.  Useful for understanding how a
        measure is computed or where a dimension originates.  For the full
        graph (with rules, joins and pathNames, or a whole query's lineage, as
        Mermaid or Turtle), use ``get_lineage``.

        Args:
            name: The dimension, measure, or metric name to explain.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_explain_artefact(_resolve_model_id(model_id), name)

    @mcp.tool(title="Search Dimension Values", annotations=_READ_ONLY_OPEN_WORLD)
    def search_values(
        dimension: str,
        search: str | None = None,
        where_json: str | None = None,
        limit: int | None = None,
        dialect: str | None = None,
        model_id: str | None = None,
    ) -> str:
        """List or search the values a dimension holds, to ground a filter value.

        Use before writing a filter such as ``Country = 'Germany'`` when you do
        not know how the column spells it (``Germany``, ``DE``, ``GERMANY``): a
        wrong guess returns no rows rather than an error.

        - ``search`` set → case-insensitive match ranked exact, prefix, contains,
          then fuzzy for misspellings (``Germny`` finds ``Germany``) where the
          dimension has few enough values.
        - ``search`` omitted → the values listed in order; a date or number
          dimension returns its minimum and maximum instead.

        Each step is an executed dimension-only query, so the model's static
        filters (a tenant filter included) restrict the values. Requires query
        execution. A dimension declaring ``valueSearch: false`` is refused.

        Args:
            dimension: The dimension whose values are listed or searched.
            search: Term to look for. Omit to list the values (or the range).
            where_json: Query filters narrowing the values, as a JSON array of
                ``where`` items (same shape as a QueryObject's ``where``).
            limit: Values to return (API default 20, capped server-side).
            dialect: Target SQL dialect. When omitted the API resolves via
                model.settings.defaultDialect → server default.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_search_values(
            _resolve_model_id(model_id), dimension, search, where_json, limit, dialect
        )

    @mcp.tool(title="Lineage", annotations=_READ_ONLY)
    def get_lineage(
        name: str | None = None,
        kind: str | None = None,
        query_json: str | None = None,
        dialect: str | None = None,
        output_format: str = "text",
        model_id: str | None = None,
    ) -> str:
        """Lineage graph of an artefact or a query, down to the tables it reads.

        Pass ``name`` for a dimension, measure, metric or business rule, or
        ``query_json`` for a whole query. The graph follows references down to
        data objects: columns (computed ones included), filter and grain
        columns, the measures and metrics a metric or rule reads, rules a rule
        references, and for a query its ``select.fields`` columns, ``exists``
        subqueries, and the joins the planner chose (with their ``pathName``,
        role-playing joins included), across every leg of a multi-fact query
        (the legs meet in a ``union`` node). Compiles only, never executes.

        Args:
            name: Artefact name. Mutually exclusive with ``query_json``.
            kind: "dimension", "measure", "metric" or "rule". Optional: without
                it every kind is tried, and a name used by two kinds (a rule
                named like a measure) returns both.
            query_json: A QueryObject as a JSON string (as for
                ``execute_query``). Mutually exclusive with ``name``.
            dialect: SQL dialect to plan the query for (``query_json`` only);
                defaults to the model's, then the server's.
            output_format: "text" (default: nodes by kind, then edges), "mermaid"
                (a flowchart, sources on the left), "turtle" (``prov:wasDerivedFrom``
                edges over the OBSL graph's IRIs; merges with ``get_model_graph``)
                or "json" (the raw nodes, edges and Mermaid).
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_lineage(
            _resolve_model_id(model_id), name, kind, query_json, dialect, output_format
        )

    @mcp.tool(title="List Examples", annotations=_READ_ONLY)
    def list_examples(intent: str | None = None, model_id: str | None = None) -> str:
        """List canonical example queries authored alongside the model.

        Returns each example's name, description, and intent tags.  Use
        ``get_example`` for full detail (query payload + compiled SQL preview).

        Args:
            intent: Optional intent-tag filter.  Falls back through exact →
                contains → fuzzy tag matching.  When no examples match,
                the server returns a ``suggestion`` listing available tags.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_list_examples(_resolve_model_id(model_id), intent)

    @mcp.tool(title="Get Example", annotations=_READ_ONLY)
    def get_example(name: str, model_id: str | None = None) -> str:
        """Get a single example by name with its query and compiled SQL preview.

        Args:
            name: The example's ``name`` field.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_get_example(_resolve_model_id(model_id), name)

    # ----- business rules -----

    @mcp.tool(title="List Business Rules", annotations=_READ_ONLY)
    def list_rules(model_id: str | None = None) -> str:
        """List the model's business rules with statistics.

        Rules are conditions over dimensions, measures and metrics declared in
        the model's ``rules:`` block. Each is row-level (dimensions only, a
        ``WHERE`` predicate) or aggregate (a measure or metric at a declared
        ``grain``, a ``HAVING`` condition). ``classification`` and
        ``eligibility`` rules describe members; ``validation`` and
        ``constraint`` rules state an invariant whose violations are reported.
        Statistics by type, level, severity and executability come first.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_list_rules(_resolve_model_id(model_id))

    @mcp.tool(title="Explain Business Rule", annotations=_READ_ONLY)
    def explain_rule(name: str, model_id: str | None = None) -> str:
        """Explain one business rule: definition, what it reads, the tables
        behind it, dependencies, ontology links, and the SQL it compiles to for
        the session's dialect. ``get_lineage(name, kind="rule")`` draws the graph.

        Args:
            name: The rule's name (as listed by ``list_rules``).
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_explain_rule(_resolve_model_id(model_id), name)

    @mcp.tool(title="Evaluate Business Rule", annotations=_READ_ONLY_OPEN_WORLD)
    def evaluate_rule(
        name: str,
        limit: int | None = None,
        dialect: str | None = None,
        format_values: bool = False,
        model_id: str | None = None,
    ) -> str:
        """Run one business rule and list its findings.

        Findings are the members for a classification or eligibility rule
        (the rows the condition selects) and the violations for a validation
        or constraint rule (the rows that break it). Runs through the same
        cache-aware pipeline as ``execute_query``, so it needs query execution
        enabled on the API.

        Args:
            name: The rule's name.
            limit: Max findings to return (the API's default row limit applies
                when omitted).
            dialect: SQL dialect override; defaults to the model's effective one.
            format_values: Locale-format numbers and dates in the rows.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_evaluate_rule(_resolve_model_id(model_id), name, limit, dialect, format_values)

    @mcp.tool(title="Evaluate Business Rules", annotations=_READ_ONLY_OPEN_WORLD)
    def evaluate_rules(
        types: list[str] | None = None,
        severities: list[str] | None = None,
        executable_only: bool = False,
        max_rules: int | None = None,
        limit: int = 5,
        dry_run: bool = False,
        dialect: str | None = None,
        model_id: str | None = None,
    ) -> str:
        """Run every business rule (or a subset) into one report.

        Per rule: status (``executed``, ``compiled`` on a dry run, ``skipped``,
        ``failed``), the finding count, a few sample findings and any error.
        A failure never hides the other rules. ``dry_run=True`` compiles every
        rule without executing anything, which also works when query execution
        is disabled on the API.

        Args:
            types: Only these rule types (classification, eligibility,
                validation, constraint).
            severities: Only these severities (error, warning, info).
            executable_only: Skip rules that do not compile instead of
                reporting them as failed.
            max_rules: Stop after this many rules.
            limit: Sample findings to include per rule (default 5).
            dry_run: Compile only; report status without running.
            dialect: SQL dialect override; defaults to the model's effective one.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_evaluate_rules(
            _resolve_model_id(model_id),
            types,
            severities,
            executable_only,
            max_rules,
            limit,
            dry_run,
            dialect,
        )

    # ----- ontology links -----

    @mcp.tool(title="Find Concept Mappings", annotations=_READ_ONLY)
    def find_concept_mappings(
        concept: str | None = None,
        namespace: str | None = None,
        relation: str | None = None,
        types: list[str] | None = None,
        model_id: str | None = None,
    ) -> str:
        """List the model's links into external ontologies.

        Every ``externalConceptMappings`` entry with the artefact it sits on,
        the SKOS relation (``exact``, ``close``, ``broader`` meaning the
        external concept is the broader one, ``narrower``, ``related``), the
        concept expanded to an absolute IRI, and its provenance. Use it to go
        from an external term ("schema:Product") to the artefacts that mean it.

        Args:
            concept: Only mappings to this concept (compact or full IRI).
            namespace: Only mappings into this namespace (prefix name or IRI).
            relation: Only this relation.
            types: Only artefacts of these types (model, dataObject, dimension,
                measure, metric, rule).
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_find_concept_mappings(
            _resolve_model_id(model_id), concept, namespace, relation, types
        )

    @mcp.tool(title="List Concept Namespaces", annotations=_READ_ONLY)
    def list_concept_namespaces(model_id: str | None = None) -> str:
        """Which external ontologies the model links into, most used first,
        plus declared prefixes that no mapping uses yet.

        Args:
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_list_concept_namespaces(_resolve_model_id(model_id))

    @mcp.tool(title="List Unmapped Artefacts", annotations=_READ_ONLY)
    def list_unmapped_artefacts(types: list[str] | None = None, model_id: str | None = None) -> str:
        """List the artefacts that still have no external concept mapping.

        The governance gap list: everything in the mappable scope (model,
        data objects, dimensions, measures, metrics, rules) without a link.

        Args:
            types: Only these artefact types.
            model_id: a loaded model's id (multi-model); omit in single-model.
        """
        return _impl_list_unmapped_artefacts(_resolve_model_id(model_id), types)

    # ----- compile / plan -----

    # ----- execute (always registered; gated by the query_execute capability) -----

    @mcp.tool(title="Execute Query", annotations=_READ_ONLY_OPEN_WORLD)
    def execute_query(
        query_json: str,
        model_id: str | None = None,
        dialect: str | None = None,
        output_format: str = "json",
        format_values: bool | None = None,
        locale: str | None = None,
        timezone: str | None = None,
    ) -> str:
        """Compile and execute a semantic query (QueryObject), returning SQL + results.

        Pass ``query_json`` — a QueryObject as a JSON string. If no ``limit`` is set,
        a server-side default row limit applies. Measures come back in the order
        the query selects them (API 3.0). A query reading data objects of several
        data sources is answered across them: the response's ``federation`` lists
        each source's query, and ``sql`` is the DuckDB query combining them.

        A refused query fails with every error's code, ``hint`` and did-you-mean
        suggestions: correct the query from them and retry.

        Args:
            query_json: Complete QueryObject as a JSON string: a ``select`` of
                dimensions/measures (or ``fields``), plus optional ``where``,
                ``having``, ``orderBy``, ``limit``, ``offset``, ``dimensionsExclude``,
                ``usePathNames``, ``asOf``, ``fill`` (every period of the time
                dimension, empty ones too), and coalesce groups. Dimension/measure names must
                match those defined in the loaded model.
            model_id: a loaded model's id (multi-model); omit in single-model.
            dialect: Target SQL dialect. When omitted the API resolves via
                model.settings.defaultDialect → server default.
            output_format: Response format — "json" (default) or "tsv".
            format_values: Format numeric cells as display strings.
            locale: BCP-47 locale for number formatting (e.g. "de").
            timezone: IANA timezone (e.g. "Europe/Berlin") labelling result
                timestamps that carry none. A model's ``settings.queryTimezone``
                wins over it: its timestamps are wall-clock times in that zone.
        """
        return _impl_execute_query(
            _resolve_model_id(model_id),
            query_json,
            dialect=dialect,
            output_format=output_format,
            format_values=format_values,
            locale=locale,
            timezone=timezone,
        )

    # ----- both modes: stateless validation of a model given inline -----

    if _single_model_mode:

        @mcp.tool(title="Validate Model", annotations=_READ_ONLY_OPEN_WORLD)
        def validate_model(
            model: dict | str | None = None,
            model_yaml: str | None = None,
            online: bool = False,
            dialect: str | None = None,
            sample: int | str | None = None,
        ) -> str:
            """Validate an OBML model without loading it.

            Reports every structural finding the loader would report — unknown
            columns, unreachable joins, malformed expressions — but stores
            nothing and returns no model_id. Use it to check a draft before
            committing it with ``load_model``.

            With ``online=true`` the model is additionally checked against the
            live datasource: each data object is read as a compiled query reads
            it (its table, its columns, each computed column's expression, a
            nested data object's fields), and drift a purely structural check
            cannot see — a dropped table, a renamed column, a column whose type
            no longer matches its ``abstractType``, an expression the database
            rejects — is reported as an error (``DATASOURCE_*`` codes). The
            probe costs one ``SELECT … LIMIT 0`` per data object: a plan, no
            scan. ``sample`` also reads some rows of each, for what only a
            value shows (API 3.1), reported as warnings.

            Args:
                model: OBML model as a JSON object (camelCase keys, joins inside
                    each dataObject). Mutually exclusive with model_yaml.
                model_yaml: OBML model as a YAML string. Mutually exclusive
                    with model.
                online: When true, also probe the configured datasource for the
                    tables and columns the model declares. Default false — offline
                    validation opens no connection.
                dialect: Datasource to probe when ``online`` is set; defaults to
                    the dialect of the data source the model names
                    (``settings.defaultDataSource``), then the server's
                    ``DB_VENDOR``. This names a *connection to open*, not SQL to
                    generate, so unlike the ``dialect`` on the query tools it
                    does not fall back to the model's ``settings.defaultDialect``.
                    Ignored when ``online`` is false and no ``sample`` is given.
                sample: Also read a sample of each data object's rows. A count
                    (``1000``) reads the first that many rows; the API sets no
                    upper bound, so keep it small. A percent (``"1%"``) draws
                    about that share row by row and returns at most 1000 rows,
                    but may scan many more to find them, up to the whole
                    table. On BigQuery a count is billed for the whole columns
                    it reads, while a percent of a table reads about that
                    share of its storage. A computed column or nested field
                    failing on a value is ``DATASOURCE_SAMPLE_FAILED``, a
                    nested field NULL on every sampled element
                    ``DATASOURCE_SAMPLE_ALL_NULL`` (warnings). Implies
                    ``online``.

            ``extends`` / ``inherits`` are absent by design: the stateless
            /validate route this mode uses ignores both, so offering them would
            promise a merge that never happens. Validate the merged document.
            """
            return _impl_validate_model(model, model_yaml, None, None, online, dialect, sample)

    else:

        @mcp.tool(title="Validate Model", annotations=_READ_ONLY_OPEN_WORLD)
        def validate_model(
            model: dict | str | None = None,
            model_yaml: str | None = None,
            extends: list[str] | str | None = None,
            inherits: str | None = None,
            online: bool = False,
            dialect: str | None = None,
            sample: int | str | None = None,
        ) -> str:
            """Validate an OBML model without loading it.

            Reports every structural finding the loader would report — unknown
            columns, unreachable joins, malformed expressions — but stores
            nothing and returns no model_id. Use it to check a draft before
            committing it with ``load_model``.

            With ``online=true`` the model is additionally checked against the
            live datasource: each data object is read as a compiled query reads
            it (its table, its columns, each computed column's expression, a
            nested data object's fields), and drift a purely structural check
            cannot see — a dropped table, a renamed column, a column whose type
            no longer matches its ``abstractType``, an expression the database
            rejects — is reported as an error (``DATASOURCE_*`` codes). The
            probe costs one ``SELECT … LIMIT 0`` per data object: a plan, no
            scan. ``sample`` also reads some rows of each, for what only a
            value shows (API 3.1), reported as warnings.

            Args:
                model: OBML model as a JSON object (camelCase keys, joins inside
                    each dataObject). Mutually exclusive with model_yaml.
                model_yaml: OBML model as a YAML string. Mutually exclusive
                    with model.
                extends: Optional list of analytical-fragment YAML strings to
                    merge into the model before validating.
                inherits: Optional model_id of an already-loaded parent model in
                    the session whose data objects and joins the model inherits.
                online: When true, also probe the configured datasource for the
                    tables and columns the model declares. Default false — offline
                    validation opens no connection.
                dialect: Datasource to probe when ``online`` is set; defaults to
                    the dialect of the data source the model names
                    (``settings.defaultDataSource``), then the server's
                    ``DB_VENDOR``. This names a *connection to open*, not SQL to
                    generate, so unlike the ``dialect`` on the query tools it
                    does not fall back to the model's ``settings.defaultDialect``.
                    Ignored when ``online`` is false and no ``sample`` is given.
                sample: Also read a sample of each data object's rows. A count
                    (``1000``) reads the first that many rows; the API sets no
                    upper bound, so keep it small. A percent (``"1%"``) draws
                    about that share row by row and returns at most 1000 rows,
                    but may scan many more to find them, up to the whole
                    table. On BigQuery a count is billed for the whole columns
                    it reads, while a percent of a table reads about that
                    share of its storage. A computed column or nested field
                    failing on a value is ``DATASOURCE_SAMPLE_FAILED``, a
                    nested field NULL on every sampled element
                    ``DATASOURCE_SAMPLE_ALL_NULL`` (warnings). Implies
                    ``online``.
            """
            return _impl_validate_model(
                model, model_yaml, extends, inherits, online, dialect, sample
            )

    # ----- single-model only: the pre-loaded model's source -----

    if _single_model_mode:

        @mcp.tool(title="Get Model", annotations=_READ_ONLY)
        def get_model() -> str:
            """Get the pre-loaded OBML YAML model source.

            Returns the original OBML YAML that was loaded into the API at
            startup.  Useful for understanding the model definition in the
            author's terms.  (Single-model mode only.)
            """
            resp = _api_request("GET", f"{_API_V1}/settings", retry_on_expired=False)
            data = _parse_json(resp)
            yaml_content = data.get("model_yaml")
            if not yaml_content:
                raise ToolError("No model YAML available from the API")
            return yaml_content

        return

    # ----- multi-model only: session/model management + one-shot batch -----

    @mcp.tool(title="Load Model", annotations=_SESSION_WRITE)
    def load_model(
        model: dict | str | None = None,
        osi_yaml: str | None = None,
        extends: list[dict] | str | None = None,
        inherits: str | None = None,
        dedup: bool = True,
        datasource: str | None = None,
    ) -> str:
        """Load a semantic model and return a model_id.

        Provide exactly one source: ``model`` (native OBML JSON) or ``osi_yaml``
        (OSI YAML, converted to OBML server-side, surfacing conversion warnings).

        Args:
            model: OBML model as a JSON object with top-level keys: ``version``,
                ``dataObjects``, ``dimensions``, ``measures``, ``metrics``
                (camelCase throughout). Joins live INSIDE each dataObject
                (``joins`` list), not at the top level, and reference OBML column
                names. Supports ``extends``/``inherits``. Mutually exclusive with
                osi_yaml.
            osi_yaml: OSI model as a YAML string. Converted to OBML server-side
                before loading. Surfaces OSI → OBML conversion warnings and advisory
                OSI-schema validation alongside the model summary.
                ``extends``/``inherits`` do not apply to this source. Mutually
                exclusive with model.
            extends: Optional list of analytical fragment objects (dimensions,
                measures, metrics) to merge into the model before loading
                (OBML source only).
            inherits: Optional model_id of an already-loaded parent model whose
                data objects and joins the child inherits (OBML source only).
            dedup: When true (default), reuse the model_id of identical OBML
                already loaded in the session. Pass false to force a fresh load.
            datasource: Optional name of a configured data source (see
                ``list_datasources``) to run this model's queries on, replacing
                its ``settings.defaultDataSource``. The same model loaded with
                another data source is another model, with its own model_id.
                Where the API executes queries, an unknown name or one of
                another dialect fails the load (``DATA_SOURCE_UNAVAILABLE``,
                with did-you-mean suggestions). OBML source only.
        """
        if osi_yaml is not None:
            if (
                model is not None
                or extends is not None
                or inherits is not None
                or datasource is not None
            ):
                raise ToolError(
                    "Provide exactly one source: 'osi_yaml' cannot be combined "
                    "with 'model', 'extends', 'inherits', or 'datasource'."
                )
            return _impl_load_model_from_osi(osi_yaml, dedup)
        return _impl_load_model(model, extends, inherits, dedup, datasource)

    @mcp.tool(title="Remove Model", annotations=_SESSION_DELETE)
    def remove_model(model_id: str) -> str:
        """Remove a model from the current session.

        Args:
            model_id: The id of the model to remove.
        """
        return _impl_remove_model(model_id)

    @mcp.tool(title="List Models", annotations=_READ_ONLY)
    def list_models() -> str:
        """List all models currently loaded in a session."""
        resp = _session_request("GET", "/models")
        models = _parse_json(resp)
        if not models:
            return "No models loaded.  Use load_model to load one."
        lines = ["Loaded models:", ""]
        for m in models:
            lines.append(
                f"  {m['model_id']}  "
                f"({m['data_objects']} objects, {m['dimensions']} dims, "
                f"{m['measures']} measures, {m['metrics']} metrics)"
            )
        return "\n".join(lines)

    @mcp.tool(title="Export Model to OSI", annotations=_READ_ONLY)
    def export_model_to_osi(
        model_id: str,
        model_name: str = "semantic_model",
        model_description: str = "",
        ai_instructions: str = "",
        include_ontology: bool = False,
    ) -> str:
        """Export a loaded model as OSI (Open Semantic Interchange) YAML.

        Converts a model already loaded in the session (its faithful OBML
        source) to OSI format. Returns the OSI YAML plus any conversion
        warnings and validation results.

        Args:
            model_id: id of a loaded model.
            model_name: Name for the exported OSI model.
            model_description: Description for the OSI model.
            ai_instructions: AI instructions for the OSI model.
            include_ontology: Deprecated and removed — do not set. The OSI
                ontology emit no longer exists; setting this is an error.
                Kept only so callers passing the old default (false) are not
                rejected.
        """
        return _impl_export_model_to_osi(
            _resolve_model_id(model_id),
            model_name,
            model_description,
            ai_instructions,
            include_ontology,
        )

    @mcp.tool(title="Run Query Batch", annotations=_BATCH)
    def run_batch(
        queries: list[dict],
        model_yaml: str | None = None,
        model_id: str | None = None,
        dialect: str | None = None,
        execute: bool = False,
        max_parallelism: int | None = None,
        fail_fast: bool = False,
        persist_model: bool = False,
        dedup: bool = True,
        session_id: str | None = None,
    ) -> str:
        """Run N independent queries against one model in a single round trip.

        Loads (or references) one OBML model, then runs every query in
        ``queries`` in parallel. Returns the raw JSON response (one result per
        query, keyed by ``id``, stable order). Provide exactly one of
        ``model_yaml`` or ``model_id``.

        Each ``queries`` item is a dict ``{"id": "q1", "query": {...},
        "execute": true, "dialect": "snowflake"}``; ``id`` is optional (server
        auto-assigns ``q0``, ``q1``, …) and per-item ``execute``/``dialect``
        override the batch defaults. Per-query partial failure is the default;
        ``fail_fast`` cancels the rest on first failure. A failed query's
        ``error`` carries its first error's ``hint``, ``suggestions`` and
        ``context``, and ``errors`` lists every one (API 3.0): correct the
        query from them. A query across data sources carries ``federation``.

        Args:
            queries: List of query items (see above).
            model_yaml: OBML YAML string (mutually exclusive with model_id).
            model_id: ID of an already-loaded model (requires session_id).
            dialect: Default dialect for queries that omit one.
            execute: Default execute flag (compile-only when false).
            max_parallelism: Cap on concurrent executions; server caps further.
            fail_fast: If true, cancel remaining queries on first failure.
            persist_model: Keep a yaml-loaded model in the session afterwards.
            dedup: Reuse an already-loaded identical OBML model_id (default).
            session_id: Existing session to reuse; otherwise the API creates
                a new one for the batch.
        """
        return _impl_run_batch(
            model_yaml=model_yaml,
            model_id=model_id,
            queries=queries,
            dialect=dialect,
            execute=execute,
            max_parallelism=max_parallelism,
            fail_fast=fail_fast,
            persist_model=persist_model,
            dedup=dedup,
            session_id=session_id,
        )


def _setup_mode_tools(detected: tuple[bool, bool] | None = None) -> None:
    """Detect API mode and register the tool surface. Idempotent.

    ``detected`` is a :func:`_detect_api_mode` result already fetched (lazy
    init fetches it in a worker thread); without it the API is asked here.
    """
    global _single_model_mode, _query_execute_enabled, _tools_registered
    if _tools_registered:
        return
    _single_model_mode, _query_execute_enabled = detected or _detect_api_mode()
    logger.info(
        "%s mode detected",
        "Single-model" if _single_model_mode else "Multi-model",
    )
    _register_model_tools()
    logger.info(
        "Query execution %s (execute_* tools gated by capability)",
        "enabled" if _query_execute_enabled else "disabled",
    )
    _tools_registered = True


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


class StaticPrompt(_BasePrompt):
    """Prompt with static text exposed in prompts/list for Horizon compat."""

    text: str

    def to_mcp_prompt(self, **overrides):  # type: ignore[override]
        # The text rides in `_meta`, not as a top-level attribute: the SDK's
        # Prompt model rejects unknown attributes (mcp 2.x), so assigning
        # `result.text` raises and takes the whole prompts/list response with it.
        meta = {**(overrides.get("_meta") or self.get_meta() or {}), "text": self.text}
        return super().to_mcp_prompt(**{**overrides, "_meta": meta})

    async def render(self, _arguments=None):  # type: ignore[override]
        return self.text


_WRITE_QUERY_TEXT = """\
# Querying with OrionBelt

Queries are passed to `execute_query` as a single `query_json` argument — a
complete **QueryObject** encoded as a JSON string. Call
`get_json_schema("query")` for the authoritative QueryObject schema, and
`describe_model` to discover the dimension / measure / metric names to use.

## QueryObject shape

```
execute_query(
  model_id="abc12345",
  query_json='{
    "select": {
      "dimensions": ["Customer Country"],
      "measures": ["Total Revenue"]
    },
    "where": [
      {"field": "Customer Country", "op": "equals", "value": "US"}
    ],
    "orderBy": [
      {"field": "Total Revenue", "direction": "desc"}
    ],
    "limit": 10
  }'
)
```

A minimal query is just `{"select": {"dimensions": [...], "measures": [...]}}`.
The fields below (filters, groups, dimensionsExclude, coalesce, raw `fields`,
…) all go **inside** `query_json`.

## Filter Operators

- Equality: `equals`, `notequals`, `=`, `!=`
- Comparison: `gt`, `gte`, `lt`, `lte`, `>`, `>=`, `<`, `<=`
- Set: `in`, `not_in`, `inlist`, `notinlist`
- Null: `is_null`, `is_not_null`, `set`, `notset`
- String: `contains`, `notcontains`, `like`, `notlike`, `ilike`, `notilike`
  (case-insensitive, OBSL 2.34+), `starts_with`, `ends_with`
- Regex: `regex`, `notregex` (per-dialect native syntax)
- Blank: `blank` (NULL or empty/whitespace), `notblank`
- Length: `length_eq`, `length_gt`, `length_lt` (value must be integer)
- Range: `between`, `notbetween`, `relative`

`relative` takes `{unit: day|week|month|quarter|year, count, direction?:
past|future, include_current?, calendar?, toDate?}`.  It counts units back
from today (`{unit: month, count: 1}` is the 30-odd days before today);
`calendar: true` (OBSL 2.36+) takes whole calendar periods from the start of
the current one: `{unit: quarter, count: 1, calendar: true}` is this quarter,
`include_current: false` makes it the last full one, and `toDate: true` ends
the current period today (year to date).  A week starts on the model's
`weekStart`.

On a date or timestamp field a **period literal** (OBSL 2.36+) stands for the
whole period: `= '2025-Q1'` is the quarter, `<= '2024-12'` runs through the
last moment of 31 December, `between ['2025-01', '2025-03']` covers three
months, and `in` may mix periods and ISO dates.  Also inside `exists`
subqueries.

Unsure how a column spells a value (`Germany`, `DE`, `GERMANY`)?  A wrong
guess returns no rows, not an error: call `search_values` (OBSL 2.36+) for
the dimension first.

## Filter Groups (AND/OR/NOT)

`where` and `having` arrays accept both leaf filters and filter groups
for complex boolean expressions:

```json
"where": [
  {"logic": "or", "filters": [
    {"field": "Customer Country", "op": "equals", "value": "US"},
    {"field": "Customer Country", "op": "equals", "value": "DE"}
  ]},
  {"field": "Order Status", "op": "notequals", "value": "Cancelled"}
]
```

Top-level items are combined with AND.  Groups support:
- `logic`: `"and"` (default) or `"or"`
- `filters`: array of leaf filters or nested groups (recursive)
- `negated`: `true` to negate the entire group (NOT)

## Qualified Column References (WHERE only)

WHERE filter fields accept three syntaxes:
- Dimension name: `"Customer Country"`
- Qualified column: `"Orders.Order Priority"` (DataObject.Column)
- The data object must be reachable from the query's join graph (auto-joined)

HAVING filter fields reference a measure or metric name.

## dimensionsExclude

Set `"dimensionsExclude": true` to return dimension value combinations that
do NOT exist in the data (anti-join via EXCEPT).  Requires 2+ dimensions on
independent branches and no measures.

## allowFanOut

A measure reading both a base-grain column and one from a data object the
joins replicate is evaluated per base row — right for an extended price
(`quantity * list price`), wrong for anything reading the replicated row's
own magnitude.  Nothing in the model separates the two, so the compiler
warns rather than refusing.  Set `"allowFanOut": true` on the query to
record that the duplication is understood and intended, which silences the
warning.  The generated SQL is identical either way — this suppresses a
diagnostic, it does not change the result.

## asOf (cumulative metrics without their time dimension)

A cumulative metric may be queried *without* selecting its `timeDimension`
(OBSL 2.34+): `Region` + `YTD Sales` gives each region its year-to-date as
of one period.  Set `"asOf": "2025-06-30"` on the query to pick that period
(read at the time dimension's grain — a mid-month date means that month).
Without `asOf`, the latest period with data under the query's filters is
used.  `asOf` has no effect when the time dimension is selected.

## fill (every period, empty ones too)

A query returns only the periods that have rows, so a month without orders
is missing from a monthly series.  Set `"fill": true` (OBSL 2.35+) to join
the rows onto a date spine: from the `where` bounds on the time dimension,
or else the first and last period with data, crossed with every member of
the other selected dimensions.  An empty period counts 0, reads a measure's
`defaultValue`, and is NULL otherwise; `orderBy` and `limit` apply to the
filled rows.  It needs exactly one time-grained dimension at day grain or
coarser (`FILL_REQUIRES_ONE_TIME_DIMENSION`, `FILL_GRAIN_NOT_SUPPORTED`), and
is refused beside `grouping`, `having`, totals, window and period-over-period
metrics (`FILL_COMBINATION_NOT_SUPPORTED`).

## Filtering a time-grained dimension

A `where` on a dimension that declares a `timeGrain` compares the **period**
it shows, not the date inside it (OBSL 2.35+): on `Order Month`,
`= 2025-01-01` is January, `= 2025-01-20` matches nothing, and
`>= 2025-01-15` starts in February.  To compare dates, filter the qualified
column (`Orders.Order Date`) or a dimension without a grain.

## orderBy

`orderBy` takes a selected dimension, a numeric position, or (OBSL 2.36+)
**any measure or metric, selected or not**: "the three customers with the
most revenue", showing only their names.  The sort measure is computed over
the same dimensions and `where` filters and joined onto the rows; a row it
has no value for sorts as NULL.  An unselected *dimension* is still refused
(`UNKNOWN_ORDER_BY_FIELD`), and an unselected sort measure beside
`grouping: rollup` / `cube` is `ORDER_BY_COMBINATION_NOT_SUPPORTED`.

## Dimension-only queries

A query with dimensions and no measures returns the combinations held by
**every** data object joining those dimensions (OBSL 2.36+; before, one of
them was picked): with Orders and Returns both joining Customers and
Products, region by category lists the pairs either holds.  A `where` filter
keeps only the data objects reaching it, so a filter on an Orders column
reads Orders alone.

## Selecting a field once

A dimension, measure or metric may be selected only once per query
(`DUPLICATE_DIMENSION` / `DUPLICATE_MEASURE`).  The same dimension at two
grains is two columns (`Order Date:year` beside `Order Date:month`).

## Column order

The result's columns follow the select: the dimensions in their order, then
the measures and metrics **in the order `select.measures` lists them** (OBSL
3.0+; before, measures of independent facts came back grouped by fact,
metrics over them after the rest, and `filterContext` measures last).  A
numeric `orderBy` position (`"field": "3"`) counts columns in that order.

## Supported Dialects

{dialects}

## Metric Types

Metrics are queried by name like any measure.  The types are:

- **Derived** (default): expression-based, e.g. `{[Profit]} / {[Revenue]}`.
- **Cumulative**: running total, rolling window, or grain-to-date over a
  time dimension.  Queried as a regular metric name.  Other selected
  dimensions partition the accumulation, and a rolling `window` counts
  calendar periods, not rows.  A filter on the time dimension picks the
  periods shown; the look-back still reads the history before them.
  `windowSpan: {count: 90, unit: day}` (OBSL 2.36+) instead evaluates the
  measure over its source rows inside the span ending at each shown period,
  so a distinct count or an average over "the last 90 days" is right; it
  needs the time dimension selected and no rollup / cube
  (`WINDOW_SPAN_NOT_SUPPORTED`).
- **Period-over-Period (PoP)**: compares a measure across time periods
  (e.g. YoY growth, MoM difference).  Queried as a regular metric name.
- **Window**: rank, lag/lead and similar functions over a measure.  With
  `windowFunction: sum | avg | min | max | count` and `frame: {preceding: N}`
  (OBSL 2.36+) it is a moving aggregate over **rows**: the current row and
  the N before it, by the time dimension ascending — a three-row moving
  average that skips an empty month.  The time dimension must be selected
  (`WINDOW_TIME_DIMENSION_NOT_IN_SELECT`), and rollup / cube is refused
  (`WINDOW_FRAME_NOT_SUPPORTED`).  A NULL is ordered last, ranked up or
  down, on every engine (OBSL 3.0+; before, its place was the engine's).
- **Reaggregate** (OBSL 2.35+): a measure aggregated at a finer grain, then
  aggregated again — "average revenue per customer, by country".  The model
  names the `measure`, the `per` dimensions added for the first stage, and
  the second-stage `aggregation`; the query just selects the metric by
  name.  `explain_artefact` shows both stages.  Refused under
  `grouping: rollup` / `cube` (`REAGGREGATE_WITH_ROLLUP`); when every `per`
  dimension is already selected it does nothing and says so
  (`REAGGREGATE_NO_OP` warning).

## Measure Filters & Ratios

Measures can have **filters** that restrict which rows contribute to
aggregation (compiled as `CASE WHEN`).  A filtered measure like
"US Revenue" can then be used in a **ratio metric**:
`{[US Revenue]} / {[Revenue]}`  — no query-level WHERE needed.

## Grain Override & Filter Context

Measures can override their aggregation grain and filter context in the
OBML model definition (not at query time):

- **Grain override** (`grain:`) — controls which dimensions a measure
  aggregates over, independently from the query's dimensions.  Enables
  percent-of-total, percent-of-parent, and cross-grain calculations.
  Modes: `FIXED` (start empty) or `RELATIVE` (inherit query dims).
  Operators: `exclude`, `include`, `keepOnly`.
  `total: true` is shorthand for `grain: {mode: FIXED}`.

- **Filter context** (`filterContext:`) — controls which query WHERE
  filters apply to a measure.  Enables unfiltered baselines and
  selective filter exclusion.  Modes: `FIXED` (ignore all query
  filters) or `RELATIVE` (inherit and modify).
  Operators: `exclude`, `include` (static filters), `keepOnly`.

Both are defined in the OBML YAML and passed through to the API.
The full OBML syntax and examples are available in the OBML reference.

## Role-Playing Dimensions (via)

Dimensions can use `via` to force the join path through a specific intermediate
data object.  This enables **role-playing dimensions** — the same target table
accessed through different join paths:

```yaml
dimensions:
  SalesEmployee:
    dataObject: Employees
    column: Name
    via: Sales          # reach Employees through Sales

  ReturnEmployee:
    dataObject: Employees
    column: Name
    via: Returns        # reach Employees through Returns
```

Both dimensions reference `Employees.Name` but produce different results because
they join through different fact tables.  The `via` data object must be reachable
from the query's fact table, and the dimension's `dataObject` must be reachable
from `via` in the directed join graph.

`via` alone reads through the primary join from `via` to the dimension's data
object.  When `via` joins that data object more than once (a primary plus
secondary joins), add `pathName` to pin the dimension to one named join —
several roles of the same data object can then appear in **one** query, each
joined under its own alias:

```yaml
dataObjects:
  Orders:
    joins:
      - joinType: many-to-one
        joinTo: Employees
        pathName: sales
        columnsFrom: [SalesRepId]
        columnsTo: [EmployeeId]
      - joinType: many-to-one
        joinTo: Employees
        secondary: true
        pathName: support
        columnsFrom: [SupportRepId]
        columnsTo: [EmployeeId]

dimensions:
  Sales Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: sales
  Support Employee:
    dataObject: Employees
    column: Name
    resultType: string
    via: Orders
    pathName: support
```

Role dimensions are pinned: a query's `usePathNames` does not change them.
`pathName` requires `via` and must name a join declared directly on `via` that
targets the dimension's data object.  A role reaches only its own data object's
columns (not objects joined beyond it).

When querying, simply use the role-playing dimension name in `query_json`:
```
query_json='{"select": {"dimensions": ["SalesEmployee"], "measures": ["Revenue"]}}'
```

## Coalesce Dimensions

Role-playing dimensions appear as separate columns in CFL output — one row per
role per person.  To collapse them into a single output column, add a coalesce
group to the `select.dimensions` array in ``query_json``::

    {"select": {
        "dimensions": [
            {"coalesce": ["SalesEmployee", "PurchaseEmployee"], "as": "Employee"}
        ],
        "measures": ["Total Sales", "Total Purchases"]
    }}

Each CFL leg projects only its own role-playing dimension (others NULL); the
outer wrapper emits ``COALESCE(d1, d2, ...) AS alias`` and groups by it.

Rules:
- At least 2 members; all must be existing model dimensions
- All members must share the same ``resultType``
- The ``as`` alias must not collide with any model dimension or measure name
- ``orderBy`` may reference the alias directly
- ``where`` filters use the underlying dimension names (per-leg filtering)

## Raw Mode (Physical Column Access)

Raw mode returns un-aggregated rows — no GROUP BY, no measures, no metrics.
Put ``fields`` (instead of ``dimensions``/``measures``) inside ``select``::

    query_json='{
      "select": {"fields": ["Orders.OrderDate", "Customers.Country"],
                  "distinct": true},
      "where": [{"field": "Customers.Country", "op": "equals", "value": "US"}],
      "limit": 100
    }'

Fields use ``DataObject.Column`` syntax referencing physical columns.

Raw mode (``select.fields``) is **mutually exclusive** with
``select.dimensions`` / ``select.measures``, ``having``, and
``dimensionsExclude``.  ``select.distinct`` is only valid in raw mode;
``where``, ``orderBy``, ``limit``, and ``offset`` work in both modes.

## Execute Query — Output Formatting

``execute_query`` supports additional output parameters:

- ``format``: ``"json"`` (default) or ``"tsv"`` (tab-separated text)
- ``format_values``: Format numeric cells as locale-aware display strings
- ``locale``: BCP-47 locale tag (e.g. ``"de"``, ``"en-US"``)
- ``timezone``: IANA timezone (e.g. ``"Europe/Berlin"``) labelling timestamps
  that carry none.  A model's ``settings.queryTimezone`` wins over it (OBSL
  3.0+): its timestamps are wall-clock times there, and "today" — for
  ``current_date()`` and every ``relative`` filter — is counted in that zone,
  as is "now" for ``current_timestamp()`` and ``current_time()`` (OBSL 3.1+).

## Queries across data sources (OBSL 3.0+)

A model's data objects may live in different databases: each names its own
`dataSource`, else the model's `settings.defaultDataSource` applies
(`describe_model` shows both).  A query reading several is written exactly
as any other and answered across them: each source runs its part (a *leg*),
and OBSL combines the legs' results in DuckDB.  The `execute_query` response
then carries `federation` — each leg's `data_source`, `dialect`, `sql` and
`data_objects` — while `sql` is the combining query and `dialect` is
`duckdb`.

Most query shapes work across sources.  What does not is refused at compile
time with a `FEDERATION_*` code and a hint saying what to do instead (often:
query each source's measures separately).  At run time a leg returning too
many rows is `FEDERATION_LEG_TOO_LARGE` (filter it, or select fewer
dimensions), and a server running its limit of federated queries answers
`FEDERATION_BUSY` (HTTP 503: retry later).

## Default Dialect

When ``dialect`` is omitted from ``execute_query``, the API
resolves it via: model ``settings.defaultDialect`` → server ``DB_VENDOR`` env →
``"postgres"``.  Use ``describe_model`` to see the model's default dialect.

## Result Caching & Determinism

The API hashes its result cache on compiled SQL.  Two behaviours keep that
deterministic:

- **Auto-ORDER BY on LIMIT** — when a query sets `limit` without `orderBy`
  the compiler appends `ORDER BY <all dims>` (or `<all raw fields>`) so
  the cache never freezes an arbitrary slice.  Aggregate-only queries
  (no dimensions, no fields) are exempt — they already return one row.
- **Non-deterministic SQL bypass** — compiled SQL containing `RAND()`,
  `NOW()`, `CURRENT_DATE`, `TABLESAMPLE`, etc. is excluded from the cache.
  The `execute_query` JSON response surfaces this via
  `ttl_source = "no_cache:non_deterministic_sql"` so callers can see why
  a fresh round-trip happened.

If you need pagination, always set an explicit `orderBy` and pair `limit`
with `offset`.

## When a query is refused

Since OBSL 3.0 a refused query reports **every** error with what it takes to
repair it: a stable `code`, the `path` in the query, a `hint` naming the fix,
did-you-mean `suggestions` (names matched on names and synonyms), and
structured `context`.  It also says when a name is in the wrong place (a
dimension listed under measures, a measure filtered in `where`).  Compiling
first and correcting on refusal is cheaper than checking every name up front:
read the hint and suggestions, fix the query, and run it again.

## Tips

- Use `describe_model` first to see available dimension/measure names.
- Use `list_dialects` to check dialect capabilities.
- Dimension names with time grain: append `:month`, `:year`, etc.
- Weekday in an expression: `extract('day_of_week', x)` is the ISO day of
  week, Monday 1 to Sunday 7, on every engine (OBSL 2.36+).
"""

_WRITE_BUSINESS_RULE_TEXT = """\
# Writing a business rule (OBML `rules:` block)

A rule is a named condition over the model's dimensions, measures and metrics.
No SQL: the API compiles it to the query that reports its findings.

```yaml
rules:
  High Value Client:            # the rule's name
    type: eligibility           # classification | eligibility | validation | constraint
    description: Clients whose lifetime sales pass the strategic-account threshold
    grain: [Client Name]        # aggregate rules only: evaluated per these dimensions
    condition:
      field: Total Sales        # a dimension, measure or metric name
      op: ">"                   # the query filter operators (no exists/nonexists)
      value: 50000
  Healthy Category:
    type: validation
    severity: warning           # validation/constraint only: error | warning | info
    grain: [Product Category]
    condition:
      all:                      # all | any | not compose; rule inlines another rule
        - {field: Total Sales, op: ">", value: 0}
        - not: {rule: High Return Rate}
```

The four things that go wrong without being told:

1. **Level is derived, not declared.** A condition over dimensions only is
   row-level and compiles to a `WHERE` predicate; one that touches a measure or
   metric is aggregate and compiles to `HAVING` at the declared `grain`. An
   aggregate rule must declare `grain`; a row-level rule must not.
2. **An aggregate rule may compare only dimensions of its grain.** Anything
   else would be neither grouped nor aggregated (`RULE_DIMENSION_OUTSIDE_GRAIN`).
3. **Findings semantics.** `classification` and `eligibility` rules describe
   members: the findings are the rows the condition selects. `validation` and
   `constraint` rules state an invariant: the findings are its violations, so
   the compiled query negates the condition. Only these two take `severity`.
4. **References must match.** `rule: Other` inlines another rule's condition;
   it must have the same level and grain, and references must form a DAG.

Ontology links on a rule (`externalConceptMappings`) work like on any artefact:
`concept` (compact or full IRI) plus `relation`, where `broader` means the
external concept is the broader one (SKOS `broadMatch`).

Then: `load_model` reports every rule problem at once with a source span;
`list_rules` shows what loaded; `explain_rule` shows the compiled SQL;
`get_lineage(name, kind="rule")` shows what it reads down to the tables;
`evaluate_rule` / `evaluate_rules` return the findings.
"""

_DEBUG_VALIDATION_TEXT = """\
# OBML Validation Error Codes

## Parse Errors

- `UNKNOWN_PROPERTY`: An OBML object or QueryObject contains a property name
  that is not part of its schema (e.g. `filtter:` instead of `filter:`).
  Strict parsing is the default — unknown keys are never silently dropped.
  The API response carries one error per offending key with a `path` (e.g.
  `where[0]`) and a "did you mean?" suggestion list derived from the model's
  real fields.
  Fix: Use the suggestion, or check the OBML reference (`get_obml_reference()`)
  for the exact field names. Common culprits: typos, snake_case vs camelCase
  mix-ups, fields that moved between versions.
- `YAML_PARSE_ERROR`: Invalid YAML syntax.
  Fix: Check indentation, quoting, colons.
- `YAML_SAFETY_ERROR`: YAML safety constraint violated (anchors, oversized).
  Fix: OBML does not use anchors/aliases. Reduce document size.
- `DATA_OBJECT_PARSE_ERROR`: Cannot parse a data object.
  Fix: Check required fields (code, database, schema, columns).
- `DIMENSION_PARSE_ERROR`: Cannot parse a dimension definition.
  Fix: Check required fields (dataObject, column, resultType).
- `MEASURE_PARSE_ERROR`: Cannot parse a measure definition.
  Fix: Check required fields (aggregation, resultType) and either columns or expression.
- `METRIC_PARSE_ERROR`: Cannot parse a metric definition.
  Fix: Derived metrics need `expression`.  Cumulative metrics need `measure`
  + `timeDimension` (and `window`/`grainToDate` are mutually exclusive;
  `windowSpan` excludes `window`, `grainToDate`, `cumulativeType` and
  `partitionBy`, and is refused over a reaggregate metric).  A window metric
  with `windowFunction: sum|avg|min|max|count` requires `frame: {preceding: N}`
  and takes no `offset`, `buckets`, `defaultValue` or `orderDirection`.
  Period-over-period metrics need `expression` + `periodOverPeriod` block
  with `timeDimension`, `grain`, and `offsetGrain`.  Reaggregate metrics need
  `measure`, `per` and `aggregation`.
- `MEASURE_FILTER_PARSE_ERROR`: Cannot parse a measure filter.
  Fix: Each filter needs `column` ({dataObject, column}), `operator`, and
  `values`.  Filter groups need `logic` (and/or) and `filters` array.
- `FILTER_PARSE_ERROR`: Cannot parse a static model filter.
  Fix: Each static filter needs `dataObject`, `column`, and `operator`.
  Use `value` for single-value operators, `values` for list operators (inlist, between).
  Dates must be ISO 8601 strings (e.g. '2026-01-01').
- `WRONG_FUNCTION_ARITY`: An expression calls a portable-catalog scalar function
  with the wrong number of arguments (e.g. `substring({Zip}, 1, 5, 9)`). Only
  catalog names are arity-checked — a vendor function's arity is not OBSL's to
  know, so it is passed through unchecked.
  Fix: Match the canonical signature named in the error. See
  `get_function_catalog()` for every entry's arity and semantics.
- `INVALID_COLUMN_EXPRESSION`: A computed column's `expression` could not be
  parsed. Reported at model load, and again at query time if the column is
  reached — the same answer `INVALID_METRIC_EXPRESSION` has always given for a
  metric formula. The expression parser reads a *subset* of SQL: `||`,
  `INTERVAL` and `EXTRACT(... FROM ...)` are outside it, and a call missing its
  closing `)` is refused rather than closed for you (an invented parenthesis
  moves what the call wraps, so `ROUND({Amount}, 2 * 100` and
  `ROUND({Amount}, 2) * 100` are different numbers).
  Fix: Rewrite it with portable catalog functions (`get_function_catalog()`),
  or move the construct into the source view.
- `INVALID_MEASURE_EXPRESSION`: A measure's `expression` could not be parsed —
  the same check `INVALID_COLUMN_EXPRESSION` makes on a computed column, applied
  to a measure formula (added in OBSL 2.27; before that a measure expression was
  only checked for the references inside it, so a body that could not parse was
  carried to codegen and failed at the engine).  A malformed `{[...]}` reference
  is reported separately by `MALFORMED_EXPRESSION_REF` and does not count against
  the surrounding syntax.
  Fix: Rewrite it with portable catalog functions (`get_function_catalog()`),
  or move the construct into the source view.

## Time Grain Errors

- `TIME_GRAIN_ON_NON_TEMPORAL`: A dimension sets `timeGrain` over a column whose
  `abstractType` is not `date`, `timestamp` or `timestamp_tz`. There is nothing
  to truncate.
  Fix: Point the dimension at a temporal column, or — for text encoding a date,
  e.g. '2024-03' — define a computed column with `to_date()` and use that.
- `RESULT_TYPE_LOSES_GRAIN`: A dimension declares a `resultType` too narrow to
  hold the bucket its `timeGrain` makes (OBSL 2.27+). A temporal `resultType` is
  emitted as a CAST that sits in the GROUP BY as well as the projection, so a
  narrow one does not merely relabel the column — it *merges buckets and changes
  the measures*, silently, because nothing about it is a SQL error. `hour` /
  `minute` / `second` need `timestamp` (a `date` drops the time of day), and
  neither `time` nor `time_tz` ever holds a grain, since a grain carries a date.
  Raised at load, and again at query time for a `dimension:grain` override the
  model never saw.  Declaring *wider* than the grain is allowed and harmless — a
  month grain as `timestamp` is a date at midnight. A non-temporal `resultType`
  is not cast at all and holds anything.
  Fix: Widen `resultType` to `timestamp` (or drop it), or coarsen `timeGrain`.
- `GRAIN_FINER_THAN_DECLARED`: A grain is asked of a column that holds a
  coarser one (OBSL 2.35+) — a day or week of a column of month starts. A
  column's grain is read from a computed column that is a whole
  `date_trunc('<grain>', ...)`, or declared with the column field `grain`
  (`describe_model` shows it beside the column). Checked for a query's
  `dimension:grain` and, at load, for `timeGrain`, a reaggregate `per` grain, a
  PoP `grain` and a cumulative `grainToDate`. Coarser grains (quarter, year of
  months) are answered.
  Fix: Ask for the column's grain or a coarser one, or read a finer column.
- `INVALID_TIME_GRAIN`: A query names a grain OBML does not have (e.g.
  `Order Date:day_of_week`); the message lists the valid ones (OBSL 2.35+,
  previously an untyped 500).
- `AGGREGATE_IN_COLUMN_EXPRESSION`: A computed column's expression contains an
  aggregate or window function (`SUM(...)`, `BOOL_OR`, `ARG_MAX`, ...), which
  `GROUP BY ALL` would take for a measure (OBSL 2.35+; such a model no longer
  loads).
  Fix: Move the aggregate into a measure. For ClickHouse `ANY(...)` the hint
  suggests `SOME(...)`.
- `INEXACT_DECIMAL_VALUE`: A non-integer value for a `decimal` field arrived
  without its written digits (a JSON body, a merged fragment), so it cannot be
  compared exactly.
  Fix: Give the digits as text: `valueString: "1.0000000000000001"`.

## Reaggregate Metric Errors (OBSL 2.35+)

A `type: reaggregate` metric computes `measure` at the query's dimensions plus
`per` (stage 1), then aggregates those values with `aggregation` (stage 2).

- `REAGGREGATE_MEASURE_ONLY`: `measure` names something other than a measure,
  a reaggregate metric or a cumulative metric.
- `REAGGREGATE_CYCLE`: A chain of reaggregate metrics refers back to itself.
- `REAGGREGATE_AVG_OF_AVG`: `aggregation: avg` over something that already
  averages (directly, or through a min/max/median/percentile/first/last
  reaggregate of an average) weighs every group alike; the message shows the
  chain. `avg` over a `sum` or `count` stays allowed.
- `REAGGREGATE_CUMULATIVE_TIME_DIMENSION`: Over a cumulative metric, `per` has
  to name its `timeDimension`.
- `REAGGREGATE_ORDER_KEY`: `first` / `last` need exactly one time entry in
  `per` (a grained entry, a dimension with `timeGrain`, or a date dimension).
- `REAGGREGATE_HAVING_MEASURE_ONLY`: A reaggregate `having` condition names a
  metric; it filters stage-1 groups and takes measures only (no
  `exists`/`nonexists`).
- `REAGGREGATE_WITH_ROLLUP`: Selected under `grouping: rollup` / `cube`.
- `REAGGREGATE_COMBINATION_NOT_SUPPORTED`: A derived metric combines a
  reaggregate metric with a total, grain, `filterContext`, cumulative or PoP
  component in one formula.
  Fix: Select them side by side instead.
- `REAGGREGATE_POPULATION_PER` / `REAGGREGATE_POPULATION_GROUPING` /
  `REAGGREGATE_POPULATION_HAVING`: `population: dimension` with a grained or
  `via` `per` entry, a query dimension reading both members and facts, or a
  `having`.
- `REAGGREGATE_CARRY_FORWARD_POPULATION` / `REAGGREGATE_CARRY_FORWARD_AXIS`:
  `carryForward: true` without `population: dimension`, or over two dates.
- `REAGGREGATE_NO_OP` (warning): Every `per` dimension is already in the
  query, so each group has one value and the metric changes nothing.

## Datasource Errors (online validation only)

Raised only by `validate_model(online=true)`, which reads each data object
from the configured warehouse as a compiled query would: its table, its
columns, each computed column's expression and a nested data object's fields
(OBSL 3.1+; before, plain columns only). Offline validation resolves a model
against *itself* and never sees these — a model can be structurally perfect
and still name a table that was dropped.

- `DATASOURCE_TABLE_MISSING`: A data object's table is not there under the
  `database` / `schema` / `code` the model declares.
  Fix: Correct the physical binding, or restore/rename the table.
- `DATASOURCE_COLUMN_MISSING`: The table exists but a declared column `code` is
  not addressable on it, or a nested data object's field is not in the array's
  elements (on Snowflake and MySQL a missing field reads as NULL instead).
  Fix: Correct the column's `code`, or restore the column.
- `DATASOURCE_COLUMN_CASE`: The column exists under a different identifier case
  than the model spells it. The probe quotes identifiers exactly as codegen
  does, so the engine — not a table of folding rules — settles this.
  Fix: Spell the column's `code` as the engine stores it.
- `DATASOURCE_TYPE_MISMATCH`: A column's physical type is not in the family its
  `abstractType` declares. The comparison is deliberately coarse (number /
  datetime / string / boolean / binary): `int32` widening to `int64`, or a
  `timestamp` stored as `TIMESTAMP_NTZ`, is not drift. A computed column is
  checked against the type it declares too.
  Fix: Correct `abstractType`, or the column.
- `DATASOURCE_EXPRESSION_FAILED`: The database could not evaluate a computed
  column's `expression` (OBSL 3.1+). Carries the driver's error.
  Fix: Correct the expression for this database: the columns it reads, and
  that the database has every function it calls.
- `DATASOURCE_UNNEST_FAILED`: The parent's column a `nestedIn` data object
  names could not be read as an array (OBSL 3.1+).
  Fix: Correct `nestedIn.column`, or check it is an array column of the parent.
- `DATASOURCE_UNAVAILABLE`: No datasource is configured or reachable, so nothing
  could be probed.
  Fix: Configure the connection, or validate offline (`online=false`).
- `DATASOURCE_UNSUPPORTED_DIALECT`: The `dialect` named for the probe has no
  driver. Note this names a *connection to open*, defaulting to the dialect of
  the data source the model names (`settings.defaultDataSource`), then the
  server's `DB_VENDOR` — not the model's `settings.defaultDialect`.
  Fix: Pass a dialect the deployment is configured for; see `list_dialects()`.
- `DATASOURCE_PROBE_FAILED`: The probe query itself errored for a reason that is
  neither a missing table nor a missing column (permissions, a timeout).
  Fix: Read the message — it carries the engine's own error.

With `validate_model(sample=...)` (OBSL 3.1+) each data object's rows are
read too, for what only a value shows. A count (`1000`) reads the first that
many rows, with no upper bound; a percent (`"1%"`) returns at most 1000 rows
but may scan many more to find them, up to the whole table. These are
warnings: the model stays valid.

- `DATASOURCE_SAMPLE_FAILED` (warning): The SQL plans, but a value in the
  sample does not fit it — e.g. `{Name} * 2` over text, which Snowflake,
  Dremio and Databricks refuse only on a row. Points at the computed column's
  `expression` or the nested field's `code` that fails on its own.
  Fix: Correct the expression, or the declared `abstractType`.
- `DATASOURCE_SAMPLE_ALL_NULL` (warning): A nested field is NULL on every
  sampled element — on Snowflake and MySQL, what a field the elements lack
  reads as.
  Fix: Check the field's `code`.

## Data Source Errors (load and execution, OBSL 3.0+)

Note the underscore: `DATA_SOURCE_UNAVAILABLE` is not the online probe's
`DATASOURCE_UNAVAILABLE` above.

- `DATA_SOURCE_UNAVAILABLE`: The data source a model names
  (`settings.defaultDataSource`, a data object's `dataSource`, or
  `load_model(datasource=...)`) cannot run it. Where the API executes queries
  the load itself fails (at `settings.defaultDataSource`); otherwise the query
  does. `context.reason` says which case: `unknownSource` (with `suggestions`
  for a misspelt name and the configured names in `context.dataSources`),
  `dialectMismatch` (`context.sourceDialect` vs `context.dialect`),
  `ambiguousSource` (several sources of the dialect and the model names none),
  `invalidConfiguration` or `extensionOutdated` (server side).
  Fix: Name a configured source of the model's dialect — see
  `list_datasources()`.

## Reference Errors

- `UNKNOWN_DATA_OBJECT`: References non-existent data object.
  Fix: Check spelling; suggestions are included.
- `UNKNOWN_COLUMN`: Column name not found in data object.
  Fix: Check column name spelling within the referenced data object.
- `UNKNOWN_DATA_OBJECT_IN_EXPRESSION`: Measure expression `{[DataObject].[Column]}` \
references unknown data object.
  Fix: Check data object name in the expression.
- `UNKNOWN_COLUMN_IN_EXPRESSION`: Measure expression `{[DataObject].[Column]}` \
references unknown column.
  Fix: Check column name in the expression.
- `UNKNOWN_MEASURE_REF`: Metric expression `{[Measure Name]}` references unknown measure.
  Fix: Check measure name in the expression.
- `UNKNOWN_MEASURE`: Query references missing measure.
  Fix: Check measure name in query select.
- `UNKNOWN_DIMENSION`: Query references missing dimension.
  Fix: Check dimension name in query select.
- `UNKNOWN_JOIN_TARGET`: `joinTo` references unknown data object.
  Fix: Check `joinTo` value matches a data object name.
- `UNKNOWN_JOIN_COLUMN`: Join column not found in data object.
  Fix: Check `columnsFrom`/`columnsTo` column names exist.
- `UNKNOWN_PATH_NAME`: `usePathNames` references non-existent path.
  Fix: Check source, target, and pathName match a secondary join.

## Semantic Errors

- `DUPLICATE_IDENTIFIER`: Duplicate name across data objects, dimensions, measures, or metrics.
  Fix: All names must be unique across the model.
- `CYCLIC_JOIN`: Join graph contains a cycle.
  Fix: Remove circular join references.
- `MULTIPATH_JOIN`: Multiple join paths between two data objects.
  Fix: Make join graph unambiguous, or use secondary joins with pathName.
- `JOIN_COLUMN_COUNT_MISMATCH`: `columnsFrom` and `columnsTo` have different lengths.
  Fix: Ensure both lists have the same number of entries.

## Secondary Join Errors

- `SECONDARY_JOIN_MISSING_PATH_NAME`: Secondary join has no `pathName`.
  Fix: Add a `pathName` to the secondary join.
- `DUPLICATE_JOIN_PATH_NAME`: Duplicate `pathName` for the same (source, target) pair.
  Fix: Use a unique `pathName` per (source, target) pair.

## Via (Role-Playing Dimension) Errors

- `INVALID_VIA_DATA_OBJECT`: Dimension `via` references an unknown data object, or the
  dimension's target data object is not reachable from `via` in the directed join graph.
  Fix: Check that `via` names an existing data object and that the dimension's `dataObject`
  is reachable from it through primary joins.
- `INVALID_DIMENSION_PATH`: A dimension sets `pathName` without `via`, or `via` declares
  no join to the dimension's data object with that `pathName`.
  Fix: Add `via`, or use one of the declared pathNames listed in the error.
- `AMBIGUOUS_VIA`: Warning — `via` joins the dimension's data object more than once and
  the dimension has no `pathName`, so it silently reads through the primary join.
  Fix: Set `pathName` to pick the role the dimension stands for.
- `MISSING_VIA`: Warning — a dimension's target data object has direct joins from multiple
  fact tables, which may cause ambiguous join paths.
  Fix: Add role-playing dimensions with `via` to disambiguate, or ignore if ambiguity is
  intentional.

## Coalesce Dimension Errors

- `COALESCE_MISSING_ALIAS`: Coalesce dimension requires a non-empty `as` alias.
  Fix: Add `"as": "AliasName"` to the coalesce group.
- `DUPLICATE_COALESCE_ALIAS`: Duplicate coalesce alias in the same query.
  Fix: Use a unique `as` alias for each coalesce group.
- `COALESCE_ALIAS_COLLISION`: Coalesce alias collides with an existing model dimension
  or measure name.
  Fix: Choose an alias that doesn't match any dimension or measure name.
- `COALESCE_TOO_FEW_MEMBERS`: Coalesce requires at least 2 dimensions.
  Fix: Add at least 2 dimension names to the `coalesce` array.
- `COALESCE_TYPE_MISMATCH`: Coalesce members have incompatible result types.
  Fix: All members must share the same `resultType`.

## Grain & Filter Context Errors

- `UNKNOWN_GRAIN_DIMENSION`: Grain override references a non-existent dimension.
  Fix: Check dimension name in grain.include, grain.exclude, or grain.keepOnly.
- `UNKNOWN_FILTER_CONTEXT_FIELD`: Filter context references a non-existent field.
  Fix: Check field names in filterContext.exclude, filterContext.keepOnly, or
  filterContext.include[].field — must be a dimension name or DataObject.Column.
- `GRAIN_NOT_SUBSET`: Effective grain dimensions are not a subset of query dimensions.
  Fix: Ensure all grain dimensions are included in the query's dimension list.
- `ANCHOR_REQUIRED_AMBIGUOUS_KEY`: A measure expression reads two independent facts
  that share more than one dimension, so there is no single grain to conform them
  at — each shared key gives a different answer.
  Fix: Set `anchor:` on the measure, naming the data object whose grain the
  expression is evaluated at (one of the facts it reads, or an object they all
  join to).
- `CONFORMED_GRAIN_ASSUMED` (warning, not an error): A cross-fact measure expression
  had no declared join path and no `anchor:`, so the facts were conformed to the one
  data object they all join to. The warning names the object chosen. Only a
  `SUM` of a product reads the same under every anchor; a sum of terms that do
  not all carry an anchor column, and `AVG` / `MIN` / `MAX`, do not (see the
  `FAN_TRAP_RISK` warning).
  Fix: If the chosen grain is not what you meant, set `anchor:` explicitly.

## Resolution Errors (at query time)

- `AMBIGUOUS_JOIN`: Multiple join paths found during query resolution.
  Fix: Make join graph unambiguous or use `usePathNames`.
- `UNREACHABLE_REQUIRED_OBJECT`: A data object required by the query cannot be reached
  from the base object via directed joins. Many-to-one joins are forward-only; reverse
  traversal would inflate row counts.
  Fix: Add an explicit join from the base object (or an intermediate object) to the
  unreachable object, or split the query.
- `MALFORMED_EXPRESSION_REF`: Expression contains a malformed `{[...]}` reference
  (missing brackets, separators, or braces).
  Fix: Measure expressions use `{[DataObject].[Column]}` syntax;
  metric expressions use `{[Measure Name]}` syntax.  Check for missing
  `[`, `]`, `{`, `}`, or `.` separators.
- `INVALID_METRIC`: A metric cannot be computed in the shape this query asks
  for. Most often a period-over-period metric combined with measures from
  independent facts: the comparison is built around a date spine over one join
  tree, and those measures are stacked into a UNION ALL that has no such tree.
  Also raised for two PoP metrics at different grains in one query.
  Fix: Split the query — the PoP metric against its own fact, the other
  measures separately — or drop the cross-fact measures.
- `INVALID_METRIC_EXPRESSION`: Metric expression could not be parsed.
  Fix: Use `{[Measure Name]}` syntax in metric expressions.
- `INVALID_FILTER_OPERATOR`: Unrecognised filter operator in query.
  Fix: Use a supported operator (equals, gt, gte, lt, lte, inlist, regex, blank, length_eq, etc.).
- `INVALID_FILTER_VALUE`: Filter value has the wrong type for the operator.
  Fix: `regex`/`notregex` require a string pattern; `length_eq`/`length_gt`/`length_lt`
  require an integer value.  A period literal (`'2025-Q1'`) is refused with an
  operator that has no period reading (`contains`) or a period a date cannot
  bound (`'0000'`, `'9999-12'`).
- `INVALID_RELATIVE_FILTER`: Malformed relative time filter.
  Fix: Check unit (day/week/month/quarter/year), count, direction,
  include_current, calendar, toDate.
- `UNKNOWN_FILTER_FIELD`: Filter field is not a dimension (WHERE) or measure (HAVING).
  Fix: Check field name matches a dimension or measure in the model.
- `UNKNOWN_ORDER_BY_FIELD`: ORDER BY field is neither a selected dimension nor a
  measure or metric of the model (since OBSL 2.36 a measure or metric may be
  sorted by without being selected).
  Fix: Select the dimension, sort by a model measure/metric, or use a numeric position.
- `ORDER_BY_COMBINATION_NOT_SUPPORTED`: An unselected sort measure beside
  `grouping: rollup` / `cube` (OBSL 2.36+).
  Fix: Select the measure, or drop the rollup/cube.
- `INVALID_ORDER_BY_POSITION`: Numeric ORDER BY position is out of range.
  Fix: Use a position between 1 and the number of SELECT columns.
- `CUMULATIVE_TIME_DIMENSION_NOT_IN_SELECT`: Since OBSL 2.34 a cumulative
  metric no longer needs its `timeDimension` selected (see `asOf`); the error
  remains only for `grouping: rollup` / `cube`, or when another dimension over
  the same date column is selected.
  Fix: Select the metric's `timeDimension`, or drop the rollup/cube or the
  other dimension on that date column.
- `CUMULATIVE_MEASURE_ONLY` / `WINDOW_MEASURE_ONLY`: A cumulative or window
  metric's `measure` names a derived, cumulative, PoP or window metric. It
  takes a measure or a reaggregate metric (OBSL 2.35+; previously reported as
  `UNKNOWN_MEASURE`).
- `DUPLICATE_DIMENSION` / `DUPLICATE_MEASURE`: The same dimension (at the same
  grain), measure or metric is selected twice (OBSL 2.35+).
  Fix: Select it once. Two grains of one dimension are two columns.
- `UNREACHABLE_FILTER_OBJECT`: No selected fact can reach the data object a
  `where` filter is on (OBSL 2.35+; before, such a filter was silently
  skipped). A filter only *some* facts reach is applied where it can be and
  reported as the `FILTER_NOT_APPLIED` warning instead.
  Fix: Filter on an object the query's facts join to, or add a measure from a
  fact that reaches it.
- `STATIC_FILTER_UNREACHABLE`: A static model filter declared
  `enforce: always` cannot be applied to a data object the query reads, so the
  query is refused rather than run unfiltered (a tenant filter fails closed).
  `describe_model` marks such filters `[enforce: always]`.
- `FILTER_ADMITS_ROWS_WITHOUT`: A filter on the many side of a join can be
  satisfied by parents without children (e.g. `Order ID notset`), in a shape
  that cannot add those parents (ROLLUP / CUBE with several parents).
  Fix: Express it with `exists` / `nonexists`.
- `SUBQUERY_FILTER_OBJECT_NOT_JOINABLE`: A nested `exists` / `nonexists`
  inside a subquery filters a field on the outer subject.
  Fix: Put that condition in the outer `where`.
- `FILL_REQUIRES_ONE_TIME_DIMENSION` / `FILL_GRAIN_NOT_SUPPORTED` /
  `FILL_COMBINATION_NOT_SUPPORTED`: `fill: true` needs exactly one
  time-grained dimension (outside a coalesce group) at day grain or coarser,
  and is refused beside `grouping`, `having`, totals, window and PoP metrics.
- `WINDOW_TIME_DIMENSION_NOT_IN_SELECT`: a window metric's `timeDimension`
  (required for a `frame`, LAG / LEAD) is not among the selected dimensions.
  Fix: Select the time dimension.
- `WINDOW_FRAME_NOT_SUPPORTED` (OBSL 2.36+): a window metric with `frame`
  under `grouping: rollup` / `cube`, whose subtotal rows would be read as
  periods.  Fix: Drop the rollup/cube.
- `WINDOW_SPAN_NOT_SUPPORTED` (OBSL 2.36+): a `windowSpan` cumulative metric
  queried without its time dimension or under rollup / cube, or over a
  measure deduplicated per join row or carrying a `filterContext`.
- `VALUE_SEARCH_DISABLED` (403) / `VALUE_SEARCH_NOT_SUPPORTED`
  (`search_values`, OBSL 2.36+): the dimension declares `valueSearch: false`;
  or a `search` term was given for a date/number dimension (omit it to get
  the range) or for a non-string dimension.

Since OBSL 3.0 each of these carries a `hint`, did-you-mean `suggestions` and
structured `context` where they apply; an unknown name also says when it is
a measure listed as a dimension, the reverse, or a measure filtered in `where`.

### Fanout refusals ("Query fanout detected")

A join that repeats a measure's rows would inflate it, so the query is refused
rather than answered wrong. Stable codes since OBSL 3.0 (`query/plan` reported
`FANOUT_ERROR` before):

- `FANOUT`: The selected dimensions force a join that repeats the measure's
  rows. The context names the measure, the join and the dimensions forcing it.
  Fix: Drop or change those dimensions, or set `"allowFanOut": true` if the
  multiplication is intended.
- `NESTED_FANOUT`: Two nested data objects (`nestedIn`) are unnested together,
  repeating each one's elements per element of the other.
  Fix: Query one nested object at a time, or set `allowFanOut: true`.
- `GRAIN_DEDUP_UNSUPPORTED`: A measure needs deduplication per grain, but the
  query combines it with something the rewrite cannot express (e.g. an
  averaged total beside it).
- `ANCHOR_NOT_CONFORMABLE`: An anchored measure reads a fact that shares no
  join key with its anchor.
  Fix: Join both to a common data object on the same columns, or anchor the
  measure on an object that reaches the other fact.
- `ANCHORED_FILTER_NOT_SUPPORTED`: An anchored measure's filter reads both the
  conformed fact and the anchor's side.
  Fix: Split it into a condition on each side.

### Federation refusals (queries across data sources, OBSL 3.0+)

Raised when a query reads data objects of several data sources in a shape the
combine step cannot answer as one database would. Each has a hint.

- `FEDERATION_UNSUPPORTED_QUERY` / `FEDERATION_UNSUPPORTED_FILTER` /
  `FEDERATION_UNSUPPORTED_JOIN` / `FEDERATION_UNSUPPORTED_AGGREGATION` /
  `FEDERATION_UNSUPPORTED_METRIC`: That query shape, filter, join (e.g.
  many-to-many between sources), aggregation or metric is not supported
  across sources.
- `FEDERATION_MULTI_FACT`: The measures' facts cannot be combined across
  sources in this shape.
  Fix: Query each data source's measures separately.
- `FEDERATION_MEASURE_ACROSS_SOURCES`: A measure reads a column computed from
  another database.
  Fix: Compute the column from the measure's own database.
- `FEDERATION_LEG_TOO_LARGE` (at run time): A source's part of the query
  returned more rows or memory than the server allows. The message names the
  joins between sources that make it large.
  Fix: Filter, join the sources on a column with fewer values, or select
  fewer dimensions.
- `FEDERATION_BUSY` (HTTP 503): The server is already running its limit of
  federated queries. Fix: Retry later.

## Dialect Capability Errors (at query time)

- `UNSUPPORTED_AGGREGATION`: The selected dialect does not implement the
  measure's aggregation function (HTTP 422 from the API, surfaced as a
  ToolError with `aggregation=…, dialect=…` context).
  Fix: Change `dialect`, or rewrite the measure to use a supported
  aggregation. See `list_dialects()` for each dialect's
  `supported_aggregations`.
- `UNSUPPORTED_GROUPING`: The selected dialect does not support the
  requested `WITH ROLLUP` / `WITH CUBE` (e.g. MySQL has no CUBE). The
  execute path returns HTTP 422.
  Fix: Change `dialect`, drop the modifier, or rewrite the query
  (e.g. UNION of explicit grain combinations).
- `UNSUPPORTED_FUNCTION`: The selected dialect cannot render a portable-catalog
  scalar function an expression calls (HTTP 422, with `function=…, dialect=…`).
  The catalog's counterpart to `UNSUPPORTED_AGGREGATION`.
  Fix: Change `dialect`, or rewrite the expression. See `list_dialects()` for
  each dialect's `supported_functions`.
- `AMBIGUOUS_TABLE_REFERENCE`: The selected dialect cannot disambiguate a table
  reference in the compiled SQL (HTTP 422, with `database=…, dialect=…`).
  Fix: Qualify the data object's `database`/`schema` in the model, or compile
  against a dialect that resolves the reference.
- `UNSUPPORTED_NESTED_ACCESS`: The query reads a nested data object (one
  declared with `nestedIn`, whose rows are a parent's array column) and the
  selected dialect has no FROM-clause unnest to expand it (HTTP 422, with
  `dataObject=…, dialect=…`).
  Fix: Change `dialect`, or give the nested object a `code` table to be read
  from instead — note that the fallback emits a `NESTED_SOURCE_FALLBACK`
  warning, because the table and the array are not guaranteed to agree.

## Warnings (non-blocking)

A warning never fails the load or the query — it is returned alongside the
result, and `load_model` reports the count.

- `NON_PORTABLE_FUNCTION`: An expression calls a function the portable catalog
  does not carry. The call is emitted verbatim, which pins the model to the
  engines that spell it that way. Becomes a hard error when the model sets
  `settings.expressionMode: portable`.
  Fix: Use a catalog entry when one covers the need (`get_function_catalog()`).
- `NARROWING_DATA_TYPE`: A measure declares a `dataType` too narrow for the
  values its own source column is allowed to hold (e.g. `int` declared over a
  `bigint` column). A value that outgrows the declared type raises on most
  engines, saturates on MySQL and wraps on ClickHouse.
  Fix: Widen `dataType` (`bigint`, or a decimal with more integer digits) if
  the column can really reach those values.
- `OUT_OF_SCOPE_TABLE`: The compiled SQL's outermost query names a table its
  own `FROM` does not provide, so the statement parses but the database will
  reject it. This is a compiler defect rather than a model one.
  Fix: Nothing to change in the model — report the query upstream.
- `NON_ADDITIVE_CUMULATIVE_SUM`: A cumulative `sum` metric adds up per-period
  values of a measure that does not add up across periods — a distinct
  count, average, min/max (OBSL 2.34+). Summed monthly distinct customers
  count a customer once per month.
  Fix: Base the cumulative sum on a `sum`/`count` measure, or use
  `cumulativeType: avg`/`min`/`max` if per-period values are what you mean.
- `DECLARED_TYPE_NOT_APPLIED`: A result column could not be reconciled to the
  type the model declares for it, so it is returned as whatever the engine sent
  (OBSL 2.27+). Reconciliation is what makes a `boolean` measure over an
  unsafe source report `type=boolean` instead of rows of 0/1/7 under a
  numeric label; where it cannot be applied, the warning says so rather than
  letting the label and the values disagree in silence.
  Fix: Usually nothing — read the values as the engine's own type. If the
  declared type matters downstream, cast in the measure expression.
- `FILTER_NOT_APPLIED`: A `where` filter could not be applied to some of the
  query's measures — their fact does not reach it — so they are computed
  without it (OBSL 2.35+; previously the whole query was refused). The warning
  names them.
  Fix: Usually nothing: they are what that fact holds. Drop those measures if
  every number must honour the filter.
- `DIMENSION_NOT_APPLIED`: In a query over several facts, a fact cannot
  reach a query dimension's data object, so its measures are not split by
  that dimension: all their values sit in the row where it is NULL, and the
  other rows show 0 or NULL (OBSL 3.1+; silent before). The warning names the
  measures and the dimensions. The rows still add up to the total.
  Fix: Join the fact to the dimension's data object, or query those measures
  without the dimension.
- `STATIC_FILTER_NOT_APPLIED`: A static model filter (default
  `enforce: relevant`) was skipped because the query cannot reach its data
  object. Silent before OBSL 2.35.
  Fix: If it is a tenant or security filter, declare `enforce: always` so such
  a query is refused (`STATIC_FILTER_UNREACHABLE`).
- `FAN_TRAP_RISK`: An anchored measure sums a term with no anchor column, which
  is added once per anchor row (e.g. `SUM({[Returns].[Qty]} - {[Sales].[Qty]})`
  anchored on `Returns` subtracts a date's sales once per return).
  Fix: Split the measure into one per fact and combine them in a derived
  metric.

## Business Rule Errors (`rules:` block, OBSL 2.30+)

- `RULE_PARSE_ERROR`: `rules` is not a mapping, a rule is not a mapping, or a
  property has the wrong type or an unknown enum value.
  Fix: `rules` maps rule name → {type?, condition, grain?, severity?, ...};
  `type` is classification | eligibility | validation | constraint.
- `INVALID_RULE_CONDITION`: A condition node is not exactly one form
  (comparison `field`/`op`/`value`, `all`, `any`, `not`, or `rule`), lacks
  `op`, uses an unknown or disallowed operator, or has an empty `all`/`any`.
  Fix: One form per node; the operators are the query filter operators
  (`exists`/`nonexists` excluded).
- `UNKNOWN_RULE_FIELD`: A comparison names something that is not a dimension,
  measure or metric.
  Fix: Use `find_artefacts` to get the exact name.
- `UNKNOWN_RULE`: A `rule:` reference names a rule that does not exist, or the
  rule itself.
- `UNKNOWN_RULE_GRAIN`: `grain` names an unknown dimension.
- `RULE_GRAIN_REQUIRED`: An aggregate rule (one over a measure or metric) has
  no `grain`.
  Fix: Declare the dimensions the aggregate is evaluated at.
- `RULE_GRAIN_NOT_ALLOWED`: A row-level rule (dimensions only) has a `grain`.
  Fix: Remove it — row-level rules are a WHERE predicate, not a grouping.
- `RULE_DIMENSION_OUTSIDE_GRAIN`: An aggregate rule compares a dimension that
  is not in its grain (it would be neither grouped nor aggregated).
  Fix: Add the dimension to `grain`, or compare a measure instead.
- `RULE_REFERENCE_MISMATCH`: A referenced rule has a different level or grain.
  Fix: Only rules of the same level and grain can be inlined.
- `CYCLIC_RULE_REFERENCE`: Rules reference each other in a cycle.
- `INVALID_RULE_SEVERITY`: `severity` on a classification or eligibility rule.
  Fix: Severity belongs to validation and constraint rules only.

A rule with a problem is reported and dropped; the others still load, so
every problem surfaces at once.

## Ontology Link Errors (`ontology.prefixes` / `externalConceptMappings`)

- `ONTOLOGY_PARSE_ERROR`: `ontology` or `ontology.prefixes` is not a mapping.
- `INVALID_ONTOLOGY_PREFIX`: Prefix name is not an identifier, the namespace is
  not an absolute IRI, or a built-in prefix (rdf, rdfs, owl, skos, xsd) is
  rebound.
- `UNKNOWN_ONTOLOGY_PREFIX`: A compact IRI uses a prefix that is neither
  declared nor built in (the message lists the known ones).
  Fix: Declare it under `ontology.prefixes`, or write the full IRI.
- `INVALID_CONCEPT_IRI`: Blank node, relative reference, angle brackets,
  forbidden characters, or an empty local name.
  Fix: `prefix:LocalName` or an absolute IRI, no `<...>`, no whitespace.
- `INVALID_CONCEPT_MAPPING`: An entry is not a mapping, `relation` is missing or
  unknown, `justification` unknown, `confidence` outside 0..1, or the list is
  not a list.
  Fix: `relation` is exact | close | broader | narrower | related (`broader`
  means the external concept is the broader one).
- `DUPLICATE_CONCEPT_MAPPING`: Same expanded IRI and relation twice on one
  artefact.
- `CONFLICTING_CONCEPT_MAPPING`: Same expanded IRI with two different relations
  on one artefact.
- `ONTOLOGY_PREFIX_CONFLICT`: An `extends` fragment or an inheriting child binds
  an already-bound prefix to a different namespace.
  Fix: One namespace per prefix across the whole composition.

## Debugging Steps

1. Run `load_model(model_yaml)` — it validates and returns any errors.
2. Read the error code and message carefully.
3. Fix the YAML and re-load.
"""


@mcp.prompt
def write_obml_model() -> str:
    """OBML syntax reference — how to write a semantic model in YAML."""
    return _fetch_obml_reference()


@mcp.prompt
def write_obsql_query() -> str:
    """OBSQL grammar reference — natural SQL surface against the model."""
    return _fetch_obsql_reference()


@mcp.prompt
def write_query() -> str:
    """How to build the QueryObject (query_json) for the execute_query tool."""
    dialect_list = ", ".join(f"`{d}`" for d in _fetch_dialect_names())
    return _WRITE_QUERY_TEXT.replace("{dialects}", dialect_list)


mcp.add_prompt(
    StaticPrompt(
        name="debug_validation",
        description="All OBML validation error codes with causes and fixes.",
        text=_DEBUG_VALIDATION_TEXT,
        meta={"text": _DEBUG_VALIDATION_TEXT},
    )
)

mcp.add_prompt(
    StaticPrompt(
        name="write_business_rule",
        description=(
            "How to write an OBML business rule: level, grain, findings semantics, references."
        ),
        text=_WRITE_BUSINESS_RULE_TEXT,
        meta={"text": _WRITE_BUSINESS_RULE_TEXT},
    )
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _ApiUnusableError(Exception):
    """The configured API cannot be served: unreachable, or another ``major.minor``."""


def _verify_api() -> None:
    """Check the API answers ``/health`` with this server's ``major.minor``.

    Each MCP minor tracks the API minor of the same number (tools, parameters
    and response fields follow that API), so MCP 3.1 serves API 3.1.x only.
    Patch releases may differ. Raises :class:`_ApiUnusableError` with the reason;
    an API or a build without a version is let through. A pass is timed in
    ``_api_verified_at``, a failure clears it.
    """
    global _api_verified_at
    _api_verified_at = None
    client = _get_client()
    try:
        resp = _do_request(client, "GET", "/health", None, public=True)
        resp.raise_for_status()
    except ToolError as exc:
        # The message says why — a stopped service, an untrusted certificate
        # and a refused client certificate need different fixes.
        raise _ApiUnusableError(f"API health check failed: {exc}") from None
    except httpx.HTTPStatusError as exc:
        raise _ApiUnusableError(
            f"API health check failed: {exc.response.status_code} {exc.response.text}"
        ) from None

    mcp_version = _package_version()
    api_version = None
    with contextlib.suppress(ValueError, AttributeError):
        api_version = resp.json().get("version")
    if mcp_version and api_version:
        mcp_parts = mcp_version.split(".")
        if mcp_parts[:2] != str(api_version).split(".")[:2]:
            raise _ApiUnusableError(
                f"Incompatible API version: MCP server v{mcp_version} requires API "
                f"v{mcp_parts[0]}.{mcp_parts[1]}.x — found v{api_version}. Patch "
                "differences are allowed; major or minor mismatches are not supported."
            )
    _api_verified_at = time.monotonic()


def _check_api_health() -> None:
    """At stdio startup: exit unless the API is reachable and of this ``major.minor``."""
    try:
        _verify_api()
    except _ApiUnusableError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    logger.info("API health check passed (%s)", settings.api_base_url)


def _registered_tool_count() -> int | None:
    """How many tools are registered, for the startup banner.

    Derived rather than declared: the count is cosmetic, and a constant that
    only a human keeps in step with the registrations is a constant that goes
    stale. ``None`` when it cannot be read — a banner line is not worth failing
    a startup over.
    """
    try:
        return len(asyncio.run(mcp._list_tools()))
    except Exception:  # noqa: BLE001 - cosmetic; never blocks startup
        logger.debug("Could not count registered tools for the startup banner", exc_info=True)
        return None


def _detect_api_mode() -> tuple[bool, bool]:
    """Query the API to detect single-model mode and query execution support.

    Returns:
        (single_model_mode, query_execute_enabled)

    ``/v1/settings`` needs a credential on an API with auth. A refused one
    raises :class:`ToolError` rather than defaulting: the mode is fixed once
    detected, and a guess made from a 401 would outlast the credential's fix.
    The next request detects again.
    """
    client = _get_client()
    try:
        resp = client.get(f"{_API_V1}/settings")
        if resp.status_code in (401, 403):
            _raise_api_error(resp)
        resp.raise_for_status()
        data = resp.json()
        single = data.get("single_model_mode", False)
        can_execute = bool(data.get("query_execute", False))
        return single, can_execute
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("Could not detect API mode — defaulting to multi-model, no execute")
        return False, False


def _stateless_http_enabled() -> bool:
    """Whether the HTTP transport should run without per-connection session state.

    Stateless mode drops the MCP transport session entirely: no
    ``Mcp-Session-Id``, no stream resumability, no out-of-band server→client
    messages — every request is self-contained. Nothing here depends on that
    state (the tool phase is derived from explicit loaded-model state, never
    from the connection — see "Tool phase model"; no tool sends progress,
    sampling, or elicitation requests), so instances behind a load balancer no
    longer need session affinity.

    Note this is the *transport* session only. The API session and the
    loaded-model set are kept per identity (``_Identity``): process-global in
    the service modes, shared by every client of an instance as before; per
    signed-in user in ``oidc_user``, whose bearer token comes on every request.

    SSE cannot run stateless (it is a long-lived per-connection stream, and
    FastMCP rejects the combination), so it is forced off there.
    """
    if settings.mcp_transport == "sse":
        if settings.mcp_stateless_http:
            logger.warning(
                "MCP_STATELESS_HTTP is ignored on the sse transport — SSE requires a "
                "per-connection session. Use MCP_TRANSPORT=http for stateless operation."
            )
        return False
    return settings.mcp_stateless_http


def _is_loopback_bind(host: str) -> bool:
    """Whether the HTTP transport's bind address is reachable only on this box."""
    candidate = host.strip().strip("[]").lower()
    if candidate in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # A hostname we would have to resolve to classify. Treat it as exposed:
        # a warning that did not need saying costs a line, one that did and was
        # not said costs the port.
        return False


def _warn_if_transport_exposed() -> None:
    """Warn when the HTTP transport is reachable off-box with nothing in front.

    Unlike stdio (pipes to a child process, no socket at all), the HTTP/SSE
    transport is a real listener — and this server terminates no TLS and,
    outside the user mode (``oidc_user`` checks every caller's token, which
    leaves only the TLS half), authenticates no caller. Whatever can reach the
    port can call every registered tool, spending the server's own ``API_KEY``
    against the API, and reads the queries and results in transit. The
    credential itself never crosses this hop; the access it buys does.

    Both jobs belong to an ingress, so this is scoped to actual evidence of
    exposure rather than firing on every HTTP start and teaching operators to
    scroll past it. ``MCP_BEHIND_PROXY=true`` acknowledges an ingress we have no
    way to detect.

    Cloud Run is the one deployment we *can* detect, and it splits the two jobs:
    TLS is automatic (the service URL is HTTPS and plain HTTP is forwarded to
    the container, so the mandatory 0.0.0.0 bind says nothing about exposure),
    while what may reach the service is a deploy-time choice this process cannot
    read. Suppressing the alarm there and saying nothing else would let silence
    read as proof of protection, so it gets its own note about the half that is
    still the operator's.

    That half is not only IAM. An ``--ingress`` restriction or an authenticating
    load balancer in front settles it just as well, and OBSL's own rollout uses
    exactly that — ``--allow-unauthenticated`` with
    ``--ingress internal-and-cloud-load-balancing``, which is a closed service
    rather than an open one. So the note names the ways rather than prescribing
    IAM: telling an operator to fix something they solved differently is how a
    correct warning gets classified as noise.

    The counterpart on the API's own listeners is its 2.28.0 warning for Flight
    SQL authenticating over plaintext gRPC. Those listeners — and, since 2.29.0,
    its REST surface — can serve TLS themselves (``*_TLS_*``); this transport
    cannot, so an ingress is the only way it gets TLS at all.
    """
    if settings.mcp_transport == "stdio":
        return
    if settings.mcp_behind_proxy or _is_loopback_bind(settings.mcp_server_host):
        return
    if _auth_mode() == AUTH_USER:
        # Callers are authenticated — each brings a token issued for this
        # server — so only the TLS half is left, which Cloud Run provides.
        if os.environ.get("K_SERVICE"):
            return
        logger.warning(
            "The %s transport is bound to %s with no TLS: callers are authenticated "
            "(MCP_AUTH_MODE=oidc_user), but their bearer tokens, queries and results "
            "are readable in transit on port %s. Put an ingress in front that "
            "terminates TLS, or set MCP_BEHIND_PROXY=true to acknowledge one this "
            "server cannot see.",
            settings.mcp_transport,
            settings.mcp_server_host,
            settings.effective_port,
        )
        return
    if os.environ.get("K_SERVICE"):
        logger.info(
            "Cloud Run terminates TLS at its front end, so the %s bind on port "
            "%s is expected and traffic to the service URL is encrypted. What "
            "may reach the service is a separate, deploy-time choice this "
            "process cannot read: IAM invoker permission, an --ingress "
            "restriction, or an authenticating load balancer in front. With "
            "none of those, --allow-unauthenticated leaves every tool callable "
            "by anyone who learns the URL, spending this server's API "
            "credential. Confirm one of them is in place.",
            settings.mcp_server_host,
            settings.effective_port,
        )
        return
    logger.warning(
        "The %s transport is bound to %s with no TLS and no caller "
        "authentication: anything that reaches port %s can call every tool "
        "with this server's API credential, over a channel that is readable "
        "in transit. Put an ingress in front that terminates TLS and controls "
        "access, bind MCP_SERVER_HOST to loopback, or set MCP_BEHIND_PROXY=true "
        "to acknowledge one this server cannot see.",
        settings.mcp_transport,
        settings.mcp_server_host,
        settings.effective_port,
    )


def main() -> None:
    """Run the MCP server using settings from environment / .env file."""
    _configure_logging()

    logger.info("=" * 60)
    logger.info("OrionBelt Semantic Layer MCP Server v%s", __version__)
    logger.info("Thin MCP server — delegates to OrionBelt Semantic Layer REST API")
    logger.info("=" * 60)

    # Build the API client now, on every transport: it carries the TLS context,
    # so unusable certificate settings stop startup naming the setting, rather
    # than failing on the first tool call (HTTP defers everything else).
    try:
        _get_client()
    except ApiTLSConfigError as exc:
        logger.error("API TLS configuration is unusable: %s", exc)
        raise SystemExit(1) from None

    auth_mode = _auth_mode()
    if auth_mode == AUTH_USER and settings.mcp_transport == "stdio":
        logger.error(
            "MCP_AUTH_MODE=oidc_user signs users in through their MCP client over "
            "HTTP, and stdio has no such sign-in. Run MCP_TRANSPORT=http, or for a "
            "local stdio server set API_KEY to your BI token from OBSL's /tokens page."
        )
        raise SystemExit(1)
    if auth_mode in _OAUTH_MODES and settings.api_key:
        logger.warning("API_KEY is ignored under MCP_AUTH_MODE=%s", auth_mode)

    if settings.mcp_transport == "stdio":
        # stdio: eager init — connection is local & synchronous, fail-fast is fine.
        _check_api_health()
        try:
            _setup_mode_tools()
        except (ToolError, AuthConfigError) as exc:
            # A refused credential (or a client-credentials token the IdP would
            # not issue): say so now, rather than serve a guessed mode.
            logger.error("Could not read the API's settings: %s", exc)
            raise SystemExit(1) from None

        if _single_model_mode:
            # Verify the pre-loaded model is valid and reachable (fail fast)
            client = _get_client()
            try:
                resp = client.get(f"{_API_V1}/schema")
                resp.raise_for_status()
                data = resp.json()
                logger.info(
                    "Pre-loaded model validated: %d data objects, %d dimensions, "
                    "%d measures, %d metrics",
                    len(data.get("data_objects", [])),
                    len(data.get("dimensions", [])),
                    len(data.get("measures", [])),
                    len(data.get("metrics", [])),
                )
            except httpx.HTTPStatusError as exc:
                logger.error("Pre-loaded model not available: %s", exc.response.text)
                raise SystemExit(1) from None
            except httpx.HTTPError as exc:
                logger.error("Cannot reach API to validate pre-loaded model: %s", exc)
                raise SystemExit(1) from None
        # Counted, never constant: the two modes register different sets and
        # both have drifted from a hand-maintained number before. execute_query
        # is always registered and gated at list time by capability, so this
        # counts everything registered — the *visible* surface is smaller when
        # query_execute is off (−1) or in the design phase (run-only verbs
        # hidden); single-model mode is always run-time.
        tool_count = _registered_tool_count()
        mode_label = "single-model" if _single_model_mode else "multi-model"
        stateless = False
    else:
        # HTTP/SSE: mode detection waits for the first request (the version
        # gate and LazyInitMiddleware, installed at import), so the container
        # starts immediately (good for Cloud Run cold starts).
        tool_count = 0
        mode_label = "deferred (lazy init on first request)"
        # Resolved once — the sse fallback logs a warning on the way.
        stateless = _stateless_http_enabled()

    logger.info("")
    logger.info("Configuration:")
    logger.info("  API URL:    %s", settings.api_base_url)
    logger.info("  API TLS:    %s", _api_tls_summary())
    logger.info("  API auth:   %s", _auth_summary())
    logger.info("  Transport:  %s", settings.mcp_transport)
    if settings.mcp_transport != "stdio":
        logger.info("  Host:       %s", settings.mcp_server_host)
        logger.info("  Port:       %s", settings.effective_port)
        logger.info("  Stateless:  %s", stateless)
    logger.info("  Log Level:  %s", settings.log_level)
    logger.info("  Log Format: %s", settings.log_format)
    logger.info("  Timeout:    %ss", settings.api_timeout)
    logger.info("")
    _warn_if_transport_exposed()
    if settings.mcp_transport == "stdio":
        counted = "?" if tool_count is None else str(tool_count)
        logger.info("Registered %s MCP tools (%s mode)", counted, mode_label)
    else:
        logger.info("Tool registration: %s", mode_label)
    logger.info("")

    try:
        if settings.mcp_transport == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(
                transport=settings.mcp_transport,
                host=settings.mcp_server_host,
                port=settings.effective_port,
                log_level=settings.log_level.lower(),
                stateless_http=stateless,
            )
    except KeyboardInterrupt:
        logger.info("Shutting down…")


if __name__ == "__main__":
    main()
