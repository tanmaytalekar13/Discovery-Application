import pytest

from unittest import mock

from app.discovery.common.candidate import from_mcp_registry_candidate
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
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


def _registry_candidate(
    name: str,
    *,
    remotes: tuple[dict, ...] = (),
    title: str | None = None,
) -> MCPRegistryCandidate:
    return MCPRegistryCandidate(
        server_name=name,
        title=title or name,
        description="An MCP server",
        version="1.0.0",
        repository_url=None,
        packages=(),
        remotes=remotes,
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
def _converted_registry_candidates(candidates):
    """Match the real `discover_services` contract: converted candidates."""
    return tuple(from_mcp_registry_candidate(c) for c in candidates)


@pytest.mark.asyncio
async def test_official_sources_run_together_and_aggregate():
    """Registry and service search both target the official source and aggregate."""
    registry = FakeMCPRegistry(candidates=[_registry_candidate("scraper-mcp")])

    adapter = MCPDiscoveryAdapter(mcp_registry=registry, enable_service_discovery=True)

    with mock.patch(
        "app.search.mcp_adapter.discover_services",
        new=mock.AsyncMock(
            return_value=_converted_registry_candidates([_registry_candidate("service-mcp")])
        ),
    ):
        outcomes = await adapter.discover("web scraping tool")

    assert {outcome.source for outcome in outcomes} == {"mcp_registry", "service"}
    all_candidates = [c for outcome in outcomes for c in outcome.candidates]
    assert len(all_candidates) == 2


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


@pytest.mark.asyncio
async def test_registry_hit_stops_cascade_and_skips_github():
    """Spec v2 Section 9.1: official registry hit -> GitHub is never queried."""
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(candidates=[_registry_candidate("tavily")])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("tavily")

    assert {outcome.source for outcome in outcomes} == {"mcp_registry"}
    assert github.calls == []  # GitHub never contacted


@pytest.mark.asyncio
async def test_registry_miss_falls_through_to_github():
    """Official registry empty -> GitHub is queried as the fallback source."""
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(candidates=[])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("obscure-tool")

    # The empty registry attempt is still reported; GitHub is the only source
    # that produced candidates.
    assert {outcome.source for outcome in outcomes} == {"mcp_registry", "github"}
    registry_outcome = next(o for o in outcomes if o.source == "mcp_registry")
    assert registry_outcome.succeeded and registry_outcome.candidates == ()
    github_outcome = next(o for o in outcomes if o.source == "github")
    assert len(github_outcome.candidates) == 1
    assert github.calls == [("obscure-tool", 20)]


@pytest.mark.asyncio
async def test_registry_failure_falls_through_to_github():
    """A registry error is a miss, not a cascade stop - GitHub still runs."""
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(error=RuntimeError("registry down"))

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("tavily")

    by_source = {outcome.source: outcome for outcome in outcomes}
    assert by_source["mcp_registry"].succeeded is False
    assert by_source["github"].succeeded is True
    assert len(by_source["github"].candidates) == 1


@pytest.mark.asyncio
async def test_service_discovery_hit_counts_as_official_hit():
    """A service-search hit targets the official registry, so it stops the cascade."""
    github = FakeGitHub(candidates=[_github_candidate("scraper")])
    registry = FakeMCPRegistry(candidates=[])

    adapter = MCPDiscoveryAdapter(
        github=github,
        mcp_registry=registry,
        enable_service_discovery=True,
    )

    with mock.patch(
        "app.search.mcp_adapter.discover_services",
        new=mock.AsyncMock(
            return_value=_converted_registry_candidates([_registry_candidate("figma-mcp")])
        ),
    ):
        outcomes = await adapter.discover("figma")

    assert "service" in {outcome.source for outcome in outcomes}
    assert github.calls == []


@pytest.mark.asyncio
async def test_third_party_gateway_candidates_are_filtered():
    """Smithery/Glama/Composio-style hosted gateways never reach search results."""
    smithery = _registry_candidate(
        "smithery-wrapper",
        remotes=(
            {"url": "https://server.smithery.ai/@x/y/mcp", "type": "streamable-http"},
        ),
    )
    glama = _registry_candidate("glama-wrapper", title="Hosted on glama.ai")

    github = FakeGitHub(candidates=[_github_candidate("upstream-real")])
    registry = FakeMCPRegistry(candidates=[smithery, glama, _registry_candidate("clean-server")])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("anything")

    registry_outcome = next(o for o in outcomes if o.source == "mcp_registry")
    titles = [c.title for c in registry_outcome.candidates]
    assert titles == ["clean-server"]  # excluded candidates dropped


@pytest.mark.asyncio
async def test_excluded_registry_results_do_not_stop_the_github_fallback():
    """If every registry hit was an excluded gateway, GitHub fallback still runs."""
    smithery = _registry_candidate(
        "smithery-wrapper",
        remotes=(
            {"url": "https://server.smithery.ai/@x/y/mcp", "type": "streamable-http"},
        ),
    )
    github = FakeGitHub(candidates=[_github_candidate("upstream-real")])
    registry = FakeMCPRegistry(candidates=[smithery])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("tavily")

    by_source = {outcome.source: outcome for outcome in outcomes}
    assert "github" in by_source  # cascade continued past the excluded-only hit
    assert [c.title for c in by_source["github"].candidates] == ["upstream-real"]


@pytest.mark.asyncio
async def test_upstream_github_candidates_are_never_excluded():
    """The exclusion list targets hosted gateways, not upstream GitHub repos."""
    github = FakeGitHub(candidates=[_github_candidate("tavily-mcp")])
    registry = FakeMCPRegistry(candidates=[])

    adapter = MCPDiscoveryAdapter(github=github, mcp_registry=registry)

    outcomes = await adapter.discover("tavily")

    github_outcome = next(o for o in outcomes if o.source == "github")
    assert len(github_outcome.candidates) == 1
