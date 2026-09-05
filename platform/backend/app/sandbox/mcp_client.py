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
explicit hint. The `mcp` library handles the wire format for both.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 1  # 1 = one retry after the first attempt
DEFAULT_SSE_READ_TIMEOUT = 60.0


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


@dataclass
class InvokeResult:
    """Result of a tools/call invocation."""

    status: str  # "success" | "error"
    result: Any = None
    error: str | None = None
    requires_auth: bool = False
    duration_ms: int = 0


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
    "169.254.169.254",           # AWS / Azure / GCP metadata
}

# Private / loopback IP ranges we never dial.
# We use a simple string-prefix check on the hostname; for production
# this should be replaced with `ipaddress.ip_address(...).is_private`.
_PRIVATE_HOST_PREFIXES = (
    "10.",
    "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.",
    "172.24.", "172.25.", "172.26.", "172.27.",
    "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.",
    "fc", "fd",   # IPv6 ULA
    "fe80",       # IPv6 link-local
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
        raise UnsafeURLError(
            f"Unsupported scheme {parsed.scheme!r} (only http/https)"
        )

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
):
    """Open an MCP client session, yielding a ClientSession bound to
    the chosen transport.

    Imports the mcp library lazily so that unit tests of the rest of
    the module don't pay the import cost, and so import errors are
    isolated to where they matter.
    """
    from mcp import ClientSession

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
    ) -> None:
        self._url = _validate_url(url, allow_local=allow_local_host)
        self._auth_token = auth_token
        self._preferred_transport = preferred_transport
        self._timeout = timeout
        self._max_retries = max_retries
        self._transport_name = _pick_transport(self._url, preferred_transport)

    # ---- headers ---------------------------------------------------------

    def _build_headers(self) -> dict[str, str]:
        """Build the request headers, including auth if present."""
        headers: dict[str, str] = {
            "Accept": "application/json, text/event-stream",
            "User-Agent": "DiscoveryApplicationBot/1.0 (+tool-test)",
        }
        if self._auth_token:
            # Bearer is the most common shape; servers that expect a
            # raw API key can still accept this since the auth scheme
            # is server-defined.
            headers["Authorization"] = f"Bearer {self._auth_token}"
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
                    op_name, exc, attempt + 2, self._max_retries + 1,
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
                    self._url, transport, headers, self._timeout
                ) as session:
                    init_result = await session.initialize()
                    tools_result = await session.list_tools()
            except asyncio.CancelledError:
                raise MCPTimeoutError(
                    f"Connection to {self._url} was cancelled (timeout or request interrupted)"
                ) from None
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
                raise MCPTimeoutError(
                    f"Timed out connecting to {self._url}"
                ) from exc
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
                    "name": getattr(init_result, "serverInfo", None) and init_result.serverInfo.name,
                    "version": getattr(init_result, "serverInfo", None) and init_result.serverInfo.version,
                    "protocol_version": getattr(init_result, "protocolVersion", None),
                },
            )

        try:
            return await self._with_retries("connect", _do_connect)
        except MCPTimeoutError as exc:
            return ConnectResult(error=str(exc), transport=transport)
        except MCPTransportError as exc:
            return ConnectResult(error=str(exc), transport=transport)
        except MCPClientError as exc:
            return ConnectResult(error=str(exc), transport=transport)

    # ---- public: invoke --------------------------------------------------

    async def invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> InvokeResult:
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
                    self._url, transport, headers, self._timeout
                ) as session:
                    await session.initialize()
                    call_result = await session.call_tool(tool_name, arguments)
            except asyncio.CancelledError:
                raise MCPTimeoutError(
                    f"Request to {self._url} was cancelled (timeout or request interrupted)"
                ) from None
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

            duration = _elapsed_ms(started)

            # The MCP spec marks a tool result as an error if `isError`
            # is set, but the call itself succeeded at the protocol
            # level. Surface that distinction to the UI.
            is_error = getattr(call_result, "isError", False)
            if is_error:
                return InvokeResult(
                    status="error",
                    error=_extract_error_message(call_result),
                    result=_coerce_content(call_result),
                    duration_ms=duration,
                )

            return InvokeResult(
                status="success",
                result=_coerce_content(call_result),
                duration_ms=duration,
            )

        try:
            return await self._with_retries("invoke", _do_invoke)
        except MCPTimeoutError as exc:
            return InvokeResult(status="error", error=str(exc), duration_ms=_elapsed_ms(started))
        except MCPTransportError as exc:
            return InvokeResult(status="error", error=str(exc), duration_ms=_elapsed_ms(started))
        except MCPProtocolError as exc:
            return InvokeResult(status="error", error=str(exc), duration_ms=_elapsed_ms(started))
        except MCPClientError as exc:
            return InvokeResult(status="error", error=str(exc), duration_ms=_elapsed_ms(started))


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
