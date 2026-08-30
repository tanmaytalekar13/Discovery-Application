import pytest

from app.discovery.common.candidate import CandidateReference, SourceOutcome
from app.models import ItemType, SourceType
from app.search.orchestrator import DiscoveryOrchestrator


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


class FakeProtocolAdapter:
    def __init__(self, outcomes):
        self._outcomes = outcomes
        self.calls = []

    async def discover(self, query, max_results):
        self.calls.append((query, max_results))
        return self._outcomes


def test_orchestrator_requires_at_least_one_adapter():
    with pytest.raises(ValueError):
        DiscoveryOrchestrator()


@pytest.mark.asyncio
async def test_empty_query_is_rejected():
    mcp_adapter = FakeProtocolAdapter([])
    orchestrator = DiscoveryOrchestrator(mcp_adapter=mcp_adapter)

    with pytest.raises(ValueError):
        await orchestrator.discover("   ")


@pytest.mark.asyncio
async def test_runs_mcp_and_a2a_adapters_concurrently_and_aggregates():
    mcp_adapter = FakeProtocolAdapter(
        [
            SourceOutcome(
                source="github", succeeded=True, candidates=(_candidate("mcp"),)
            )
        ]
    )
    a2a_adapter = FakeProtocolAdapter(
        [
            SourceOutcome(
                source="a2a_registry[0]",
                succeeded=True,
                candidates=(_candidate("a2a"),),
            )
        ]
    )

    orchestrator = DiscoveryOrchestrator(
        mcp_adapter=mcp_adapter, a2a_adapter=a2a_adapter
    )

    result = await orchestrator.discover("web scraping tool")

    assert len(result.candidates) == 2
    assert len(result.tool_candidates) == 1
    assert len(result.agent_candidates) == 1
    assert result.sources_attempted == ("mcp:github", "a2a:a2a_registry[0]")
    assert result.sources_succeeded == ("mcp:github", "a2a:a2a_registry[0]")
    assert result.sources_failed == ()

    # Both protocol adapters must actually have been invoked.
    assert mcp_adapter.calls == [("web scraping tool", 20)]
    assert a2a_adapter.calls == [("web scraping tool", 20)]


@pytest.mark.asyncio
async def test_failed_source_is_reported_without_breaking_the_result():
    mcp_adapter = FakeProtocolAdapter(
        [
            SourceOutcome(
                source="github", succeeded=True, candidates=(_candidate("mcp"),)
            ),
            SourceOutcome(
                source="mcp_registry",
                succeeded=False,
                candidates=(),
                error="RuntimeError: registry unavailable",
            ),
        ]
    )

    orchestrator = DiscoveryOrchestrator(mcp_adapter=mcp_adapter)

    result = await orchestrator.discover("web scraping tool")

    assert len(result.candidates) == 1
    assert result.sources_succeeded == ("mcp:github",)
    assert result.sources_failed == ("mcp:mcp_registry",)
    assert result.source_errors == {
        "mcp:mcp_registry": "RuntimeError: registry unavailable"
    }


@pytest.mark.asyncio
async def test_item_type_tool_only_runs_mcp_adapter():
    mcp_adapter = FakeProtocolAdapter([])
    a2a_adapter = FakeProtocolAdapter([])

    orchestrator = DiscoveryOrchestrator(
        mcp_adapter=mcp_adapter, a2a_adapter=a2a_adapter
    )

    await orchestrator.discover("web scraping tool", item_type="tool")

    assert mcp_adapter.calls == [("web scraping tool", 20)]
    assert a2a_adapter.calls == []


@pytest.mark.asyncio
async def test_item_type_agent_only_runs_a2a_adapter():
    mcp_adapter = FakeProtocolAdapter([])
    a2a_adapter = FakeProtocolAdapter([])

    orchestrator = DiscoveryOrchestrator(
        mcp_adapter=mcp_adapter, a2a_adapter=a2a_adapter
    )

    await orchestrator.discover("research agent", item_type="agent")

    assert mcp_adapter.calls == []
    assert a2a_adapter.calls == [("research agent", 20)]


@pytest.mark.asyncio
async def test_invalid_item_type_is_rejected():
    orchestrator = DiscoveryOrchestrator(mcp_adapter=FakeProtocolAdapter([]))

    with pytest.raises(ValueError):
        await orchestrator.discover("query", item_type="bogus")
