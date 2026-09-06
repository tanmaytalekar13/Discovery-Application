"""Tests for ranking source priority multiplier."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.models import (
    ArtifactMetadata,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemType,
    ItemStatus,
    Reliability,
    SourceType,
)
from app.query.ranking import (
    SOURCE_PRIORITY_MULTIPLIER,
    _source_priority,
    rank_items,
    RankingWeights,
)


def _make_item(
    *,
    name: str = "test-item",
    provider: str = "",
    source_type: SourceType = SourceType.MCP_REGISTRY,
) -> Item:
    now = datetime.now(timezone.utc)
    src = DiscoverySource(type=source_type, id="test", provider=provider)
    return Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name=name,
        description="A test item",
        source=src,
        provenance=[src],
        evidence=[],
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(score=0.8, confidence=0.9, scoring_version="1.0"),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        artifacts=ArtifactMetadata(source_available=False, config_files=[]),
    )


class TestSourcePriority:
    def test_mcp_registry_has_highest_priority(self):
        item = _make_item(provider="MCP Registry")
        assert _source_priority(item) == 2.0

    def test_npm_registry_has_high_priority(self):
        item = _make_item(provider="npm Registry")
        assert _source_priority(item) == 1.3

    def test_github_topics_has_high_priority(self):
        item = _make_item(provider="GitHub Topics")
        assert _source_priority(item) == 1.3

    def test_github_has_medium_priority(self):
        item = _make_item(provider="GitHub")
        assert _source_priority(item) == 1.2

    def test_awesome_list_has_slight_priority(self):
        item = _make_item(provider="awesome-list")
        assert _source_priority(item) == 1.1

    def test_web_search_has_low_priority(self):
        item = _make_item(provider="Web Search")
        assert _source_priority(item) == 0.9

    def test_unknown_provider_defaults_to_one(self):
        item = _make_item(provider="Some Unknown Source")
        assert _source_priority(item) == 1.0


class TestRankedItemsWithSourcePriority:
    """Test that source priority affects final ranking."""

    @pytest.fixture
    def embedder(self):
        """A mock embedder that returns a fixed vector."""
        mock = MagicMock()
        mock.embed_text.return_value = [0.5, 0.5]
        mock.embed_item.return_value = [0.5, 0.5]
        return mock

    def test_mcp_registry_item_ranks_above_npm_with_same_base_score(
        self, embedder
    ):
        """Even with equal relevance, MCP Registry should outrank npm."""
        mcp_item = _make_item(provider="MCP Registry", name="same-score-item")
        npm_item = _make_item(provider="npm Registry", name="same-score-item")

        results = rank_items(
            "test query",
            [mcp_item, npm_item],
            embedder=embedder,
            weights=RankingWeights(
                relevance=0.45,
                reliability=0.35,
                freshness=0.1,
                evidence=0.1,
            ),
        )

        # MCP Registry has priority 2.0, npm has 1.3
        # With equal base scores, MCP wins
        assert results[0].item.provenance[0].provider == "MCP Registry"
        assert results[0].source_priority == 2.0

    def test_source_priority_multiplier_in_final_score(self, embedder):
        """The final_score should incorporate the source priority multiplier."""
        item = _make_item(provider="MCP Registry")
        results = rank_items(
            "test query",
            [item],
            embedder=embedder,
            weights=RankingWeights(
                relevance=0.45,
                reliability=0.35,
                freshness=0.1,
                evidence=0.1,
            ),
        )
        # final_score = base_score * 2.0
        # base_score ≈ 0.45*1.0 + 0.35*0.8 + 0.1*... = ~0.725
        # final_score ≈ 0.725 * 2.0 ≈ 1.45
        assert results[0].final_score > results[0].relevance
