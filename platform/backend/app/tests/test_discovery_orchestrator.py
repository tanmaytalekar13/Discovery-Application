import pytest

from app.discovery.common.candidate import CandidateReference, SourceOutcome
from app.models import ItemType, SourceType
from app.search.orchestrator import DiscoveryOrchestrator
from app.search.mcp_adapter import MCPDiscoveryAdapter


def _candidate(protocol: str) -> CandidateReference:
    item_type = ItemType.TOOL if protocol == "mcp" else ItemType.AGENT
    return CandidateReference(
        protocol=protocol,
        item_type=item_type,
        source_type=SourceType.GITHUB,
        source_provider="GitHub",
        source_id="example/repo",
        title="example",
        description="",
    )


class FakeMCPAdapter:
    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls = []

    async def discover(self, query, max_results, min_results=1):
        self.calls.append((query, max_results, min_results))
        return self._outcomes


def test_orchestrator_requires_an_mcp_adapter():
    with pytest.raises(ValueError):
        DiscoveryOrchestrator(mcp_adapter=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_empty_query_is_rejected():
    orchestrator = DiscoveryOrchestrator(mcp_adapter=FakeMCPAdapter([]))

    with pytest.raises(ValueError):
        await orchestrator.discover("   ")


@pytest.mark.asyncio
async def test_runs_the_mcp_adapter_and_aggregates_outcomes():
    candidate = _candidate("mcp")  # one instance: fields carry fresh uuid/timestamps
    adapter = FakeMCPAdapter(
        [
            SourceOutcome(
                source="github", succeeded=True, candidates=(candidate,)
            ),
            SourceOutcome(
                source="mcp_registry",
                succeeded=False,
                candidates=(),
                error="RuntimeError: registry unavailable",
            ),
        ]
    )
    orchestrator = DiscoveryOrchestrator(mcp_adapter=adapter)

    result = await orchestrator.discover("web scraping tool", min_results=3)

    assert len(result.candidates) == 1
    assert result.tool_candidates == (candidate,)
    assert result.sources_attempted == ("mcp:github", "mcp:mcp_registry")
    assert result.sources_succeeded == ("mcp:github",)
    assert result.sources_failed == ("mcp:mcp_registry",)
    assert result.source_errors == {
        "mcp:mcp_registry": "RuntimeError: registry unavailable"
    }
    # The shortfall is plumbed through to the adapter's cascade threshold.
    assert adapter.calls == [("web scraping tool", 20, 3)]


@pytest.mark.asyncio
async def test_item_type_agent_runs_no_live_source():
    adapter = FakeMCPAdapter([])
    orchestrator = DiscoveryOrchestrator(mcp_adapter=adapter)

    result = await orchestrator.discover("research agent", item_type="agent")

    assert result.candidates == ()
    assert result.sources_attempted == ()
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_invalid_item_type_is_rejected():
    orchestrator = DiscoveryOrchestrator(mcp_adapter=FakeMCPAdapter([]))

    with pytest.raises(ValueError):
        await orchestrator.discover("query", item_type="bogus")


def test_adapter_is_the_real_mcp_cascade():
    """The orchestrator's contract is written against MCPDiscoveryAdapter."""
    adapter = MCPDiscoveryAdapter(mcp_registry=object())
    assert isinstance(adapter, MCPDiscoveryAdapter)
