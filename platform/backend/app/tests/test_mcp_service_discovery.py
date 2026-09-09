import httpx
import pytest

from app.discovery.mcp_registry.service_adapter import (
    MCPSearchAPIError,
    MCPSearchError,
    MCPSearchRateLimitError,
    discover_services,
)


class MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    async def handle_async_request(self, request):
        self.calls.append(request)
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(payload, status=200, headers=None):
    return httpx.Response(
        status,
        json=payload,
        headers=headers or {},
    )


def _registry_entry(
    server_name: str,
    *,
    title: str = "MCP server",
    description: str = "An MCP server",
    version: str = "1.0.0",
    repository_url: str | None = None,
    official: bool = True,
):
    entry = {
        "server": {
            "name": server_name,
            "title": title,
            "description": description,
            "version": version,
            "remotes": [
                {
                    "type": "streamable-http",
                    "url": f"https://mcp.example/{server_name}",
                }
            ],
        },
        "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active" if official else "unofficial",
            }
        },
    }
    if repository_url:
        entry["server"]["repository"] = {"url": repository_url}
    return entry


@pytest.mark.asyncio
async def test_discover_services_filters_to_official_active_entries():
    """Only official + active registry entries are returned."""
    payload = {
        "servers": [
            _registry_entry("slack/mcp", official=True),
            _registry_entry("figma/mcp", official=False),
            _registry_entry("zoom/mcp", official=True),
        ],
        "metadata": {"nextCursor": None},
    }

    client = httpx.AsyncClient(
        transport=MockTransport([_response(payload)]),
        base_url="https://registry.test",
    )

    results = await discover_services(
        "mcp",
        base_url="https://registry.test",
        httpx_client=client,
    )

    assert [c.source_id for c in results] == ["slack/mcp", "zoom/mcp"]


@pytest.mark.asyncio
async def test_discover_services_parses_repository_and_remote_urls():
    """Repository and remote URLs are preserved in the parsed result."""
    payload = {
        "servers": [
            _registry_entry(
                "github/mcp",
                repository_url="https://github.com/modelcontextprotocol/servers",
            )
        ],
        "metadata": {"nextCursor": None},
    }

    client = httpx.AsyncClient(
        transport=MockTransport([_response(payload)]),
        base_url="https://registry.test",
    )

    results = await discover_services(
        "mcp",
        base_url="https://registry.test",
        httpx_client=client,
    )

    assert results[0].source_id == "github/mcp"
    assert str(results[0].repository_url) == "https://github.com/modelcontextprotocol/servers"
    assert results[0].raw_metadata["mcp_remote_urls"] == ["https://mcp.example/github/mcp"]


@pytest.mark.asyncio
async def test_discover_services_uses_search_parameter():
    """The registry search parameter is sent with the service query."""
    payload = {
        "servers": [_registry_entry("slack/mcp")],
        "metadata": {"nextCursor": None},
    }

    transport = MockTransport([_response(payload)])
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://registry.test",
    )

    await discover_services(
        "slack",
        base_url="https://registry.test",
        httpx_client=client,
    )

    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request.url.params["search"] == "slack"
    assert request.url.params["limit"] == "50"


@pytest.mark.asyncio
async def test_discover_services_handles_cursor_pagination():
    """Multiple pages are fetched when the registry returns a next cursor."""
    payload_1 = {
        "servers": [_registry_entry("slack/mcp")],
        "metadata": {"nextCursor": "slack/mcp:1.0.0"},
    }
    payload_2 = {
        "servers": [_registry_entry("zoom/mcp")],
        "metadata": {"nextCursor": None},
    }

    transport = MockTransport(
        [_response(payload_1), _response(payload_2)]
    )
    client = httpx.AsyncClient(
        transport=transport,
        base_url="https://registry.test",
    )

    results = await discover_services(
        "mcp",
        base_url="https://registry.test",
        httpx_client=client,
    )

    assert [result.source_id for result in results] == ["slack/mcp", "zoom/mcp"]
    assert transport.calls[1].url.params["cursor"] == "slack/mcp:1.0.0"


@pytest.mark.asyncio
async def test_discover_services_rejects_empty_query():
    with pytest.raises(MCPSearchError):
        await discover_services("   ")


@pytest.mark.asyncio
async def test_discover_services_exposes_rate_limit():
    client = httpx.AsyncClient(
        transport=MockTransport([_response({"servers": []}, 429, {"Retry-After": "10"})]),
        base_url="https://registry.test",
    )

    with pytest.raises(MCPSearchRateLimitError):
        await discover_services(
            "slack",
            base_url="https://registry.test",
            httpx_client=client,
        )


@pytest.mark.asyncio
async def test_discover_services_exposes_api_error():
    client = httpx.AsyncClient(
        transport=MockTransport([_response({"servers": []}, 500)]),
        base_url="https://registry.test",
    )

    with pytest.raises(MCPSearchAPIError):
        await discover_services(
            "slack",
            base_url="https://registry.test",
            httpx_client=client,
        )


@pytest.mark.asyncio
async def test_discover_services_returns_candidate_references():
    """Candidates integrate directly with the existing orchestrator."""
    payload = {
        "servers": [_registry_entry("slack/mcp")],
        "metadata": {"nextCursor": None},
    }

    client = httpx.AsyncClient(
        transport=MockTransport([_response(payload)]),
        base_url="https://registry.test",
    )

    candidates = await discover_services(
        "slack",
        base_url="https://registry.test",
        httpx_client=client,
    )

    assert len(candidates) == 1
    assert candidates[0].protocol == "mcp"
    assert candidates[0].item_type.value == "tool"
    assert candidates[0].source_provider == "MCP Registry Service Search"
    assert candidates[0].source_id == "slack/mcp"
    assert candidates[0].raw_metadata["mcp_server_name"] == "slack/mcp"
