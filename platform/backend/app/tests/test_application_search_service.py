from datetime import datetime, timezone
from uuid import uuid4

import pytest

import app.search.application as application_module
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


@pytest.fixture(autouse=True)
def _no_db_in_application_search(monkeypatch):
    """Unit tests must not touch the verification DB directly.

    The real `verified_catalog_keys` bridge call is exercised in
    test_verification_bridge.py; here it is stubbed to mirror the live
    deployment's answer: every seeded fixture item is verified. Tests that
    specifically model an unverified catalog (the cold-miss ones) override
    this with `_gate_verifies_nothing`.
    """
    async def all_verified(items):
        return {
            item.canonical_id or str(item.item_id) for item in items
        }

    monkeypatch.setattr(
        application_module, "verified_catalog_keys", all_verified
    )


def _gate_verifies_nothing(monkeypatch):
    """Model a catalog where no server holds a verified badge."""
    monkeypatch.setattr(
        application_module, "verified_catalog_keys", _empty_gate
    )


async def _empty_gate(items):
    return set()


def settings(mode: str) -> Settings:
    return Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        discovery_mode=mode,
    )


def item(name: str, *, verified: bool = True) -> Item:
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
                kind="protocol_validation" if verified else "discovery",
                statement="validated" if verified else "seen in discovery",
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
    assert result.metadata.db_fallback is False
    assert result.ranked.results[0].item is live_item


class FailingOrchestrator:
    """Discovery source that cannot be reached at all (external outage)."""

    def __init__(self):
        self.calls = []

    async def discover_and_catalog(
        self,
        query,
        *,
        phase10_pipeline,
        item_type="all",
        max_results=20,
    ):
        self.calls.append(query)
        raise ConnectionError("official MCP registry unreachable")


@pytest.mark.asyncio
async def test_live_mode_discovery_failure_serves_db_catalog_fallback():
    """DB is the source of truth: an external discovery outage must degrade to
    the stored official + verified servers, never to an empty response."""
    cached_items = [item(f"db_weather_{i}") for i in range(3)]
    phase11 = FakePhase11(cached_items)
    orchestrator = FailingOrchestrator()
    service = ApplicationSearchService(
        settings=settings("live"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=FakePhase10([]),
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert orchestrator.calls == ["weather"]
    assert [ranked.item.name for ranked in result.ranked.results] == [
        f"db_weather_{i}" for i in range(3)
    ]
    assert result.metadata.mode == "live"
    assert result.metadata.db_fallback is True
    assert result.metadata.cached_results == 3
    assert result.metadata.sources_attempted[0] == "arcadedb"
    assert "arcadedb" in result.metadata.sources_succeeded


@pytest.mark.asyncio
async def test_live_mode_empty_discovery_serves_db_catalog_fallback():
    """Discovery succeeding with zero approved results still falls back to the
    durable catalog instead of serving an empty page."""
    phase11 = FakePhase11([item("db_weather_only")])
    orchestrator = FakeOrchestrator()
    orchestrator.discovery = SearchOrchestratorResult(
        candidates=(),
        sources_attempted=("mcp:mcp_registry",),
        sources_succeeded=("mcp:mcp_registry",),
        sources_failed=(),
    )
    service = ApplicationSearchService(
        settings=settings("live"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=FakePhase10([]),
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert [ranked.item.name for ranked in result.ranked.results] == [
        "db_weather_only"
    ]
    assert result.metadata.db_fallback is True
    assert result.metadata.live_candidates == 0
    assert result.metadata.approved_count == 0


@pytest.mark.asyncio
async def test_mixed_mode_warm_hit_still_runs_live_discovery_and_merges():
    """Spec v2 Section 4.2 (Warm Search): a catalog hit answers instantly from
    ArcadeDB but live discovery still runs and merges with the cached shortlist.
    """
    cached_items = [item(f"cached_weather_{i}") for i in range(5)]
    live_item = item("live_weather")
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([live_item])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    # Live discovery ran even though the catalog had verified hits.
    assert orchestrator.calls != []
    assert result.metadata.mode == "merged"
    assert result.metadata.sources_attempted == ("arcadedb", "mcp:mcp_registry")
    assert result.metadata.sources_succeeded == ("arcadedb", "mcp:mcp_registry")
    assert result.metadata.live_candidates == 1
    assert result.metadata.approved_count == 1
    # Merged shortlist: the live item plus the cached entries, capped.
    names = [ranked.item.name for ranked in result.ranked.results]
    assert "live_weather" in names
    assert any(name.startswith("cached_weather_") for name in names)
    assert len(result.ranked.results) == 6
    assert result.metadata.cached_results == 5


@pytest.mark.asyncio
async def test_mixed_mode_catalog_hit_with_no_live_approval_serves_catalog_shortlist():
    """When live discovery approves nothing, the verified catalog entries serve."""
    cached_items = [item(f"cached_weather_{i}") for i in range(5)]
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert orchestrator.calls != []
    assert result.metadata.mode == "merged"
    assert result.metadata.cached_results == 5
    assert result.metadata.approved_count == 0
    assert [ranked.item.name for ranked in result.ranked.results][:5] == [
        f"cached_weather_{i}" for i in range(5)
    ]


@pytest.mark.asyncio
async def test_mixed_mode_cold_miss_runs_live_discovery_and_caps_results(monkeypatch):
    """No verified catalog hit -> live cascade runs, results stay capped."""
    _gate_verifies_nothing(monkeypatch)
    phase11 = FakePhase11([item("stale_weather", verified=False)])
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([item("live_weather", verified=False)])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert orchestrator.calls == [("weather", phase10, "tool", 10)]
    assert result.metadata.mode == "merged"
    assert result.metadata.live_candidates == 1
    assert result.metadata.approved_count == 1
    # The unverified cached row is gated OUT of the shortlist entirely.
    assert phase11.rank_calls[0][1] == [phase10.result.approved[0]]
    assert [ranked.item for ranked in result.ranked.results] == [
        phase10.result.approved[0]
    ]
    assert result.metadata.cached_results == 0


@pytest.mark.asyncio
async def test_mixed_mode_cold_miss_serves_official_catalog_fallback_when_live_approves_nothing(monkeypatch):
    """Stale-but-available: with no live approval the verified/official
    catalog shortlist serves - and the cap applies to it."""
    _gate_verifies_nothing(monkeypatch)  # nothing badge-verified in cache
    cached_items = [item(f"official_weather_{i}") for i in range(9)]
    # Mark every fixture as a seeded official connector.
    for entry in cached_items:
        entry.provenance[0].provider = "official_connectors"
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=20)

    assert orchestrator.calls != []
    assert result.metadata.mode == "merged"
    assert result.metadata.approved_count == 0
    # Fallback capped at the cold-miss cap (7), not the requested 20.
    assert len(result.ranked.results) == 7
    assert result.metadata.cached_results == 7


@pytest.mark.asyncio
async def test_merged_mode_counts_every_cached_row_under_colliding_canonical_keys():
    """Regression: 'cached' must count catalog rows, not distinct canonical_id values.

    Phase 10 stores one row per (server, tool) with the sha256 canonical id
    derived from different payloads, so multiple cached rows for the same
    server can carry DIFFERENT canonical_id values while `_merge_items`
    deduped them under one uuid5 key. Counting membership of the merged
    shortlist's keys against `live_ids` therefore underreported: 4 cached
    Tavily rows showed as `1 cached` next to `4 approved` live candidates.
    """
    # Same shape the live DB had: four verified registry items, all with
    # distinct canonical ids.
    cached_items = [item(f"cached_tavily_{i}") for i in range(4)]
    # Live discovery re-found the same server: its approved copy shares the
    # canonical identity the merge collapses onto, so 4 cached -> 1 merged.
    live_copy = cached_items[0].model_copy(deep=True)
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator()
    phase10 = FakePhase10([live_copy])
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("tavily", item_type="tool", limit=10)

    assert result.metadata.mode == "merged"
    # All 4 catalog rows were served (3 un-collapsed + 1 live-won copy).
    assert len(result.ranked.results) == 4
    assert result.metadata.cached_results == 4
    assert result.metadata.approved_count == 1
    assert result.metadata.live_candidates == 1
