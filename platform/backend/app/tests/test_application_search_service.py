from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config import Settings
from app.discovery.common.candidate import CandidateReference
from app.models import (
    DiscoveryEvidence,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemType,
    Reliability,
    SourceType,
    ToolMetadata,
)
from app.normalization.pipeline import Phase10Result
from app.query.planner import QueryPlan
from app.query.ranking import RankedItem
from app.query.service import Phase11SearchResult
from app.search.application import ApplicationSearchService
from app.search.orchestrator import SearchOrchestratorResult


def settings(mode: str) -> Settings:
    return Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        discovery_mode=mode,
    )


def item(name: str) -> Item:
    now = datetime.now(timezone.utc)
    source = DiscoverySource(
        type=SourceType.MCP_REGISTRY,
        id=name,
        url=f"https://example.com/{name}",
        provider="test",
    )
    return Item(
        item_id=uuid4(),
        canonical_id=name,
        type=ItemType.TOOL,
        name=name,
        description=f"{name} weather forecast data",
        source=source,
        provenance=[source],
        evidence=[
            DiscoveryEvidence(
                evidence_id=uuid4(),
                kind="protocol_validation",
                statement="validated",
                source=source,
                observed_at=now,
            )
        ],
        reliability=Reliability(score=0.9, confidence=0.8),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        tool=ToolMetadata(
            server_id=name,
            tool_name=name,
            mcp_schema={"type": "object"},
        ),
    )


def candidate() -> CandidateReference:
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="MCP Registry",
        source_id="live_weather",
        title="live_weather",
        description="weather forecast data",
    )


class FakePhase11:
    def __init__(self, cached_items=()):
        self.cached_items = list(cached_items)
        self.search_calls = []
        self.rank_calls = []

    async def search(self, query, *, item_type=None, limit=None):
        self.search_calls.append((query, item_type, limit))
        return Phase11SearchResult(
            plan=self._plan(query, item_type),
            results=tuple(
                RankedItem(
                    item=entry,
                    final_score=0.8,
                    relevance=0.8,
                    reliability=0.9,
                    freshness=1.0,
                    evidence=0.5,
                    source_priority=1.0,
                )
                for entry in self.cached_items
            ),
        )

    async def rank_catalog_items(
        self,
        query,
        items,
        *,
        item_type=None,
        limit=None,
        plan=None,
    ):
        self.rank_calls.append((query, list(items), item_type, limit, plan))
        ranked = [
            RankedItem(
                item=entry,
                final_score=0.9,
                relevance=0.9,
                reliability=entry.reliability.score,
                freshness=1.0,
                evidence=0.5,
                source_priority=1.0,
            )
            for entry in items
        ]
        return Phase11SearchResult(
            plan=plan or self._plan(query, item_type),
            results=tuple(ranked[: limit or 20]),
        )

    @staticmethod
    def _plan(query, item_type):
        return QueryPlan(
            keywords=["weather"],
            preferred_type=item_type or "all",
            expanded_query=query,
            used_fallback=True,
        )


class FakeOrchestrator:
    def __init__(self):
        self.calls = []
        self.discovery = SearchOrchestratorResult(
            candidates=(candidate(),),
            sources_attempted=("mcp:mcp_registry",),
            sources_succeeded=("mcp:mcp_registry",),
            sources_failed=(),
        )

    async def discover_and_catalog(
        self,
        query,
        *,
        phase10_pipeline,
        item_type="all",
        max_results=20,
    ):
        self.calls.append((query, phase10_pipeline, item_type, max_results))
        return self.discovery, phase10_pipeline.result


class FakePhase10:
    def __init__(self, approved):
        self.result = Phase10Result(
            candidates_seen=1,
            resolved=1,
            deduplicated=1,
            approved=tuple(approved),
            rejected=(),
        )


@pytest.mark.asyncio
async def test_cached_mode_only_uses_phase11_cached_search():
    cached_item = item("cached_weather")
    phase11 = FakePhase11([cached_item])
    service = ApplicationSearchService(settings=settings("cached"), phase11=phase11)

    result = await service.search("weather", item_type="tool", limit=5)

    assert result.metadata.mode == "cached"
    assert result.metadata.sources_attempted == ("arcadedb",)
    assert result.metadata.cached_results == 1
    assert result.ranked.results[0].item is cached_item
    assert phase11.search_calls == [("weather", "tool", 5)]
    assert phase11.rank_calls == []


@pytest.mark.asyncio
async def test_live_mode_runs_discovery_catalog_then_ranks_approved_items():
    live_item = item("live_weather")
    phase11 = FakePhase11()
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([live_item])
    service = ApplicationSearchService(
        settings=settings("live"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=3)

    assert orchestrator.calls == [("weather", phase10, "tool", 3)]
    assert phase11.search_calls == []
    assert phase11.rank_calls[0][1] == [live_item]
    assert result.metadata.mode == "live"
    assert result.metadata.live_candidates == 1
    assert result.metadata.approved_count == 1
    assert result.ranked.results[0].item is live_item


@pytest.mark.asyncio
async def test_mixed_mode_merges_cached_and_live_before_phase11_ranking():
    cached_item = item("cached_weather")
    live_item = item("live_weather")
    phase11 = FakePhase11([cached_item])
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([live_item])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert result.metadata.mode == "merged"
    assert result.metadata.sources_attempted == ("arcadedb", "mcp:mcp_registry")
    assert result.metadata.cached_results == 1
    assert result.metadata.approved_count == 1
    assert phase11.rank_calls[0][1] == [live_item, cached_item]
    assert [ranked.item for ranked in result.ranked.results] == [
        live_item,
        cached_item,
    ]


@pytest.mark.asyncio
async def test_mixed_mode_skips_seed_results_when_live_discovery_succeeds_but_approves_nothing():
    cached_item = item("cached_weather")
    phase11 = FakePhase11([cached_item])
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert result.metadata.mode == "merged"
    assert result.metadata.cached_results == 0
    assert result.metadata.approved_count == 0
    assert result.ranked.results == ()
