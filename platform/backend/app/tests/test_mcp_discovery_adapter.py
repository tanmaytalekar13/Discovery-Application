import pytest

from app.discovery.common.candidate import CandidateReference, SourceOutcome
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.models import DiscoverySource, ItemType, SourceType
from app.search.mcp_adapter import MCPDiscoveryAdapter


def _registry_candidate(name: str) -> CandidateReference:
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="MCP Registry",
        source_id=name,
        title=name,
        description=f"{name} MCP server",
    )


def _github_candidate(name: str) -> GitHubCandidate:
    return GitHubCandidate(
        repository=f"example/{name}",
        name=name,
        html_url=f"https://github.com/example/{name}",
        clone_url=f"https://github.com/example/{name}.git",
        default_branch="main",
        description=f"{name} MCP server",
        item_type=ItemType.TOOL,
        evidence=("repository explicitly references MCP",),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id=f"example/{name}",
            url=f"https://github.com/example/{name}",
        ),
    )


class FakeRegistry:
    def __init__(self, results):
        self._results = results
        self.calls = []

    async def search(self, query, max_results=20):
        self.calls.append((query, max_results))
        return self._results


class FakeGitHub:
    def __init__(self, results):
        self._results = results
        self.calls = []

    async def discover(self, query, max_results=20):
        self.calls.append((query, max_results))
        return self._results


def _adapter(registry=None, github=None, service=False) -> MCPDiscoveryAdapter:
    return MCPDiscoveryAdapter(
        mcp_registry=registry,
        github=github,
        enable_service_discovery=service,
    )


@pytest.mark.asyncio
async def test_registry_hits_stop_the_cascade_before_github():
    """Registry covers the shortfall -> GitHub is never queried."""
    registry = FakeRegistry([object()])
    github = FakeGitHub([_github_candidate("gh_slack")])
    adapter = _adapter(
        registry=registry,
        github=github,
    )
    # Patch conversion of registry candidates.
    adapter._discover_registry = _fake_convert(registry, 2, _registry_candidate)

    outcomes = await adapter.discover("slack", max_results=10, min_results=1)

    assert [o.source for o in outcomes] == ["mcp_registry"]
    assert outcomes[0].succeeded
    assert github.calls == []


@pytest.mark.asyncio
async def test_registry_shortfall_pulls_github_until_covered():
    """DB + Registry still short -> GitHub fills the gap."""
    registry = FakeRegistry([object()])
    github = FakeGitHub([
        _github_candidate("gh_one"),
        _github_candidate("gh_two"),
    ])
    adapter = _adapter(registry=registry, github=github)
    adapter._discover_registry = _fake_convert(registry, 1, _registry_candidate)

    outcomes = await adapter.discover("slack", max_results=10, min_results=3)

    assert [o.source for o in outcomes] == ["mcp_registry", "github"]
    # Shortfall still needed after the registry candidate (3 - 1 = 2),
    # queried with the normalized GitHub MCP query - not the full page.
    assert github.calls == [("slack mcp", 2)]


@pytest.mark.asyncio
async def test_registry_outcome_failure_still_tries_github():
    """A registry outage must not silently skip the GitHub completion step."""
    registry = FakeRegistry([])

    async def broken_search(query, max_results=20):
        raise RuntimeError("registry unreachable")

    registry.search = broken_search
    github = FakeGitHub([_github_candidate("gh_one")])
    adapter = _adapter(registry=registry, github=github)

    outcomes = await adapter.discover("slack", max_results=10, min_results=1)

    assert [o.source for o in outcomes] == ["mcp_registry", "github"]
    assert outcomes[0].succeeded is False
    assert outcomes[1].succeeded is True


@pytest.mark.asyncio
async def test_disabled_sources_are_not_attempted():
    adapter = _adapter()  # nothing configured

    outcomes = await adapter.discover("slack")

    assert outcomes == []


def _fake_convert(source_client, count, factory):
    """Replace _discover_registry with a count-only fake conversion."""

    async def _convert(query, max_results):
        source_client.calls.append((query, max_results))
        return [factory(f"reg_{i}") for i in range(count)]

    return _convert
