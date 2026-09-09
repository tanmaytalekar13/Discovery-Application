import pytest

from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.discovery.mcp_registry.service_adapter import MCPSearchResult
from app.models import DiscoverySource, ItemType, SourceType
from app.search.mcp_adapter import MCPDiscoveryAdapter


def _github_candidate(name: str) -> GitHubCandidate:
    return GitHubCandidate(
        repository=f"example/{name}",
        name=name,
        html_url=f"https://github.com/example/{name}",
        clone_url=f"https://github.com/example/{name}.git",
        default_branch="main",
        description="An MCP server",
        item_type=ItemType.TOOL,
        evidence=("uses tools/call",),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id=f"example/{name}",
            url=f"https://github.com/example/{name}",
        ),
    )


def _registry_candidate(name: str) -> MCPRegistryCandidate:
    return MCPRegistryCandidate(
        server_name=name,
        title=name,
        description="An MCP server",
        version="1.0.0",
        repository_url=None,
        packages=(),
        remotes=(),
        raw_server={"name": name},
        source=DiscoverySource(type=SourceType.MCP_REGISTRY, id=name, url=None),
    )


class FakeGitHub:
    def __init__(self, candidates=None, error=None):
        self._candidates = candidates or []
        self._error = error
        self.calls = []

    async def discover(self, query, max_results):
        self.calls.append((query, max_results))
        if self._error:
            raise self._error
        return self._candidates


class FakeMCPRegistry:
    def __init__(self, candidates=None, error=None):
        self._candidates = candidates or []
        self._error = error

    async def search(self, query, max_results):
        if self._error:
            raise self._error
        return self._candidates

@pytest.mark.asyncio
async def test_disabled_sources_are_not_attempted():
    adapter = MCPDiscoveryAdapter()

    outcomes = await adapter.discover("web scraping tool")

    assert outcomes == []
@pytest.mark.asyncio
async def test_aggregates_candidates_from_multiple_sources():
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(candidates=[_registry_candidate("scraper-mcp")])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("web scraping tool")

    assert {outcome.source for outcome in outcomes} == {"github", "mcp_registry"}
    assert all(outcome.succeeded for outcome in outcomes)

    all_candidates = [c for outcome in outcomes for c in outcome.candidates]
    assert len(all_candidates) == 2
    assert {c.source_provider for c in all_candidates} == {"GitHub", "MCP Registry"}


@pytest.mark.asyncio
async def test_one_source_failure_does_not_break_the_others():
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(error=RuntimeError("registry unavailable"))

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("web scraping tool")

    by_source = {outcome.source: outcome for outcome in outcomes}

    assert by_source["github"].succeeded is True
    assert len(by_source["github"].candidates) == 1

    assert by_source["mcp_registry"].succeeded is False
    assert by_source["mcp_registry"].candidates == ()
    assert "registry unavailable" in by_source["mcp_registry"].error


@pytest.mark.asyncio
async def test_service_discovery_enabled_flag():
    """When enabled, the adapter stores the flag (outcomes depend on registry search)."""
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(candidates=[_registry_candidate("scraper-mcp")])

    adapter = MCPDiscoveryAdapter(
        github=github,
        mcp_registry=registry,
        enable_service_discovery=True,
    )

    # Verify the flag is stored
    assert adapter._enable_service_discovery is True
