import httpx
import pytest

from app.discovery.mcp_registry.client import (
    MCPRegistryAPIError,
    MCPRegistryCandidate,
    MCPRegistryClient,
    MCPRegistryRateLimitError,
)
from app.models import ItemType, SourceType


def _server_entry(
    *,
    name="io.github.example/weather",
    version="1.2.3",
    status="active",
):
    return {
        "server": {
            "name": name,
            "title": "Weather",
            "description": "Weather MCP server",
            "version": version,
            "status": status,
            "repository": {
                "url": "https://github.com/example/weather",
                "source": "github",
            },
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "weather-mcp",
                    "version": version,
                    "transport": {"type": "stdio"},
                }
            ],
            "remotes": [
                {
                    "type": "streamable-http",
                    "url": "https://mcp.example.com/mcp",
                }
            ],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active",
                "isLatest": True,
            }
        },
    }


def test_parse_registry_candidate():
    candidate = MCPRegistryClient._parse_candidate(
        _server_entry()
    )

    assert candidate is not None
    assert candidate.server_name == "io.github.example/weather"
    assert candidate.version == "1.2.3"
    assert candidate.item_type is ItemType.TOOL
    assert candidate.protocol == "mcp"
    assert candidate.source.type is SourceType.MCP_REGISTRY
    assert candidate.source.id == "io.github.example/weather"
    assert candidate.validation_required is True
    assert len(candidate.packages) == 1
    assert len(candidate.remotes) == 1


def test_deleted_server_is_not_candidate():
    assert (
        MCPRegistryClient._parse_candidate(
            _server_entry(status="deleted")
        )
        is None
    )


def test_invalid_registry_entry_is_rejected():
    assert (
        MCPRegistryClient._parse_candidate(
            {"server": {"description": "missing name/version"}}
        )
        is None
    )


@pytest.mark.asyncio
async def test_search_handles_cursor_pagination():
    calls = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url)

            if request.url.path == "/v0.1/servers":
                cursor = request.url.params.get("cursor")

                if not cursor:
                    return httpx.Response(
                        200,
                        json={
                            "servers": [_server_entry()],
                            "metadata": {
                                "count": 1,
                                "nextCursor": "next-page",
                            },
                        },
                    )

                return httpx.Response(
                    200,
                    json={
                        "servers": [
                            _server_entry(
                                name="io.github.example/search"
                            )
                        ],
                        "metadata": {
                            "count": 1,
                            "nextCursor": None,
                        },
                    },
                )

            return httpx.Response(404, json={})

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with MCPRegistryClient(
        httpx_client=client,
        page_size=10,
    ) as registry:
        results = await registry.search(
            query="weather",
            max_results=10,
        )

    assert len(results) == 2
    assert results[0].server_name == "io.github.example/weather"
    assert results[1].server_name == "io.github.example/search"
    assert len(calls) == 2
    assert calls[1].params["cursor"] == "next-page"


@pytest.mark.asyncio
async def test_search_stops_at_max_results():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200,
                json={
                    "servers": [
                        _server_entry(),
                        _server_entry(
                            name="io.github.example/second"
                        ),
                    ],
                    "metadata": {
                        "count": 2,
                        "nextCursor": "unused",
                    },
                },
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with MCPRegistryClient(
        httpx_client=client
    ) as registry:
        results = await registry.search(
            query="weather",
            max_results=1,
        )

    assert len(results) == 1


@pytest.mark.asyncio
async def test_get_latest_uses_encoded_server_name():
    captured = {}

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            captured["path"] = request.url.path
            return httpx.Response(
                200,
                json=_server_entry(),
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with MCPRegistryClient(
        httpx_client=client
    ) as registry:
        result = await registry.get_latest(
            "io.github.example/weather"
        )

    assert result.server_name == "io.github.example/weather"
    assert (
        captured["path"]
        == "/v0.1/servers/io.github.example/weather/versions/latest"
    )


@pytest.mark.asyncio
async def test_rate_limit_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                429,
                headers={"Retry-After": "30"},
                json={"error": "rate limited"},
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with MCPRegistryClient(
        httpx_client=client
    ) as registry:
        with pytest.raises(MCPRegistryRateLimitError):
            await registry.search("weather", max_results=1)


@pytest.mark.asyncio
async def test_api_failure_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                503,
                json={"error": "unavailable"},
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with MCPRegistryClient(
        httpx_client=client
    ) as registry:
        with pytest.raises(MCPRegistryAPIError):
            await registry.search("weather", max_results=1)
