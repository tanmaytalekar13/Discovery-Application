import httpx
import pytest

from app.discovery.web_search.client import (
    BraveWebSearchProvider,
    RawSearchResult,
    WebSearchAPIError,
    WebSearchCandidate,
    WebSearchDiscoveryAdapter,
    WebSearchProvider,
    WebSearchRateLimitError,
)
from app.models import DiscoverySource, ItemType, SourceType


# ============================================================
# Classification (pure text, no network)
# ============================================================


def test_classifies_mcp_result():
    result = WebSearchDiscoveryAdapter.classify_search_result(
        title="weather-mcp: An MCP server for weather data",
        url="https://github.com/example/weather-mcp",
        snippet="Implements tools/list and tools/call for weather lookups.",
    )

    assert result is not None
    item_type, evidence = result
    assert item_type is ItemType.TOOL
    assert evidence


def test_classifies_a2a_result():
    result = WebSearchDiscoveryAdapter.classify_search_result(
        title="research-agent: An A2A agent with an Agent Card",
        url="https://agents.example.com/research/.well-known/agent-card.json",
        snippet="Supports message/send and the A2A protocol.",
    )

    assert result is not None
    item_type, evidence = result
    assert item_type is ItemType.AGENT
    assert evidence


def test_rejects_weak_false_positive():
    result = WebSearchDiscoveryAdapter.classify_search_result(
        title="What is an AI agent? A beginner's guide",
        url="https://blog.example.com/what-is-an-ai-agent",
        snippet="This article explains agents and MCP in general terms.",
    )

    assert result is None


def test_classifies_both_signals_as_tool_with_combined_evidence():
    result = WebSearchDiscoveryAdapter.classify_search_result(
        title="toolkit: MCP server that also exposes an Agent Card",
        url="https://github.com/example/toolkit",
        snippet="Implements tools/call and message/send, with Agent Card support.",
    )

    assert result is not None
    item_type, evidence = result
    assert item_type is ItemType.TOOL
    assert any("also contains A2A evidence" in item for item in evidence)


# ============================================================
# WebSearchDiscoveryAdapter using a fake provider
# ============================================================


class _FakeProvider(WebSearchProvider):
    def __init__(self, results: list[RawSearchResult]) -> None:
        self._results = results
        self.last_query: str | None = None

    async def search(self, query, max_results=20):
        self.last_query = query
        return self._results[:max_results]


@pytest.mark.asyncio
async def test_discover_returns_classified_candidates_only():
    provider = _FakeProvider(
        [
            RawSearchResult(
                title="weather-mcp: An MCP server",
                url="https://github.com/example/weather-mcp",
                snippet="Implements tools/call for weather.",
                raw_result={"title": "weather-mcp"},
            ),
            RawSearchResult(
                title="Totally unrelated blog post",
                url="https://blog.example.com/unrelated",
                snippet="Nothing to do with agents or tools here.",
                raw_result={},
            ),
        ]
    )

    adapter = WebSearchDiscoveryAdapter(provider)
    results = await adapter.discover("weather mcp", max_results=5)

    assert len(results) == 1
    assert isinstance(results[0], WebSearchCandidate)
    assert results[0].item_type is ItemType.TOOL
    assert results[0].source.type is SourceType.WEB_SEARCH
    assert results[0].source.id == "https://github.com/example/weather-mcp"


@pytest.mark.asyncio
async def test_discover_mcp_uses_default_query():
    provider = _FakeProvider([])
    adapter = WebSearchDiscoveryAdapter(provider)

    await adapter.discover_mcp(max_results=5)

    assert "MCP" in provider.last_query


@pytest.mark.asyncio
async def test_discover_a2a_uses_default_query():
    provider = _FakeProvider([])
    adapter = WebSearchDiscoveryAdapter(provider)

    await adapter.discover_a2a(max_results=5)

    assert "A2A" in provider.last_query


@pytest.mark.asyncio
async def test_discover_rejects_empty_query():
    adapter = WebSearchDiscoveryAdapter(_FakeProvider([]))

    with pytest.raises(ValueError):
        await adapter.discover("   ")


def test_candidate_provenance_is_web_search():
    candidate = WebSearchCandidate(
        title="weather-mcp",
        url="https://github.com/example/weather-mcp",
        snippet="",
        item_type=ItemType.TOOL,
        evidence=("evidence",),
        source=DiscoverySource(
            type=SourceType.WEB_SEARCH,
            id="https://github.com/example/weather-mcp",
            url="https://github.com/example/weather-mcp",
        ),
        raw_result={},
    )

    assert candidate.source.type is SourceType.WEB_SEARCH


# ============================================================
# BraveWebSearchProvider (real documented contract, mocked transport)
# ============================================================


def test_empty_api_key_is_rejected():
    with pytest.raises(ValueError):
        BraveWebSearchProvider(api_key="  ")


@pytest.mark.asyncio
async def test_brave_provider_parses_results():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.headers["X-Subscription-Token"] == "test-key"
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "weather-mcp",
                                "url": "https://github.com/example/weather-mcp",
                                "description": "An MCP server for weather.",
                            },
                            {"title": "no url here"},
                        ]
                    }
                },
            )

    client = httpx.AsyncClient(
        transport=MockTransport(), base_url="https://api.search.brave.test"
    )

    async with BraveWebSearchProvider(
        api_key="test-key",
        base_url="https://api.search.brave.test",
        httpx_client=client,
    ) as provider:
        results = await provider.search("weather mcp", max_results=5)

    assert len(results) == 1
    assert results[0].title == "weather-mcp"
    assert results[0].url == "https://github.com/example/weather-mcp"


@pytest.mark.asyncio
async def test_brave_provider_rate_limit_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                429, headers={"Retry-After": "10"}, json={"error": "rate limited"}
            )

    client = httpx.AsyncClient(
        transport=MockTransport(), base_url="https://api.search.brave.test"
    )

    async with BraveWebSearchProvider(
        api_key="test-key",
        base_url="https://api.search.brave.test",
        httpx_client=client,
    ) as provider:
        with pytest.raises(WebSearchRateLimitError):
            await provider.search("weather mcp")


@pytest.mark.asyncio
async def test_brave_provider_api_error_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(500, json={"error": "unavailable"})

    client = httpx.AsyncClient(
        transport=MockTransport(), base_url="https://api.search.brave.test"
    )

    async with BraveWebSearchProvider(
        api_key="test-key",
        base_url="https://api.search.brave.test",
        httpx_client=client,
    ) as provider:
        with pytest.raises(WebSearchAPIError):
            await provider.search("weather mcp")


@pytest.mark.asyncio
async def test_brave_provider_invalid_shape_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"web": {"results": "not-a-list"}})

    client = httpx.AsyncClient(
        transport=MockTransport(), base_url="https://api.search.brave.test"
    )

    async with BraveWebSearchProvider(
        api_key="test-key",
        base_url="https://api.search.brave.test",
        httpx_client=client,
    ) as provider:
        with pytest.raises(WebSearchAPIError):
            await provider.search("weather mcp")
