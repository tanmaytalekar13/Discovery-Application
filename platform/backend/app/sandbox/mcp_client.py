"""Reusable MCP JSON-RPC client helper for testing remote tools.

Wraps the official `mcp` library to provide a simple, typed API for
the three operations the Test Tool flow needs:

  - initialize() + list_tools()  (connect)
  - call_tool(name, args)         (invoke)

The wrapper adds:
  - Auth-required detection (401/403 + WWW-Authenticate header)
  - Bearer token / API key injection via headers
  - Configurable timeout and retry budget
  - URL-safety check (refuses non-HTTPS / private / loopback addresses
    unless explicitly allowed) for SSRF prevention
  - Clean error translation into structured results

This module is transport-agnostic at the API level: callers pass a
URL and we pick streamable-http or SSE based on the URL or an
explicit hint. The supported `mcp` SDK range is kept current with the
handshake-era protocol revisions used by official Registry remotes.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.sandbox.auth_signals import extract_inband_auth_error

# McpError is the JSON-RPC-level error raised by the official `mcp` SDK
# (e.g. when a server terminates a session, sends back a JSON-RPC error
# object during initialize/call_tool, etc). It is NOT an httpx error and
# is NOT a subclass of our own MCPClientError hierarchy, so it must be
# handled explicitly wherever we classify connection/invoke failures -
# otherwise it escapes uncaught straight past classify_connection_error
# and up into the API layer as an unhandled 500.
from mcp.shared.exceptions import McpError

# BaseExceptionGroup is built-in in Python 3.11+; fall back to the
# exceptiongroup backport package on earlier versions.
try:
    BaseExceptionGroup  # type: ignore[name-defined]
except NameError:
    from exceptiongroup import BaseExceptionGroup  # type: ignore[no-redef]

# ExceptionGroup is built-in in Python 3.11+; backport for earlier versions.
try:
    ExceptionGroup  # type: ignore[name-defined]
except NameError:
    from exceptiongroup import ExceptionGroup  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 1  # 1 = one retry after the first attempt
DEFAULT_SSE_READ_TIMEOUT = 60.0

# Hosted-remote domains whose MCP endpoint behavior is known to break the
# standard client flow in ways no amount of correct auth can fix. Smithery
# removed the standard MCP initialize handshake/session semantics for its
# hosted remotes (2026-07-28 release, part of its Arcade.dev-runtime
# migration) and appears to now require a Smithery-issued connection
# (their `@smithery/api` `connections.set()` / `createConnection()` flow)
# rather than accepting a raw provider bearer token directly. Until that
# flow is integrated, surface a specific "known incompatible" message
# instead of a misleading "invalid token" one.
_KNOWN_INCOMPATIBLE_HOSTS = frozenset({"server.smithery.ai"})


# ---------------------------------------------------------------------------
# Data classes - public contract
# ---------------------------------------------------------------------------


@dataclass
class RemoteToolInfo:
    """A single tool as returned by the remote server's tools/list."""

    name: str
    description: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionErrorInfo:
    """Structured classification of a connection/invoke failure.

    Produced by :func:`classify_connection_error`. All fields are
    guaranteed to be set so the UI can branch on them without null checks.
    """

    # Machine-readable reason — one of the AUTH_REASON_* constants.
    auth_reason: str
    # Human-readable message safe to show to the user (no tokens, no internals).
    user_message: str
    # True when a manual bearer-token / API-key input is a useful next step.
    show_token_input: bool
    # True when an OAuth / provider-authorization flow is a useful next step.
    show_oauth_button: bool


# ---------------------------------------------------------------------------
# Auth reason constants
# ---------------------------------------------------------------------------
AUTH_REASON_UNAUTHORIZED = "unauthorized"
AUTH_REASON_PAYMENT_REQUIRED = "payment_required"
AUTH_REASON_NOT_FOUND = "not_found"
AUTH_REASON_RATE_LIMITED = "rate_limited"
AUTH_REASON_SERVER_ERROR = "server_error"
AUTH_REASON_CONNECTION_ERROR = "connection_error"
AUTH_REASON_TIMEOUT = "timeout"
AUTH_REASON_INCOMPATIBLE = "incompatible"
AUTH_REASON_UNKNOWN = "unknown"


def _mcp_error_message(exc: McpError) -> str:
    """Best-effort extraction of the human-readable message from an McpError.

    The `mcp` SDK raises McpError wrapping a JSON-RPC ErrorData object
    (exc.error.message / exc.error.code). Some versions/transports may
    not populate that structured object, so fall back to str(exc).
    """
    err = getattr(exc, "error", None)
    msg = getattr(err, "message", None) if err is not None else None
    return msg or str(exc) or "Session terminated"


def _known_incompatible_host(url: str | None) -> bool:
    """Return True if `url` points at a host with a known-broken MCP flow.

    Pure hostname check — no network calls. Matches on exact hostname
    (case-insensitive) rather than substring, so a URL like
    `https://not-server.smithery.ai.evil.example` would NOT match, and
    subdomains would need to be added explicitly.
    """
    if not url:
        return False
    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        return False
    return bool(host) and host.lower() in _KNOWN_INCOMPATIBLE_HOSTS


def _classify_auth_rejection_body(
    status_code: int, body_text: str, headers: dict[str, str]
) -> ConnectionErrorInfo:
    """Map a non-JSON (or JSON without a recognizable `code`) auth-rejection
    body to a ConnectionErrorInfo.

    Best-effort: an unrecognized body keeps the generic 401/403 message.
    """
    lower_body = body_text.lower()
    if status_code == 402 or "payment" in lower_body or "subscription" in lower_body:
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_PAYMENT_REQUIRED,
            user_message="This tool requires a paid subscription. "
            "Set up an account or subscription on the provider's website first.",
            show_token_input=False,
            show_oauth_button=False,
        )
    if "expired" in lower_body or "revoked" in lower_body:
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_UNAUTHORIZED,
            user_message="The server rejected the provided credentials "
            f"(HTTP {status_code}): the token appears to be expired or "
            "revoked. Generate a new token and try again.",
            show_token_input=True,
            show_oauth_button=True,
        )
    if "invalid" in lower_body or "malformed" in lower_body:
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_UNAUTHORIZED,
            user_message="The server rejected the provided credentials "
            f"(HTTP {status_code}): the token is invalid. Check the token "
            "and try again.",
            show_token_input=True,
            show_oauth_button=True,
        )
    if "oauth" in lower_body:
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_UNAUTHORIZED,
            user_message="This MCP server rejected the provided credentials "
            f"(HTTP {status_code}). It appears to require an OAuth "
            "authorization flow rather than a static API token.",
            show_token_input=False,
            show_oauth_button=True,
        )
    return ConnectionErrorInfo(
        auth_reason=AUTH_REASON_UNAUTHORIZED,
        user_message="This MCP server requires credentials. "
        "Provide your API key or bearer token to continue.",
        show_token_input=True,
        show_oauth_button=True,  # OAuth discovery done by caller if needed
    )


async def _probe_auth_rejection(
    url: str, headers: dict[str, str], timeout: float
) -> ConnectionErrorInfo | None:
    """Re-send one plain initialize POST to a server that just rejected us
    with 401/403, and read the error body directly.

    The `mcp` SDK streams upstream responses inside a task and closes the
    body the moment `raise_for_status()` fires, so the HTTPStatusError that
    propagates to us carries only the status line — never the server's
    error payload (e.g. Notion's `{"code":"restricted_resource",
    "message":"Endpoint unavailable."}`, which means the token is VALID
    but the endpoint forbids static API keys). This probe recovers that
    body so the UI can show the real reason instead of a generic
    "credentials required" message.

    Never raises: any failure returns None and the caller falls back to
    the generic classification. Header/auth failures here are logged at
    debug level only — they must never mask the original error.
    """
    # Upstream rejected us with 401/403 — the stored token (if any) is
    # deliberately NOT echoed in probe logs. Only status + short body are
    # logged, and only at debug level.
    probe_url = url.split("?", 1)[0]
    try:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "mcp-tool-test", "version": "1.0.0"},
            },
        }
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(
                probe_url,
                json=body,
                headers=headers,
            )
        if resp.status_code not in _AUTH_STATUS_CODES:
            return None
        logger.debug(
            "MCP auth-rejection probe (url=%s status=%s body=%r)",
            probe_url, resp.status_code, resp.text[:300],
        )
        content_type = resp.headers.get("content-type", "").lower()
        if content_type.startswith("application/json"):
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                code = str(payload.get("code") or payload.get("error") or "")
                message = str(payload.get("message") or payload.get("error_description") or "")
                hint = ": ".join(part for part in (code, message) if part)
                if hint:
                    return ConnectionErrorInfo(
                        auth_reason=AUTH_REASON_UNAUTHORIZED,
                        user_message=(
                            f"The MCP server rejected the credentials "
                            f"(HTTP {resp.status_code}): {hint}"
                        ),
                        # A 403 from a JSON-RPC body (e.g. Notion's
                        # restricted_resource) usually means the token was
                        # accepted but the endpoint/flow is forbidden — a
                        # different token rarely helps. Keep OAuth visible.
                        show_token_input=resp.status_code == 401,
                        show_oauth_button=True,
                    )
        return _classify_auth_rejection_body(
            resp.status_code, resp.text[:500], dict(resp.headers)
        )
    except Exception:  # noqa: BLE001 - diagnostics must never mask the original error
        logger.debug("MCP auth-rejection probe failed", exc_info=True)
        return None


def _extract_auth_request_headers(exc: BaseException) -> dict[str, str]:
    """Pull the auth-related request headers from the root HTTPStatusError.

    Used to replay the failed request's credentials in the body-recovery
    probe. Returns {} when no auth headers were present (e.g. the failure
    happened on an unauthenticated request).
    """
    root = exc
    while isinstance(root, MCPClientError) and root.__cause__ is not None:
        root = root.__cause__
    root = _unwrap_exception(root)
    request = getattr(root, "request", None)
    headers = getattr(request, "headers", None)
    if not headers:
        return {}
    return {
        key: value
        for key, value in dict(headers).items()
        if key.lower() in {"authorization", "x-api-key", "x-auth-token", "api-key"}
    }


async def _maybe_probe_auth_rejection(
    info: ConnectionErrorInfo,
    exc: BaseException,
    url: str,
) -> ConnectionErrorInfo:
    """Enrich a 401/403 classification with the server's real error body.

    The `mcp` SDK closes the streamed upstream response before raising, so
    the HTTPStatusError carries only the status line. When the failure is
    an auth rejection AND the failed request carried credentials, re-send
    one plain initialize POST to recover the server's JSON error body
    (e.g. Notion returns {"code":"restricted_resource"} for a VALID token
    used without OAuth — a fundamentally different problem from an invalid
    token). Never raises; falls back to the original `info` on any problem.
    """
    if info.auth_reason != AUTH_REASON_UNAUTHORIZED:
        return info
    auth_headers = _extract_auth_request_headers(exc)
    if not auth_headers:
        # No credentials were attached — the generic "requires credentials"
        # message is already accurate.
        return info
    probed = await _probe_auth_rejection(url, auth_headers, timeout=10.0)
    if probed is None:
        return info
    logger.info(
        "MCP auth rejection enriched via probe (url=%s message=%r)",
        url.split("?", 1)[0], probed.user_message,
    )
    return probed


def classify_connection_error(
    exc: BaseException, url: str | None = None
) -> ConnectionErrorInfo:
    """Classify a connection/invoke failure for user-facing display.

    Handles:
      - Hosts with a known-incompatible hosted-remote MCP flow (checked
        first, before any status/error-text based classification, since
        those hosts return misleading signals — e.g. a 401 "invalid_token"
        even when the token is correct — that would otherwise be
        classified as an ordinary auth problem)
      - Direct HTTP errors (401, 402, 403, 404, 429, 5xx)
      - ExceptionGroup-wrapped errors from the MCP library's internal task groups
      - MCP JSON-RPC protocol-level errors (McpError), e.g. a server that
        terminates the session instead of returning a plain HTTP 401
      - Network-level errors (DNS, TLS, connect refused, genuine timeouts)
      - Any other unexpected exception

    This function is pure — no side effects, no network calls, no token logging.

    Args:
        exc: Any exception from the MCP call stack.
        url: The URL that was being dialed when `exc` was raised, if known.
            Used only for the known-incompatible-host check above; every
            other branch classifies purely from `exc`.

    Returns:
        ConnectionErrorInfo with auth_reason, user_message, show_token_input,
        and show_oauth_button fields populated.
    """
    # A known-incompatible host is checked first and unconditionally: no
    # amount of correct auth fixes it, so it should never be mislabeled as
    # an ordinary "unauthorized" or "unknown" case further down.
    if _known_incompatible_host(url):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_INCOMPATIBLE,
            user_message=(
                "This tool is hosted on Smithery, which changed how it "
                "handles authentication for its remote MCP servers. "
                "Smithery no longer accepts a provider API key/token "
                "directly — it requires a connection created through "
                "Smithery's own Connect API, which this app doesn't yet "
                "support. Providing a token here will not fix this."
            ),
            show_token_input=False,
            show_oauth_button=False,
        )

    # Recursively unwrap ExceptionGroup / BaseExceptionGroup to find the
    # first meaningful exception. MCP library wraps errors in TaskGroups.
    # Follow __cause__ chain to handle nested conversions:
    #   MCPTransportError → MCPTimeoutError → CancelledError → BaseExceptionGroup → HTTPStatusError
    root_cause = exc
    while True:
        if isinstance(root_cause, (ExceptionGroup, BaseExceptionGroup)):
            root_cause = _unwrap_exception(root_cause)
        elif (
            isinstance(root_cause, MCPClientError) and root_cause.__cause__ is not None
        ):
            root_cause = root_cause.__cause__
        else:
            break

    # --- HTTP status-based classification ---
    if isinstance(root_cause, httpx.HTTPStatusError):
        status = root_cause.response.status_code
        headers = root_cause.response.headers

        if status in (401, 403):
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_UNAUTHORIZED,
                user_message="This MCP server requires credentials. "
                "Provide your API key or bearer token to continue.",
                show_token_input=True,
                show_oauth_button=True,  # OAuth discovery done by caller if needed
            )

        if status == 402:
            retry_after = headers.get("retry-after", "")
            msg = "This tool requires a paid subscription. "
            if retry_after:
                msg += f"Rate limit: retry after {retry_after}."
            else:
                msg += (
                    "Set up an account or subscription on the provider's website first."
                )
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_PAYMENT_REQUIRED,
                user_message=msg,
                show_token_input=False,
                show_oauth_button=False,
            )

        if status == 404:
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_NOT_FOUND,
                user_message="MCP endpoint not found. The URL may be incorrect "
                "or the server may no longer be hosted at this address.",
                show_token_input=False,
                show_oauth_button=False,
            )

        if status == 429:
            retry_after = headers.get("retry-after", "")
            msg = "Too many requests. "
            if retry_after:
                msg += f"Retry after {retry_after}."
            else:
                msg += "Slow down or wait before trying again."
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_RATE_LIMITED,
                user_message=msg,
                show_token_input=False,
                show_oauth_button=False,
            )

        if status >= 500:
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_SERVER_ERROR,
                user_message="The tool's server is temporarily unavailable "
                "(internal error). Try again later.",
                show_token_input=False,
                show_oauth_button=False,
            )

        # Other 4xx — treat as unknown
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_UNKNOWN,
            user_message=f"Server rejected the request (HTTP {status}). "
            "If credentials are needed, try providing them below.",
            show_token_input=True,
            show_oauth_button=True,
        )

    # --- MCP protocol-level errors (JSON-RPC error responses) ---
    # Some hosted MCP transports don't surface auth failures as a plain
    # HTTP 401/403 - instead they let the transport-level handshake
    # succeed and then terminate the session (or return a JSON-RPC error)
    # during initialize/call_tool. We can't always tell "session
    # terminated because of bad/missing auth" apart from a genuine
    # protocol failure, so - like the generic fallback below - we offer
    # credentials as a next step rather than dead-ending the user. If the
    # error text itself names an auth problem, classify it more
    # specifically as unauthorized so the copy is more accurate.
    if isinstance(root_cause, McpError):
        msg = _mcp_error_message(root_cause)
        if _is_auth_error_text(msg):
            return ConnectionErrorInfo(
                auth_reason=AUTH_REASON_UNAUTHORIZED,
                user_message="This MCP server requires credentials. "
                "Provide your API key or bearer token to continue.",
                show_token_input=True,
                show_oauth_button=True,
            )
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_UNKNOWN,
            user_message=(
                f"The MCP server ended the connection ({msg}). If this tool "
                "requires credentials, provide your API key or bearer token below."
            ),
            show_token_input=True,
            show_oauth_button=True,
        )

    # --- Network-level errors ---
    exc_type = type(root_cause).__name__
    exc_msg = str(root_cause).lower()

    # SSL/TLS errors
    if any(tag in exc_type.lower() for tag in ("ssl", "tls", "certificate")) or any(
        tag in exc_msg for tag in ("ssl", "tls", "certificate", "sslerror")
    ):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_CONNECTION_ERROR,
            user_message="Secure connection failed. The server may be misconfigured "
            "or using an invalid TLS certificate.",
            show_token_input=True,
            show_oauth_button=True,
        )

    # DNS resolution failures
    if (
        any(
            tag in exc_msg
            for tag in (
                "name or service not known",
                "no address associated",
                "getaddrinfo failed",
                "dns",
            )
        )
        or "dns" in exc_type.lower()
    ):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_CONNECTION_ERROR,
            user_message="Server address could not be resolved. "
            "The URL may be incorrect.",
            show_token_input=True,
            show_oauth_button=True,
        )

    # Connection refused / reset / unreachable
    if any(
        tag in exc_msg
        for tag in (
            "connection refused",
            "connection reset",
            "connection closed",
            "cannot connect",
            "network unreachable",
            "host unreachable",
        )
    ):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_CONNECTION_ERROR,
            user_message="Could not connect to the server. "
            "The server may be down or the URL may be incorrect.",
            show_token_input=True,
            show_oauth_button=True,
        )

    # Timeout — no HTTP response received at all
    if (
        isinstance(
            root_cause,
            (
                asyncio.TimeoutError,
                asyncio.CancelledError,
                httpx.TimeoutException,
                TimeoutError,
                TimeoutError,
            ),
        )
        or any(tag in exc_type.lower() for tag in ("timeout", "cancelled"))
        or "timeout" in exc_msg
        or "timed out" in exc_msg
    ):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_TIMEOUT,
            user_message="Server is not responding. This may be an authentication "
            "issue or the server may be temporarily offline.",
            show_token_input=True,
            show_oauth_button=True,
        )

    # MCP protocol version mismatch — especially likely for a remote returned
    # by the Registry when a client SDK is older than the server's revision.
    if exc_msg and (
        "protocol version" in exc_msg
        or "unsupported protocol" in exc_msg
        or "incompatible protocol" in exc_msg
        or "invalid protocol" in exc_msg
        or ("mcp" in exc_msg and ("version" in exc_msg or "protocol" in exc_msg))
    ):
        return ConnectionErrorInfo(
            auth_reason=AUTH_REASON_INCOMPATIBLE,
            user_message="This MCP server and this application could not negotiate "
            "a compatible MCP protocol version. Try the server's "
            "alternate transport if one is listed, or update the server.",
            show_token_input=False,
            show_oauth_button=False,
        )

    # Fallback: unknown / unexpected exception
    return ConnectionErrorInfo(
        auth_reason=AUTH_REASON_UNKNOWN,
        user_message="Connection failed. If this tool requires credentials, "
        "provide your API key or bearer token below.",
        show_token_input=True,
        show_oauth_button=True,
    )


def _log_remote_failure(operation: str, url: str, exc: BaseException) -> None:
    """Log actionable upstream diagnostics without logging credentials."""
    root = exc
    while isinstance(root, MCPClientError) and root.__cause__ is not None:
        root = root.__cause__
    root = _unwrap_exception(root)
    if isinstance(root, httpx.HTTPStatusError):
        response = root.response
        if response.status_code in _AUTH_STATUS_CODES:
            # Auth rejections are an expected part of the connect flow (the
            # user often connects before entering credentials). Log one
            # concise line — no exception traceback noise.
            logger.warning(
                "MCP %s auth rejection (url=%s status=%s) — not an app error",
                operation, url.split("?", 1)[0], response.status_code,
            )
        else:
            logger.error(
                "MCP %s upstream response (url=%s status=%s headers=%s body=%r)",
                operation, url.split("?", 1)[0], response.status_code,
                {key: value for key, value in response.headers.items()
                 if key.lower() in {"content-type", "www-authenticate", "retry-after", "mcp-session-id"}},
                response.text[:512] if response.is_stream_consumed else "<streaming body not read by MCP transport>", exc_info=exc,
            )
    elif isinstance(root, httpx.TimeoutException):
        logger.error("MCP %s timeout (url=%s type=%s): %s", operation, url.split("?", 1)[0], type(root).__name__, root, exc_info=exc)
    elif isinstance(root, McpError):
        logger.error(
            "MCP %s protocol-level error (url=%s message=%s): %s",
            operation, url.split("?", 1)[0], _mcp_error_message(root), root, exc_info=exc,
        )
    else:
        logger.error("MCP %s transport/protocol failure (url=%s type=%s): %s", operation, url.split("?", 1)[0], type(root).__name__, root, exc_info=exc)


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Recursively unwrap ExceptionGroup / BaseExceptionGroup to find the
    first non-ExceptionGroup exception.

    MCP's streamablehttp_client runs requests in a TaskGroup; when one fails,
    the group wraps it in an ExceptionGroup (or BaseExceptionGroup in Python 3.11+).
    We need to extract the actual cause so we can classify it.
    """
    if isinstance(exc, BaseExceptionGroup):
        for sub_exc in exc.exceptions:
            # Recurse in case of nested groups
            inner = _unwrap_exception(sub_exc)
            if not isinstance(inner, BaseExceptionGroup):
                return inner
        # All sub-exceptions are groups — return the outermost
        return exc
    return exc


@dataclass
class ConnectResult:
    """Result of a connect/handshake attempt.

    Exactly one of `connected=True` (with populated `tools`) or
    `auth_required=True` or an `error` will be set. Callers should
    branch on `auth_required` first (UI shows Connect Account), then
    on `connected`, then on `error`.
    """

    connected: bool = False
    auth_required: bool = False
    transport: str | None = None  # "streamable-http" | "sse"
    tools: list[RemoteToolInfo] = field(default_factory=list)
    error: str | None = None
    server_info: dict[str, Any] = field(default_factory=dict)
    # Error classification (populated when connected=False and error is set)
    auth_reason: str | None = None
    user_message: str | None = None
    show_token_input: bool = False
    show_oauth_button: bool = False


@dataclass
class InvokeResult:
    """Result of a tools/call invocation."""

    status: str  # "success" | "error"
    result: Any = None
    error: str | None = None
    requires_auth: bool = False
    duration_ms: int = 0
    # Error classification (populated when status="error")
    auth_reason: str | None = None
    user_message: str | None = None
    show_token_input: bool = False
    show_oauth_button: bool = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPClientError(Exception):
    """Base error for MCP client failures."""


class MCPTimeoutError(MCPClientError):
    """Connection or read timeout."""


class MCPTransportError(MCPClientError):
    """Network-level failure (connection refused, DNS, TLS, etc.)."""


class MCPAuthRequiredError(MCPClientError):
    """Server requires authentication we don't have."""

    def __init__(self, message: str, www_authenticate: str | None = None):
        super().__init__(message)
        self.www_authenticate = www_authenticate


class MCPProtocolError(MCPClientError):
    """Server returned malformed JSON-RPC or unexpected payload."""


class UnsafeURLError(MCPClientError):
    """URL was rejected by the SSRF guard."""


# ---------------------------------------------------------------------------
# URL safety (SSRF prevention)
# ---------------------------------------------------------------------------

# Hosts we always refuse to dial. These are routable on most networks
# and must not be reachable from the backend, otherwise a malicious
# registry entry could pivot into our internal infra.
_BLOCKED_HOST_PATTERNS = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "metadata.google.internal",  # GCP metadata
    "169.254.169.254",  # AWS / Azure / GCP metadata
}

# Private / loopback IP ranges we never dial.
# We use a simple string-prefix check on the hostname; for production
# this should be replaced with `ipaddress.ip_address(...).is_private`.
_PRIVATE_HOST_PREFIXES = (
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.20.",
    "172.21.",
    "172.22.",
    "172.23.",
    "172.24.",
    "172.25.",
    "172.26.",
    "172.27.",
    "172.28.",
    "172.29.",
    "172.30.",
    "172.31.",
    "192.168.",
    "fc",
    "fd",  # IPv6 ULA
    "fe80",  # IPv6 link-local
)


def _is_unsafe_host(host: str) -> bool:
    host_lower = host.lower()
    if host_lower in _BLOCKED_HOST_PATTERNS:
        return True
    for prefix in _PRIVATE_HOST_PREFIXES:
        if host_lower.startswith(prefix):
            return True
    return False


def _validate_url(url: str, *, allow_local: bool = False) -> str:
    """Validate the URL is safe to dial.

    Only http(s) schemes are accepted. By default, loopback and
    private-network hosts are rejected (SSRF guard). Set
    `allow_local=True` to permit loopback - useful for tests, never
    for production paths serving user input.
    """
    if not url:
        raise UnsafeURLError("URL is empty")

    try:
        parsed = urlparse(url)
    except (ValueError, TypeError) as exc:
        raise UnsafeURLError(f"Malformed URL: {url!r}") from exc

    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Unsupported scheme {parsed.scheme!r} (only http/https)")

    if not parsed.hostname:
        raise UnsafeURLError("URL has no hostname")

    if not allow_local and _is_unsafe_host(parsed.hostname):
        raise UnsafeURLError(
            f"Refusing to dial non-public host {parsed.hostname!r} "
            f"(SSRF guard; pass allow_local=True to override)"
        )

    return url


# ---------------------------------------------------------------------------
# Auth-required detection
# ---------------------------------------------------------------------------

_AUTH_STATUS_CODES = {401, 403}


def _is_auth_required_response(
    response: httpx.Response | None,
    exception: Exception | None,
) -> bool:
    """Decide whether a connect failure was an auth problem.

    Triggers on:
      - 401/403 with or without WWW-Authenticate
      - 401/403 thrown as an HTTPStatusError
    """
    if response is not None and response.status_code in _AUTH_STATUS_CODES:
        return True
    if exception is not None:
        for attr in ("status_code", "code"):
            value = getattr(exception, attr, None)
            if value in _AUTH_STATUS_CODES:
                return True
        msg = str(exception).lower()
        if "401" in msg or "403" in msg or "unauthorized" in msg:
            return True
    return False


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------


def _pick_transport(url: str, preferred: str | None) -> str:
    """Pick a transport name for reporting purposes.

    The actual transport selection happens inside the `mcp` library
    based on its own URL conventions; we just normalize for our
    response payload.
    """
    if preferred in ("streamable-http", "sse"):
        return preferred
    # MCP convention: SSE endpoints typically live under /sse
    return "sse" if url.rstrip("/").endswith("/sse") else "streamable-http"


# ---------------------------------------------------------------------------
# Low-level: dial and run an MCP session
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _open_session(
    url: str,
    transport: str,
    headers: dict[str, str],
    timeout: float,
    auth_token: str | None = None,
):
    """Open an MCP client session, yielding a ClientSession bound to
    the chosen transport.

    Imports the mcp library lazily so that unit tests of the rest of
    the module don't pay the import cost, and so import errors are
    isolated to where they matter.
    """
    from mcp import ClientSession

    # Credentials are passed only in the declared HTTP header.  Adding a
    # guessed `?key=` parameter is non-standard, leaks into proxy logs, and
    # breaks servers that validate their endpoint query strictly.
    if transport == "streamable-http":
        from mcp.client.streamable_http import streamablehttp_client

        client_ctx = streamablehttp_client(
            url=url,
            headers=headers or None,
            timeout=timeout,
            sse_read_timeout=DEFAULT_SSE_READ_TIMEOUT,
        )
    elif transport == "sse":
        from mcp.client.sse import sse_client

        client_ctx = sse_client(
            url=url,
            headers=headers or None,
            timeout=timeout,
            sse_read_timeout=DEFAULT_SSE_READ_TIMEOUT,
        )
    else:
        raise MCPClientError(f"Unknown transport: {transport!r}")

    # streamablehttp_client / sse_client return (read, write, get_session_id)
    # We need to enter the async generators and then build a ClientSession.
    streams = await client_ctx.__aenter__()
    try:
        read_stream, write_stream, *_rest = streams
        session = ClientSession(read_stream, write_stream)
        await session.__aenter__()
        try:
            yield session
        finally:
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("Session close failed", exc_info=True)
    finally:
        try:
            await client_ctx.__aexit__(None, None, None)
        except (ExceptionGroup, BaseExceptionGroup) as exc:
            # MCP library raises ExceptionGroups from internal TaskGroup
            # cleanup — re-raise so the outer handler can classify the real
            # HTTP status, instead of silently swallowing it.
            root = _unwrap_exception(exc)
            raise MCPClientError(
                f"Transport cleanup raised {type(root).__name__}: {root}"
            ) from exc
        except Exception:  # noqa: BLE001
            logger.debug("Transport close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class MCPTestClient:
    """High-level MCP client for the Test Tool flow.

    Construct with a URL and (optionally) an auth token, then call
    `connect()` and `invoke()`. The client is stateless - each method
    opens its own short-lived MCP session - so a single instance can
    be reused across requests without locking.
    """

    def __init__(
        self,
        url: str,
        *,
        auth_token: str | None = None,
        preferred_transport: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        allow_local_host: bool = False,
        auth_header: str | None = None,
        auth_value_prefix: str | None = None,
    ) -> None:
        self._url = _validate_url(url, allow_local=allow_local_host)
        self._auth_token = auth_token
        self._preferred_transport = preferred_transport
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport_name = _pick_transport(self._url, preferred_transport)
        self._auth_header = auth_header
        self._auth_value_prefix = auth_value_prefix

    # ---- headers ---------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Build the request headers, including auth if present."""
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "User-Agent": "DiscoveryApplicationBot/1.0 (+tool-test)",
        }
        if self._auth_token:
            trimmed_token = self._auth_token.strip() if isinstance(self._auth_token, str) else self._auth_token
            # Log hash/length only (BUG 2 diagnosis) — never log raw token
            logger.info(
                "Remote MCP auth stage=header_build token_len=%d token_hash=%s",
                len(trimmed_token) if isinstance(trimmed_token, str) else 0,
                hashlib.sha256(str(trimmed_token).encode()).hexdigest()[:16] if trimmed_token else "",
            )
            # Most servers accept Authorization: Bearer <token>. Some tools
            # (e.g. Roboflow) require a custom header like x-api-key.
            if self._auth_header:
                # Ensure no double Bearer prefix; match server documentation
                prefix = (self._auth_value_prefix or "")
                headers[self._auth_header] = prefix + trimmed_token
            else:
                headers["Authorization"] = f"Bearer {trimmed_token}"
        return headers

    # ---- retry helper ---------------------------------------------------

    async def _with_retries(self, op_name: str, fn):
        """Run `fn` up to (1 + max_retries) times on retryable errors."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await fn()
            except (MCPTimeoutError, MCPTransportError) as exc:
                last_exc = exc
                if attempt >= self._max_retries:
                    raise
                # small backoff before retry
                await asyncio.sleep(0.5 * (attempt + 1))
                logger.info(
                    "MCP %s: retrying after %s (attempt %d/%d)",
                    op_name,
                    exc,
                    attempt + 2,
                    self._max_retries + 1,
                )
        # Shouldn't reach here, but be explicit.
        if last_exc:
            raise last_exc
        raise MCPClientError(f"{op_name} failed without raising")

    # ---- public: connect -------------------------------------------------

    async def connect(self) -> ConnectResult:
        """Open a session, run initialize + tools/list.

        Returns a `ConnectResult`. On auth failure, returns a result
        with `auth_required=True` rather than raising, so the API
        layer can map it cleanly to a 401 response.
        """
        headers = self._build_headers()
        transport = self._transport_name

        async def _do_connect() -> ConnectResult:
            try:
                async with _open_session(
                    self._url, transport, headers, self._timeout, self._auth_token
                ) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()
            except asyncio.CancelledError:
                raise MCPTimeoutError(
                    f"Connection to {self._url} was cancelled (timeout or request interrupted)"
                ) from None
            except (ExceptionGroup, BaseExceptionGroup) as exc:
                # MCP library wraps many errors in ExceptionGroup — unwrap the
                # first real cause. If it's an HTTPStatusError, let it propagate
                # to the existing HTTP handler so auth_required can be detected.
                # If it's an McpError (JSON-RPC level failure), let it propagate
                # too so the outer handler classifies it via classify_connection_error
                # instead of getting relabelled as a generic MCPClientError below.
                root = _unwrap_exception(exc)
                if isinstance(root, (httpx.HTTPStatusError, McpError)):
                    raise root from exc
                raise MCPClientError(
                    f"Unhandled exception during connect: {root}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                if _is_auth_required_response(None, exc):
                    return ConnectResult(
                        connected=False,
                        auth_required=True,
                        transport=transport,
                    )
                raise MCPTransportError(
                    f"HTTP {exc.response.status_code} during initialize"
                ) from exc
            except httpx.TimeoutException as exc:
                raise MCPTimeoutError(f"Timed out connecting to {self._url}") from exc
            except httpx.RequestError as exc:
                if _is_auth_required_response(None, exc):
                    return ConnectResult(
                        connected=False,
                        auth_required=True,
                        transport=transport,
                    )
                raise MCPTransportError(
                    f"Network error connecting to {self._url}: {exc}"
                ) from exc
            except MCPAuthRequiredError:
                return ConnectResult(
                    connected=False,
                    auth_required=True,
                    transport=transport,
                )
            # NOTE: McpError (mcp.shared.exceptions.McpError) is deliberately
            # NOT caught here. Some hosted MCP servers terminate the session
            # or return a JSON-RPC error during initialize/list_tools instead
            # of a plain HTTP 401/403 (e.g. "Session terminated"). Catching it
            # here would force a decision this layer can't make reliably; it
            # is left to propagate to connect()'s outer except block below,
            # which calls classify_connection_error() to turn it into a
            # proper ConnectResult (offering a token/OAuth retry) instead of
            # crashing the request.

            tools = [
                RemoteToolInfo(
                    name=t.name,
                    description=t.description,
                    input_schema=dict(t.inputSchema or {}),
                )
                for t in (tools_result.tools or [])
            ]
            return ConnectResult(
                connected=True,
                auth_required=False,
                transport=transport,
                tools=tools,
                server_info={
                    "name": getattr(init_result, "serverInfo", None)
                    and init_result.serverInfo.name,
                    "version": getattr(init_result, "serverInfo", None)
                    and init_result.serverInfo.version,
                    "protocol_version": getattr(init_result, "protocolVersion", None),
                },
            )

        try:
            return await self._with_retries("connect", _do_connect)
        except (
            MCPTimeoutError,
            MCPTransportError,
            MCPClientError,
            httpx.HTTPStatusError,
            RuntimeError,
            McpError,  # JSON-RPC-level failure (e.g. "Session terminated")
        ) as exc:
            _log_remote_failure("connect", self._url, exc)
            info = classify_connection_error(exc, url=self._url)
            info = await _maybe_probe_auth_rejection(info, exc, self._url)
            # Auth errors (401/403) that come through the outer handler
            # (wrapped in ExceptionGroup) should still surface as auth_required.
            # A failed network connection, timeout, or malformed endpoint is
            # not evidence that a token will help. Only surface credentials
            # for an actual auth classification.
            auth_required = info.auth_reason == AUTH_REASON_UNAUTHORIZED
            return ConnectResult(
                error=info.user_message,
                transport=transport,
                auth_required=auth_required,
                auth_reason=info.auth_reason,
                user_message=info.user_message,
                show_token_input=info.show_token_input,
                show_oauth_button=info.show_oauth_button,
            )

    # ---- public: invoke --------------------------------------------------

    async def invoke(
        self, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> InvokeResult:
        """Run tools/call on the remote server."""
        if not tool_name:
            return InvokeResult(status="error", error="tool_name is required")
        arguments = arguments or {}
        headers = self._build_headers()
        transport = self._transport_name
        started = time.monotonic()

        async def _do_invoke():
            try:
                async with _open_session(
                    self._url, transport, headers, self._timeout, self._auth_token
                ) as session:
                    await session.initialize()
                    call_result = await session.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                raise MCPTimeoutError(
                    f"Request to {self._url} was cancelled (timeout or request interrupted)"
                ) from None
            except (ExceptionGroup, BaseExceptionGroup) as exc:
                root = _unwrap_exception(exc)
                if isinstance(root, (httpx.HTTPStatusError, McpError)):
                    raise root from exc
                raise MCPClientError(
                    f"Unhandled exception during invoke: {root}"
                ) from exc
            except httpx.HTTPStatusError as exc:
                if _is_auth_required_response(None, exc):
                    return InvokeResult(
                        status="error",
                        error="Authentication required",
                        requires_auth=True,
                        duration_ms=_elapsed_ms(started),
                    )
                raise MCPTransportError(
                    f"HTTP {exc.response.status_code} during tools/call"
                ) from exc
            except httpx.TimeoutException as exc:
                raise MCPTimeoutError(
                    f"Timed out calling {tool_name} on {self._url}"
                ) from exc
            except httpx.RequestError as exc:
                if _is_auth_required_response(None, exc):
                    return InvokeResult(
                        status="error",
                        error="Authentication required",
                        requires_auth=True,
                        duration_ms=_elapsed_ms(started),
                    )
                raise MCPTransportError(
                    f"Network error calling {tool_name}: {exc}"
                ) from exc
            except MCPAuthRequiredError:
                return InvokeResult(
                    status="error",
                    error="Authentication required",
                    requires_auth=True,
                    duration_ms=_elapsed_ms(started),
                )
            # NOTE: McpError is deliberately not caught here — see the
            # matching note in _do_connect above. It propagates to invoke()'s
            # outer except block, which classifies it via
            # classify_connection_error() instead of crashing the request.

            duration = _elapsed_ms(started)

            # The MCP spec marks a tool result as an error if `isError`
            # is set, but the call itself succeeded at the protocol
            # level. Surface that distinction to the UI.
            is_error = getattr(call_result, "isError", False)

            # Check for auth-required patterns in the result content.
            # Some servers (e.g. pipeworx) return {"error":"connection_required"}
            # in structuredContent rather than via HTTP 401/403 or isError flag,
            # and stdio-style servers return application-level codes such as
            # Slack's {"ok": false, "error": "not_authed"}. The shared
            # in-band detector covers both shapes plus JSON-in-text blocks.
            auth_required = False
            auth_message: str | None = None
            structured = getattr(call_result, "structuredContent", None)
            inband = extract_inband_auth_error(structured)
            if inband:
                auth_required = True
                auth_message = inband
                if isinstance(structured, dict):
                    auth_message = structured.get("message") or inband

            # Also check content blocks for connection_required / auth text (fallback)
            if not auth_required:
                extracted = _extract_error_message(call_result)
                if extracted and (
                    "connection_required" in extracted.lower()
                    or extract_inband_auth_error(extracted)
                ):
                    auth_required = True
                    auth_message = auth_message or extract_inband_auth_error(extracted) or extracted

            # Detect auth-related errors from text content.
            # Some servers (e.g. PennyOCR) return the error as a plain text
            # block rather than HTTP 401 — check for common auth patterns.
            if is_error and not auth_required:
                extracted = _extract_error_message(call_result)
                if extracted and _is_auth_error_text(extracted):
                    auth_required = True
                    auth_message = auth_message or extracted

            if is_error and not auth_required:
                return InvokeResult(
                    status="error",
                    error=_extract_error_message(call_result),
                    result=_coerce_content(call_result),
                    duration_ms=duration,
                    requires_auth=False,
                )

            if auth_required:
                # Either isError+connection_required, or structuredContent carries
                # connection_required, or text content describes an auth error.
                # Treat as an auth-required response so the UI shows the token input.
                return InvokeResult(
                    status="error",
                    error=auth_message or "Authentication required",
                    requires_auth=True,
                    auth_reason=AUTH_REASON_UNAUTHORIZED,
                    user_message=auth_message
                    or "This tool requires credentials. "
                    "Provide your API key or bearer token to continue.",
                    show_token_input=True,
                    show_oauth_button=True,
                    duration_ms=duration,
                )

            return InvokeResult(
                status="success",
                result=_coerce_content(call_result),
                duration_ms=duration,
                requires_auth=False,
            )

        try:
            return await self._with_retries("invoke", _do_invoke)
        except (
            MCPTimeoutError,
            MCPTransportError,
            MCPProtocolError,
            MCPClientError,
            McpError,  # JSON-RPC-level failure (e.g. "Session terminated")
        ) as exc:
            _log_remote_failure("invoke", self._url, exc)
            info = classify_connection_error(exc, url=self._url)
            info = await _maybe_probe_auth_rejection(info, exc, self._url)
            return InvokeResult(
                status="error",
                error=info.user_message,
                duration_ms=_elapsed_ms(started),
                requires_auth=info.auth_reason == AUTH_REASON_UNAUTHORIZED,
                auth_reason=info.auth_reason,
                user_message=info.user_message,
                show_token_input=info.show_token_input,
                show_oauth_button=info.show_oauth_button,
            )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _coerce_content(call_result: Any) -> Any:
    """Convert an MCP CallToolResult to a JSON-serializable payload.

    The mcp library returns content as a list of typed blocks
    (TextContent, ImageContent, etc.). We surface both the structured
    content (if present) and the textual rendering for the UI.
    """
    if call_result is None:
        return None
    # structuredContent is the preferred JSON payload
    structured = getattr(call_result, "structuredContent", None)
    if structured is not None:
        return structured
    content = getattr(call_result, "content", None) or []
    out: list[Any] = []
    for block in content:
        # Pydantic models support model_dump; dicts pass through
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            out.append(block)
        else:
            out.append(str(block))
    return out


def _extract_error_message(call_result: Any) -> str:
    content = getattr(call_result, "content", None) or []
    chunks: list[str] = []
    for block in content:
        text = getattr(block, "text", None) or getattr(block, "data", None)
        if text:
            chunks.append(str(text))
    return " | ".join(chunks) if chunks else "Tool returned an error"


# Auth-related keywords in error text that indicate a missing/invalid API key
_AUTH_ERROR_KEYWORDS = frozenset(
    (
        "api key",
        "api-key",
        "apikey",
        "auth",
        "authorization",
        "bearer",
        "credential",
        "invalid token",
        "missing token",
        "missing api",
        "no api",
        "unauthorized",
        "invalid api",
    )
)


def _is_auth_error_text(text: str) -> bool:
    """Return True if `text` describes an auth/credentials problem.

    Detects common patterns from MCP servers that return auth errors
    as plain text responses rather than HTTP 401/403.
    """
    lowered = text.lower()
    return (
        any(kw in lowered for kw in _AUTH_ERROR_KEYWORDS)
        # e.g. \"Add headers: {\\"Authorization\\": \\"Bearer ...\\"}\"
        or (
            "add headers" in lowered
            and ("bearer" in lowered or "authorization" in lowered)
        )
        # e.g. \"Keys are free at https://...\"
        or ("keys are free" in lowered or "get your api key" in lowered)
        or ("sign up" in lowered and "api" in lowered)
    )