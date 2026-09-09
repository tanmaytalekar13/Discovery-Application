import base64

import httpx
import pytest

from app.discovery.github.client import (
    GitHubAPIError,
    GitHubCandidate,
    GitHubDiscoveryAdapter,
    GitHubRateLimitError,
    github_mcp_search_query,
)
from app.models import DiscoverySource, ItemType, SourceType


def _repo(**overrides):
    repo = {
        "full_name": "example/mcp-server",
        "name": "mcp-server",
        "html_url": "https://github.com/example/mcp-server",
        "clone_url": "https://github.com/example/mcp-server.git",
        "default_branch": "main",
        "description": "A Model Context Protocol server",
        "topics": ["mcp"],
    }

    repo.update(overrides)
    return repo


def test_classifies_mcp_repository():
    result = GitHubDiscoveryAdapter.classify_repository(
        _repo(),
        "This implements tools/list and tools/call.",
        [],
    )

    assert result is not None

    item_type, evidence = result

    assert item_type is ItemType.TOOL
    assert evidence


def test_classifies_a2a_repository():
    repo = _repo(
        full_name="example/research-agent",
        name="research-agent",
        description="An A2A agent with an Agent Card",
        topics=["a2a"],
    )

    result = GitHubDiscoveryAdapter.classify_repository(
        repo,
        "Supports message/send and A2A protocol.",
        [{"path": ".well-known/agent-card.json"}],
    )

    assert result is not None

    item_type, evidence = result

    assert item_type is ItemType.AGENT
    assert evidence


def test_rejects_false_positive_repository():
    repo = _repo(
        full_name="example/mcp-related-article",
        name="mcp-related-article",
        description="An article about MCP history",
        topics=[],
    )

    result = GitHubDiscoveryAdapter.classify_repository(
        repo,
        "This is a blog post about Model Context Protocol.",
        [],
    )

    assert result is None


@pytest.mark.parametrize(
    ("raw_query", "expected"),
    [
        ("Google Calendar", "google calendar mcp"),
        ("google calendar mcp", "google calendar mcp"),
        ("google-calendar", "google calendar mcp"),
        ("Google Calendar MCP server", "google calendar mcp"),
        ("Google Meet", "google meet mcp"),
        ("Slack", "slack mcp"),
        ("GitHub", "github mcp"),
        ("Google Drive", "google drive mcp"),
    ],
)
def test_github_query_preserves_service_and_normalizes_protocol_words(
    raw_query, expected
):
    assert github_mcp_search_query(raw_query) == expected


def test_candidate_contains_github_provenance():
    repo = _repo()

    result = GitHubDiscoveryAdapter.classify_repository(
        repo,
        "MCP server using tools/call.",
        [],
    )

    assert result is not None

    candidate = GitHubCandidate(
        repository=repo["full_name"],
        name=repo["name"],
        html_url=repo["html_url"],
        clone_url=repo["clone_url"],
        default_branch=repo["default_branch"],
        description=repo["description"],
        item_type=result[0],
        evidence=tuple(result[1]),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id=repo["full_name"],
            url=repo["html_url"],
        ),
    )

    assert candidate.source.type is SourceType.GITHUB
    assert candidate.source.id == repo["full_name"]
    assert str(candidate.source.url) == repo["html_url"]


@pytest.mark.asyncio
async def test_discovery_uses_search_and_repository_evidence():
    calls = []

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            calls.append(str(request.url))

            if request.url.path == "/search/repositories":
                return httpx.Response(
                    200,
                    json={"items": [_repo()]},
                )

            if request.url.path.endswith("/readme"):
                content = base64.b64encode(
                    b"Model Context Protocol server using tools/call"
                ).decode()

                return httpx.Response(
                    200,
                    json={"content": content},
                )

            if request.url.path.endswith("/contents"):
                return httpx.Response(
                    200,
                    json=[
                        {
                            "path": "mcp.json",
                            "type": "file",
                        }
                    ],
                )

            return httpx.Response(
                404,
                json={},
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://api.github.test",
    )

    async with GitHubDiscoveryAdapter(httpx_client=client) as adapter:
        results = await adapter.discover(
            "mcp",
            max_results=5,
        )

    assert len(results) == 1
    assert results[0].item_type is ItemType.TOOL
    assert results[0].source.type is SourceType.GITHUB
    assert results[0].source.id == "example/mcp-server"

    assert any("/search/repositories" in call for call in calls)
    search_call = next(call for call in calls if "/search/repositories" in call)
    assert "q=mcp" in search_call
    # A wider retrieval window prevents GitHub popularity ordering from
    # excluding low-star MCP repositories before classification.
    assert "per_page=30" in search_call


@pytest.mark.asyncio
async def test_rate_limit_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                },
                json={"message": "API rate limit exceeded"},
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://api.github.test",
    )

    async with GitHubDiscoveryAdapter(httpx_client=client) as adapter:
        with pytest.raises(GitHubRateLimitError):
            await adapter.discover(
                "mcp",
                max_results=1,
            )


@pytest.mark.asyncio
async def test_api_error_is_explicit():
    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return httpx.Response(
                500,
                json={"message": "server error"},
            )

    client = httpx.AsyncClient(
        transport=MockTransport(),
        base_url="https://api.github.test",
    )

    async with GitHubDiscoveryAdapter(httpx_client=client) as adapter:
        with pytest.raises(GitHubAPIError):
            await adapter.discover(
                "mcp",
                max_results=1,
            )
