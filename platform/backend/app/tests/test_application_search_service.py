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
from app.search.application import ApplicationSearchService, _merge_items
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


def item(name: str, *, verified: bool = True, repo: str | None = None) -> Item:
    now = datetime.now(timezone.utc)
    source = DiscoverySource(
        type=SourceType.MCP_REGISTRY,
        id=name,
        url=f"https://example.com/{name}",
        provider="test",
    )
    provenance = [source]
    if repo:
        provenance.append(
            DiscoverySource(
                type=SourceType.GITHUB,
                id=repo,
                url=f"https://github.com/{repo}",
                provider="GitHub",
            )
        )
    return Item(
        item_id=uuid4(),
        canonical_id=name,
        type=ItemType.TOOL,
        name=name,
        description=f"{name} weather forecast data",
        source=source,
        provenance=provenance,
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
    def __init__(self, approved_items=()):
        self.calls = []
        self._approved = list(approved_items)
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
        min_results=1,
    ):
        self.calls.append(
            {
                "query": query,
                "phase10_pipeline": phase10_pipeline,
                "item_type": item_type,
                "max_results": max_results,
                "min_results": min_results,
            }
        )
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
async def test_db_shortlist_full_stops_external_discovery_early():
    """Early stop: >= cap servable DB hits -> external discovery never starts.

    The cap is the app's own `search_verified_result_cap` (default 3):
    1 official + 2-3 verified servers must answer a query from the DB
    without touching the registry or GitHub.
    """
    cached_items = [item(f"db_weather_{i}") for i in range(4)]
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator()
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=FakePhase10([]),
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert orchestrator.calls == []  # external discovery never started
    assert result.metadata.mode == "merged"
    assert result.metadata.sources_attempted == ("arcadedb",)
    assert result.metadata.sources_succeeded == ("arcadedb",)
    assert [ranked.item.name for ranked in result.ranked.results] == [
        f"db_weather_{i}" for i in range(4)
    ]
    assert result.metadata.cached_results == 4


@pytest.mark.asyncio
async def test_db_shortfall_runs_discovery_with_min_results():
    """DB short of the cap -> discovery runs with the shortfall as min_results."""
    cached_items = [item("db_weather_only")]
    live_items = [item("live_weather_1"), item("live_weather_2")]
    phase11 = FakePhase11(cached_items)
    orchestrator = FakeOrchestrator(approved_items=live_items)
    phase10 = FakePhase10(live_items)
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    # cap (7) - 1 DB hit = 6 shortfall. Cap is applied via `limit`, so the
    # discovery request itself carries min(10, 7) - 1 = 6.
    assert len(orchestrator.calls) == 1
    assert orchestrator.calls[0]["min_results"] == 6
    assert result.metadata.mode == "merged"
    names = [ranked.item.name for ranked in result.ranked.results]
    assert "db_weather_only" in names
    assert "live_weather_1" in names
    assert "live_weather_2" in names
    assert result.metadata.approved_count == 2
    assert result.metadata.cached_results == 1


@pytest.mark.asyncio
async def test_discovery_failure_serves_db_catalog_fallback():
    """DB is the source of truth: an external discovery outage must degrade to
    the stored official + verified servers, never to an empty response."""
    cached_items = [item(f"db_weather_{i}") for i in range(2)]
    phase11 = FakePhase11(cached_items)

    class FailingOrchestrator:
        async def discover_and_catalog(self, query, **kwargs):
            raise ConnectionError("official MCP registry unreachable")

    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=FailingOrchestrator(),
        phase10_pipeline=FakePhase10([]),
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert [ranked.item.name for ranked in result.ranked.results] == [
        f"db_weather_{i}" for i in range(2)
    ]
    assert result.metadata.mode == "merged"
    assert result.metadata.db_fallback is True
    assert result.metadata.cached_results == 2
    assert result.metadata.sources_attempted[0] == "arcadedb"
    assert "arcadedb" in result.metadata.sources_succeeded


@pytest.mark.asyncio
async def test_cold_miss_with_no_db_hits_still_runs_discovery_and_caps(monkeypatch):
    """No servable DB hit -> discovery runs; results stay capped."""
    _gate_verifies_nothing(monkeypatch)
    phase11 = FakePhase11([item("stale_weather", verified=False)])
    live_items = [item("live_weather", verified=False)]
    orchestrator = FakeOrchestrator(approved_items=live_items)
    phase10 = FakePhase10(live_items)
    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert orchestrator.calls[0]["min_results"] == 7  # full cap shortfall
    assert result.metadata.mode == "merged"
    assert result.metadata.live_candidates == 1
    assert result.metadata.approved_count == 1
    # The unverified cached row is gated OUT of the shortlist entirely.
    assert [ranked.item for ranked in result.ranked.results] == live_items
    assert result.metadata.cached_results == 0


@pytest.mark.asyncio
async def test_sufficient_official_catalog_stops_external_discovery(monkeypatch):
    """Early stop covers official rows too: enough seeded official servers
    answer the query from the durable catalog without touching discovery,
    capped at the cold-miss cap."""
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

    assert orchestrator.calls == []  # external discovery never started
    assert result.metadata.mode == "merged"
    assert result.metadata.approved_count == 0
    assert result.metadata.db_fallback is False
    # Shortlist capped at the cold-miss cap (7), not the requested 20.
    assert len(result.ranked.results) == 7
    assert result.metadata.cached_results == 7


@pytest.mark.asyncio
async def test_official_fallback_marks_db_fallback_when_discovery_runs_and_approves_nothing(monkeypatch):
    """True cold miss: only 1 official row in the DB, discovery runs, the
    registry fails and validates nothing - the single official row serves
    (external failure never blanks the catalog) and `db_fallback` is set."""
    _gate_verifies_nothing(monkeypatch)
    cached_items = [item("official_weather")]
    cached_items[0].provenance[0].provider = "official_connectors"
    phase11 = FakePhase11(cached_items)

    class FailingOrchestrator:
        async def discover_and_catalog(self, query, **kwargs):
            raise ConnectionError("official MCP registry unreachable")

    service = ApplicationSearchService(
        settings=settings("mixed"),
        phase11=phase11,
        orchestrator=FailingOrchestrator(),
        phase10_pipeline=FakePhase10([]),
    )

    result = await service.search("weather", item_type="tool", limit=10)

    assert [ranked.item.name for ranked in result.ranked.results] == [
        "official_weather"
    ]
    assert result.metadata.mode == "merged"
    assert result.metadata.db_fallback is True
    assert result.metadata.cached_results == 1
    assert result.metadata.sources_attempted[0] == "arcadedb"
    assert "arcadedb" in result.metadata.sources_succeeded


def test_merge_items_deduplicates_on_repo_identity():
    """Registry mirror + GitHub repo for the SAME repository -> one result."""
    db_copy = item("slack_db", repo="owner/slack-mcp")
    registry_copy = item("slack_registry", repo="owner/slack-mcp")
    distinct = item("other_tool", repo="owner/other-mcp")

    merged = _merge_items([db_copy], [registry_copy, distinct])

    names = [entry.name for entry in merged]
    assert names == ["slack_db", "other_tool"]  # duplicate live copy dropped
    assert merged[0] is db_copy  # the durable catalog row stays


def test_merge_items_keeps_genuinely_different_servers():
    db_copy = item("db_one", repo="owner/one-mcp")
    live_a = item("live_a", repo="owner/two-mcp")
    live_b = item("live_b", repo="owner/three-mcp")

    merged = _merge_items([db_copy], [live_a, live_b])

    assert [entry.name for entry in merged] == ["db_one", "live_a", "live_b"]


def test_merge_items_collapses_rows_without_repo_urls():
    """Items with no repo identity still dedupe on canonical identity."""
    db_copy = item("slack_db")
    live_copy = item("slack_db")  # same canonical_id, no repo provenance

    merged = _merge_items([db_copy], [live_copy])

    assert [entry.name for entry in merged] == ["slack_db"]
    assert merged[0] is live_copy  # live copy wins the key
