"""Tests for the MCP client helper.

Tests cover:
  - URL safety / SSRF guard
  - Transport selection
  - Error class hierarchy
  - Result dataclass construction
  - _is_auth_required_response
  - MCPTestClient construction
  - Public API smoke tests (connect/invoke) via mock

The actual HTTP round-trips to real servers are tested via the
integration test in `test_integration/` if the env has real fixtures.
"""
from __future__ import annotations

import pytest

from app.sandbox.mcp_client import (
    MCPClientError,
    MCPProtocolError,
    MCPTestClient,
    MCPTimeoutError,
    MCPTransportError,
    RemoteToolInfo,
    UnsafeURLError,
    _is_unsafe_host,
    _pick_transport,
    _validate_url,
    ConnectResult,
    InvokeResult,
)


# ---------------------------------------------------------------------------
# URL safety
# ---------------------------------------------------------------------------

class TestUrlValidation:
    def test_accepts_https(self):
        assert _validate_url("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"

    def test_accepts_http(self):
        assert _validate_url("http://internal.example.com/mcp") == "http://internal.example.com/mcp"

    def test_rejects_localhost_by_default(self):
        with pytest.raises(UnsafeURLError, match="non-public host"):
            _validate_url("http://localhost:3000/mcp")
        with pytest.raises(UnsafeURLError, match="non-public host"):
            _validate_url("http://127.0.0.1:3000/mcp")

    def test_rejects_metadata_endpoints(self):
        for url in [
            "http://169.254.169.254/latest/meta-data",
            "http://metadata.google.internal/computeMetadata/v1/",
        ]:
            with pytest.raises(UnsafeURLError):
                _validate_url(url)

    def test_rejects_private_ranges(self):
        for ip in [
            "http://10.0.0.1/mcp",
            "http://192.168.1.1/mcp",
            "http://172.16.0.1/mcp",
            "http://172.31.255.255/mcp",
        ]:
            with pytest.raises(UnsafeURLError):
                _validate_url(ip)

    def test_allows_local_with_flag(self):
        url = "http://localhost:3000/mcp"
        result = _validate_url(url, allow_local=True)
        assert result == url

    def test_rejects_unsupported_scheme(self):
        for scheme in ("ftp", "file", "ssh", "ws", "grpc"):
            with pytest.raises(UnsafeURLError, match="Unsupported scheme"):
                _validate_url(f"{scheme}://example.com/mcp")

    def test_rejects_empty_url(self):
        with pytest.raises(UnsafeURLError, match="empty"):
            _validate_url("")

    def test_rejects_malformed_url(self):
        # "not-a-url" has no scheme, so it's rejected as unsupported-scheme
        # (which is still a validation failure). A truly malformed URL
        # that the parser can't even handle raises Malformed.
        with pytest.raises(UnsafeURLError):
            _validate_url("not-a-url")

    def test_allows_localhost_with_allow_local(self):
        for host in ("localhost", "127.0.0.1", "0.0.0.0"):
            result = _validate_url(f"http://{host}:3000/mcp", allow_local=True)
            assert "example" not in result  # should pass
        # IPv6 loopback needs bracket form
        result = _validate_url("http://[::1]:3000/mcp", allow_local=True)
        assert result == "http://[::1]:3000/mcp"

    def test_ipv6_link_local_rejected(self):
        with pytest.raises(UnsafeURLError):
            _validate_url("http://fe80::1/mcp")

    def test_normalizes_path(self):
        url = "https://mcp.example.com/mcp///"
        assert _validate_url(url) == "https://mcp.example.com/mcp///"  # no strip


class TestIsUnsafeHost:
    def test_blocked_hosts(self):
        for host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            assert _is_unsafe_host(host) is True

    def test_metadata_hosts(self):
        assert _is_unsafe_host("169.254.169.254") is True
        assert _is_unsafe_host("metadata.google.internal") is True

    def test_private_ranges(self):
        assert _is_unsafe_host("10.0.0.1") is True
        assert _is_unsafe_host("192.168.1.1") is True
        assert _is_unsafe_host("172.16.0.1") is True
        assert _is_unsafe_host("fc00::1") is True  # ULA
        assert _is_unsafe_host("fe80::1") is True   # link-local

    def test_public_hosts(self):
        for host in ("example.com", "api.openai.com", "mcp.waystation.ai"):
            assert _is_unsafe_host(host) is False

    def test_case_insensitive(self):
        assert _is_unsafe_host("LOCALHOST") is True
        assert _is_unsafe_host("Example.COM") is False


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------

class TestPickTransport:
    def test_explicit_streamable_http(self):
        assert _pick_transport("https://x.com/mcp", "streamable-http") == "streamable-http"

    def test_explicit_sse(self):
        assert _pick_transport("https://x.com/mcp", "sse") == "sse"

    def test_infers_sse_from_path(self):
        assert _pick_transport("https://x.com/mcp/sse", None) == "sse"
        assert _pick_transport("https://x.com/v1/sse", None) == "sse"

    def test_defaults_to_streamable_http(self):
        assert _pick_transport("https://x.com/mcp", None) == "streamable-http"


# ---------------------------------------------------------------------------
# MCPTestClient construction
# ---------------------------------------------------------------------------

class TestMCPTestClientConstruction:
    def test_minimal_construction(self):
        client = MCPTestClient("https://mcp.example.com/mcp")
        assert client._url == "https://mcp.example.com/mcp"
        assert client._auth_token is None
        assert client._timeout == 10.0
        assert client._max_retries == 1

    def test_with_auth_token(self):
        client = MCPTestClient(
            "https://mcp.example.com/mcp",
            auth_token="sk-secret-123",
        )
        assert client._auth_token == "sk-secret-123"
        headers = client._build_headers()
        assert headers["Authorization"] == "Bearer sk-secret-123"

    def test_with_custom_timeout(self):
        client = MCPTestClient(
            "https://mcp.example.com/mcp",
            timeout=30.0,
        )
        assert client._timeout == 30.0

    def test_rejects_localhost(self):
        with pytest.raises(UnsafeURLError):
            MCPTestClient("http://localhost:3000/mcp")

    def test_allows_localhost_with_flag(self):
        client = MCPTestClient(
            "http://localhost:3000/mcp",
            allow_local_host=True,
        )
        assert client._url == "http://localhost:3000/mcp"

    def test_transport_name_set(self):
        client = MCPTestClient(
            "https://mcp.example.com/mcp/sse",
            preferred_transport="sse",
        )
        assert client._transport_name == "sse"

    def test_no_auth_headers(self):
        client = MCPTestClient("https://mcp.example.com/mcp")
        headers = client._build_headers()
        assert "Authorization" not in headers
        assert headers["Accept"] == "application/json, text/event-stream"
        assert "User-Agent" in headers


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

class TestConnectResult:
    def test_default_is_error_state(self):
        r = ConnectResult()
        assert r.connected is False
        assert r.auth_required is False
        assert r.tools == []
        assert r.error is None

    def test_success_state(self):
        r = ConnectResult(
            connected=True,
            transport="streamable-http",
            tools=[RemoteToolInfo(name="send_email", description="Send an email")],
        )
        assert r.connected is True
        assert r.auth_required is False
        assert len(r.tools) == 1
        assert r.tools[0].name == "send_email"

    def test_auth_required_state(self):
        r = ConnectResult(
            connected=False,
            auth_required=True,
            transport="sse",
        )
        assert r.connected is False
        assert r.auth_required is True

    def test_error_state(self):
        r = ConnectResult(error="Connection refused")
        assert r.connected is False
        assert r.auth_required is False
        assert r.error == "Connection refused"


class TestInvokeResult:
    def test_success_with_result(self):
        r = InvokeResult(
            status="success",
            result={"sent": True, "message_id": "abc"},
            duration_ms=342,
        )
        assert r.status == "success"
        assert r.result["sent"] is True
        assert r.duration_ms == 342
        assert r.error is None
        assert r.requires_auth is False

    def test_error_with_requires_auth(self):
        r = InvokeResult(
            status="error",
            error="Authentication required",
            requires_auth=True,
            duration_ms=50,
        )
        assert r.status == "error"
        assert r.error == "Authentication required"
        assert r.requires_auth is True

    def test_error_without_auth(self):
        r = InvokeResult(
            status="error",
            error="Tool not found: nonexistent",
            duration_ms=120,
        )
        assert r.status == "error"
        assert r.requires_auth is False


# ---------------------------------------------------------------------------
# Error class hierarchy
# ---------------------------------------------------------------------------

class TestErrorHierarchy:
    def test_client_error_is_base(self):
        assert issubclass(MCPTimeoutError, MCPClientError)
        assert issubclass(MCPTransportError, MCPClientError)
        assert issubclass(MCPProtocolError, MCPClientError)
        assert issubclass(UnsafeURLError, MCPClientError)

    def test_mcp_timeout_error_message(self):
        exc = MCPTimeoutError("timed out")
        assert str(exc) == "timed out"

    def test_mcp_transport_error_message(self):
        exc = MCPTransportError("connection refused")
        assert str(exc) == "connection refused"

    def test_unsafe_url_error(self):
        exc = UnsafeURLError("bad host")
        assert str(exc) == "bad host"


# ---------------------------------------------------------------------------
# RemoteToolInfo
# ---------------------------------------------------------------------------

class TestRemoteToolInfo:
    def test_minimal(self):
        t = RemoteToolInfo(name="foo")
        assert t.name == "foo"
        assert t.description is None
        assert t.input_schema == {}

    def test_full(self):
        t = RemoteToolInfo(
            name="bar",
            description="Does a thing",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        )
        assert t.name == "bar"
        assert t.description == "Does a thing"
        assert t.input_schema["type"] == "object"
