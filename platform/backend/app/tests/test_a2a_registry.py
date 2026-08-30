import httpx
import pytest

from app.discovery.a2a_registry.client import (
    A2ARegistryAPIError,
    A2ARegistryClient,
    A2ARegistryRateLimitError,
)
from app.models import ItemType, SourceType


def _agent_entry(
    *,
    name="research-agent",
    version="1.0.0",
    status="active",
    url="https://agents.example.com/research/a2a",
    agent_card_url=None,
):
    entry = {
        "name": name,
        "description": "Research agent",
        "version": version,
        "status": status,
        "url": url,
        "repository": {"url": "https://github.com/example/research-agent"},
    }
    if agent_card_url is not None:
        entry["agentCardUrl"] = agent_card_url
    return entry


def test_parse_registry_candidate():
    candidate = A2ARegistryClient._parse_candidate(_agent_entry())

    assert candidate is not None
    assert candidate.agent_name == "research-agent"
    assert candidate.version == "1.0.0"
    assert candidate.item_type is ItemType.AGENT
    assert candidate.protocol == "a2a"
    assert candidate.source.type is SourceType.A2A_CATALOG
    assert candidate.source.id == "research-agent"
    assert candidate.validation_required is True
    assert candidate.endpoint == "https://agents.example.com/research/a2a"
    assert candidate.repository_url == "https://github.com/example/research-agent"


def test_parse_candidate_prefers_agent_card_url_as_source_url():
    candidate = A2ARegistryClient._parse_candidate(
        _agent_entry(
            agent_card_url="https://agents.example.com/.well-known/agent-card.json"
        )
    )

    assert candidate is not None
    assert str(candidate.source.url) == (
        "https://agents.example.com/.well-known/agent-card.json"
    )


def test_deleted_agent_is_not_candidate():
    assert A2ARegistryClient._parse_candidate(_agent_entry(status="deleted")) is None


def test_removed_agent_is_not_candidate():
    assert A2ARegistryClient._parse_candidate(_agent_entry(status="removed")) is None


def test_entry_without_name_is_rejected():
    assert A2ARegistryClient._parse_candidate({"description": "no name"}) is None


def test_entry_without_endpoint_or_card_url_is_rejected():
    entry = _agent_entry(url=None)
    del entry["url"]
    assert A2ARegistryClient._parse_candidate(entry) is None


def test_empty_base_url_is_rejected():
    with pytest.raises(ValueError):
        A2ARegistryClient(base_url="  ")


@pytest.mark.asyncio
async def test_search_handles_cursor_pagination():
    calls = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(request.url)

            if request.url.path == "/agents":
                cursor = request.url.params.get("cursor")

                if not cursor:
                    return httpx.Response(
                        200,
                        json={
                            "agents": [_agent_entry()],
                            "nextCursor": "next-page",
                        },
                    )

                return httpx.Response(
                    200,
                    json={
                        "agents": [_agent_entry(name="second-agent")],
                        "nextCursor": None,
                    },
                )

            return httpx.Response(404, json={})

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with A2ARegistryClient(
        base_url="https://registry.test",
        httpx_client=client,
        page_size=10,
    ) as registry:
        results = await registry.search(query="research", max_results=10)

    assert len(results) == 2
    assert results[0].agent_name == "research-agent"
    assert results[1].agent_name == "second-agent"
    assert len(calls) == 2
    assert calls[1].params["cursor"] == "next-page"


@pytest.mark.asyncio
async def test_search_stops_at_max_results():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                200,
                json={
                    "agents": [
                        _agent_entry(),
                        _agent_entry(name="second-agent"),
                    ],
                    "nextCursor": "unused",
                },
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with A2ARegistryClient(
        base_url="https://registry.test", httpx_client=client
    ) as registry:
        results = await registry.search(query="research", max_results=1)

    assert len(results) == 1


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

    async with A2ARegistryClient(
        base_url="https://registry.test", httpx_client=client
    ) as registry:
        with pytest.raises(A2ARegistryRateLimitError):
            await registry.search("research", max_results=1)


@pytest.mark.asyncio
async def test_api_failure_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(503, json={"error": "unavailable"})

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with A2ARegistryClient(
        base_url="https://registry.test", httpx_client=client
    ) as registry:
        with pytest.raises(A2ARegistryAPIError):
            await registry.search("research", max_results=1)


@pytest.mark.asyncio
async def test_invalid_agents_field_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, json={"agents": "not-a-list"})

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with A2ARegistryClient(
        base_url="https://registry.test", httpx_client=client
    ) as registry:
        with pytest.raises(A2ARegistryAPIError):
            await registry.search("research", max_results=1)


@pytest.mark.asyncio
async def test_invalid_json_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(200, content=b"not json")

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://registry.test",
    )

    async with A2ARegistryClient(
        base_url="https://registry.test", httpx_client=client
    ) as registry:
        with pytest.raises(A2ARegistryAPIError):
            await registry.search("research", max_results=1)
