from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
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
from app.query.embeddings import LocalEmbeddingModel
from app.query.planner import FallbackQueryPlanner, plan_query
from app.query.ranking import RankingWeights, rank_items
from app.query.service import Phase11SearchService


def settings(**overrides) -> Settings:
    values = {
        "arcadedb_host": "localhost",
        "arcadedb_database": "test",
        "arcadedb_user": "root",
        "arcadedb_password": "root",
    }
    values.update(overrides)
    return Settings(**values)


def item(
    name: str,
    description: str,
    *,
    item_type: ItemType = ItemType.TOOL,
    reliability: float = 0.8,
    last_seen: datetime | None = None,
) -> Item:
    now = datetime.now(timezone.utc)
    seen_at = last_seen or now
    source = DiscoverySource(
        type=SourceType.MCP_REGISTRY,
        id=name,
        url=f"https://example.com/{name}",
        provider="test",
    )
    return Item(
        item_id=uuid4(),
        type=item_type,
        name=name,
        description=description,
        source=source,
        provenance=[source],
        evidence=[
            DiscoveryEvidence(
                evidence_id=uuid4(),
                kind="protocol_validation",
                statement=f"{name} validated",
                source=source,
                observed_at=seen_at,
            )
        ],
        reliability=Reliability(score=reliability, confidence=0.8),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=seen_at,
            last_synced=now,
        ),
        tool=(
            ToolMetadata(
                server_id=name,
                tool_name=name,
                mcp_schema={"properties": {"query": {"type": "string"}}},
            )
            if item_type is ItemType.TOOL
            else None
        ),
    )


class FakeRepository:
    def __init__(self, items: list[Item]):
        self.items = items
        self.search_calls = []
        self.updated_embeddings = []

    async def search_rankable(self, *, item_type="all", limit=100, keywords=()):
        self.search_calls.append(
            {"item_type": item_type, "limit": limit, "keywords": tuple(keywords)}
        )
        results = self.items
        if item_type != "all":
            results = [entry for entry in results if entry.type.value == item_type]
        return results[:limit]

    async def update_embedding(self, item_id, embedding):
        self.updated_embeddings.append((item_id, embedding))
        return None


@pytest.mark.asyncio
async def test_gemini_planner_accepts_only_valid_json_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "generateContent" in str(request.url)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": (
                                        '{"keywords":["weather","mcp"],'
                                        '"preferred_type":"tool",'
                                        '"expanded_query":"weather MCP server",'
                                        '"source_hints":["registry"]}'
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plan = await plan_query(
            "weather tool",
            settings(gemini_api_key="secret"),
            httpx_client=client,
        )

    assert plan.preferred_type == "tool"
    assert plan.keywords == ["weather", "mcp"]
    assert plan.used_fallback is False


@pytest.mark.asyncio
async def test_gemini_failure_uses_fallback_planner():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        plan = await plan_query(
            "research agent for finance",
            settings(gemini_api_key="secret"),
            httpx_client=client,
        )

    assert plan.used_fallback is True
    assert plan.preferred_type == "agent"
    assert "finance" in plan.keywords


def test_fallback_planner_infers_type_keywords_and_hints():
    plan = FallbackQueryPlanner().plan("GitHub weather MCP tool from registry")

    assert plan.preferred_type == "tool"
    assert "weather" in plan.keywords
    assert plan.source_hints == ["github", "registry"]


def test_local_embeddings_are_deterministic_and_normalized():
    embedder = LocalEmbeddingModel(dimensions=16)

    first = embedder.embed_text("weather forecast search")
    second = embedder.embed_text("weather forecast search")

    assert first == second
    assert len(first) == 16
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_semantic_and_structured_ranking_prefers_relevant_reliable_fresh_item():
    embedder = LocalEmbeddingModel(dimensions=32)
    fresh_weather = item(
        "weather_search",
        "Find live weather forecast data",
        reliability=0.95,
    )
    stale_music = item(
        "music_search",
        "Find albums and tracks",
        reliability=0.95,
        last_seen=datetime.now(timezone.utc) - timedelta(days=120),
    )

    ranked = rank_items(
        "weather forecast",
        [stale_music, fresh_weather],
        embedder=embedder,
        weights=RankingWeights(),
    )

    assert ranked[0].item.name == "weather_search"
    assert ranked[0].relevance > ranked[1].relevance
    assert ranked[0].freshness > ranked[1].freshness


@pytest.mark.asyncio
async def test_phase11_service_plans_filters_backfills_embeddings_and_ranks():
    weather = item("weather_search", "Find live weather forecast data")
    finance_agent = item(
        "finance_agent",
        "Research financial filings",
        item_type=ItemType.AGENT,
    )
    repo = FakeRepository([weather, finance_agent])

    result = await Phase11SearchService(
        repo,
        settings(gemini_api_key="", embedding_dimensions=32),
    ).search("weather MCP tool", item_type="tool")

    assert result.plan.used_fallback is True
    assert repo.search_calls[0]["item_type"] == "tool"
    assert repo.updated_embeddings
    assert result.results[0].item.name == "weather_search"
