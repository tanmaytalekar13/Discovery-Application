import httpx
import pytest

from app.discovery.web_extraction import (
    RobotsChecker,
    SSRFGuard,
    SSRFViolation,
    WebExtractionAdapter,
    WebExtractionBlockedError,
    WebExtractionCandidate,
    WebExtractionResponseError,
)
from app.models import ItemType, SourceType

# ============================================================
# SSRFGuard (Section 23 - no network, injected fake resolver)
# ============================================================


def _resolver(mapping: dict[str, list[str]]):
    def resolve(host: str) -> list[str]:
        return mapping[host]

    return resolve


@pytest.mark.asyncio
async def test_ssrf_guard_allows_public_address():
    guard = SSRFGuard(resolver=_resolver({"example.com": ["93.184.216.34"]}))

    host = await guard.validate("https://example.com/page")

    assert host == "example.com"


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_unsupported_scheme():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("ftp://example.com/file")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_missing_hostname():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("file:///etc/passwd")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_localhost_hostname():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("http://localhost/admin")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_internal_suffix_hostname():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("http://service.internal/admin")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_loopback_ip_literal():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("http://127.0.0.1:8080/")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_private_ip_literal():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("http://10.0.0.5/")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_cloud_metadata_address():
    guard = SSRFGuard()

    with pytest.raises(SSRFViolation):
        await guard.validate("http://169.254.169.254/latest/meta-data/")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_dns_rebinding_to_private_ip():
    # The hostname itself looks public, but resolves to a private
    # address - this must be blocked, not just IP literals.
    guard = SSRFGuard(resolver=_resolver({"evil.example.com": ["10.1.2.3"]}))

    with pytest.raises(SSRFViolation):
        await guard.validate("http://evil.example.com/")


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_when_dns_fails():
    def broken_resolver(host: str) -> list[str]:
        raise OSError("no such host")

    guard = SSRFGuard(resolver=broken_resolver)

    with pytest.raises(SSRFViolation):
        await guard.validate("http://nonexistent.example.invalid/")


# ============================================================
# RobotsChecker
# ============================================================


@pytest.mark.asyncio
async def test_robots_checker_allows_when_no_robots_txt():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(404)

    client = httpx.AsyncClient(transport=MockTransport())
    checker = RobotsChecker(client)

    assert await checker.is_allowed("https://example.com/page") is True


@pytest.mark.asyncio
async def test_robots_checker_respects_disallow():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")

    client = httpx.AsyncClient(transport=MockTransport())
    checker = RobotsChecker(client)

    assert await checker.is_allowed("https://example.com/private/data") is False
    assert await checker.is_allowed("https://example.com/public/data") is True


@pytest.mark.asyncio
async def test_robots_checker_fails_open_on_transport_error():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("boom", request=request)

    client = httpx.AsyncClient(transport=MockTransport())
    checker = RobotsChecker(client)

    assert await checker.is_allowed("https://example.com/page") is True


# ============================================================
# WebExtractionAdapter.classify_page (pure text, no network)
# ============================================================


def test_classifies_mcp_page():
    result = WebExtractionAdapter.classify_page(
        title="weather-mcp docs",
        url="https://example.com/weather-mcp",
        text="This is an MCP server implementing tools/list and tools/call.",
    )

    assert result is not None
    item_type, evidence = result
    assert item_type is ItemType.TOOL
    assert evidence


def test_classifies_a2a_page():
    result = WebExtractionAdapter.classify_page(
        title="research-agent",
        url="https://example.com/.well-known/agent-card.json",
        text="This agent exposes an Agent Card and supports message/send.",
    )

    assert result is not None
    item_type, evidence = result
    assert item_type is ItemType.AGENT
    assert evidence


def test_rejects_weak_false_positive_page():
    result = WebExtractionAdapter.classify_page(
        title="What is an AI agent?",
        url="https://blog.example.com/what-is-an-ai-agent",
        text="A general explainer about agents and tools with no protocol markers.",
    )

    assert result is None


# ============================================================
# WebExtractionAdapter.extract (mocked transport)
# ============================================================


def _adapter(transport: httpx.AsyncBaseTransport, **kwargs) -> WebExtractionAdapter:
    client = httpx.AsyncClient(transport=transport)
    guard = SSRFGuard(resolver=_resolver({"example.com": ["93.184.216.34"]}))
    return WebExtractionAdapter(httpx_client=client, ssrf_guard=guard, **kwargs)


@pytest.mark.asyncio
async def test_extract_returns_classified_candidate():
    html = (
        "<html><head><title>weather-mcp</title></head>"
        "<body><p>An MCP server exposing tools/call.</p>"
        "<script>alert('should not affect classification')</script>"
        "</body></html>"
    )

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    adapter = _adapter(MockTransport())
    candidate = await adapter.extract("https://example.com/weather-mcp")

    assert isinstance(candidate, WebExtractionCandidate)
    assert candidate.item_type is ItemType.TOOL
    assert candidate.source.type is SourceType.WEB_PAGE
    assert candidate.title == "weather-mcp"
    assert "alert(" not in candidate.text_excerpt


@pytest.mark.asyncio
async def test_extract_returns_none_for_non_candidate_page():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><body>Just a regular blog post.</body></html>",
            )

    adapter = _adapter(MockTransport())
    candidate = await adapter.extract("https://example.com/blog")

    assert candidate is None


@pytest.mark.asyncio
async def test_extract_blocked_by_robots_txt():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /private/\n")
            return httpx.Response(200, text="should not be reached")

    adapter = _adapter(MockTransport())

    with pytest.raises(WebExtractionBlockedError):
        await adapter.extract("https://example.com/private/secret")


@pytest.mark.asyncio
async def test_extract_blocked_by_ssrf_guard():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, text="should not be reached")

    client = httpx.AsyncClient(transport=MockTransport())
    guard = SSRFGuard(resolver=_resolver({"internal.example.com": ["10.0.0.1"]}))
    adapter = WebExtractionAdapter(httpx_client=client, ssrf_guard=guard)

    with pytest.raises(WebExtractionBlockedError):
        await adapter.extract("http://internal.example.com/")


@pytest.mark.asyncio
async def test_extract_rejects_disallowed_content_type():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"\x00\x01\x02",
            )

    adapter = _adapter(MockTransport())

    with pytest.raises(WebExtractionResponseError):
        await adapter.extract("https://example.com/binary")


@pytest.mark.asyncio
async def test_extract_rejects_oversized_response():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html>" + b"a" * 100,
            )

    adapter = _adapter(MockTransport(), max_response_bytes=50)

    with pytest.raises(WebExtractionResponseError):
        await adapter.extract("https://example.com/huge")


@pytest.mark.asyncio
async def test_extract_rejects_too_many_redirects():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(302, headers={"location": "https://example.com/next"})

    adapter = _adapter(MockTransport(), max_redirects=2)

    with pytest.raises(WebExtractionResponseError):
        await adapter.extract("https://example.com/start")


@pytest.mark.asyncio
async def test_extract_re_validates_redirect_target_against_ssrf():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if request.url.host == "example.com":
                return httpx.Response(
                    302,
                    headers={"location": "http://internal.example.com/secret"},
                )
            return httpx.Response(200, text="should not be reached")

    client = httpx.AsyncClient(transport=MockTransport())
    guard = SSRFGuard(
        resolver=_resolver(
            {
                "example.com": ["93.184.216.34"],
                "internal.example.com": ["10.0.0.1"],
            }
        )
    )
    adapter = WebExtractionAdapter(httpx_client=client, ssrf_guard=guard)

    with pytest.raises(WebExtractionBlockedError):
        await adapter.extract("https://example.com/start")


@pytest.mark.asyncio
async def test_extract_http_error_is_wrapped():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            raise httpx.ConnectTimeout("timed out", request=request)

    adapter = _adapter(MockTransport())

    with pytest.raises(WebExtractionResponseError):
        await adapter.extract("https://example.com/slow")


@pytest.mark.asyncio
async def test_extract_many_isolates_failures():
    html = (
        "<html><head><title>weather-mcp</title></head>"
        "<body>An MCP server exposing tools/call.</body></html>"
    )

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            if "good" in str(request.url):
                return httpx.Response(
                    200, headers={"content-type": "text/html"}, text=html
                )
            raise httpx.ConnectError("boom", request=request)

    adapter = _adapter(MockTransport())

    results = await adapter.extract_many(
        [
            "https://example.com/good-tool",
            "https://example.com/broken-page",
        ]
    )

    assert len(results) == 1
    assert results[0].url == "https://example.com/good-tool"
