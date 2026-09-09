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
    _source_priority,
    rank_items,
    RankingWeights,
)


def _make_item(
    *,
    name: str = "test-item",
    description: str = "A test item",
    provider: str = "",
    source_type: SourceType = SourceType.MCP_REGISTRY,
    reliability: float = 0.8,
) -> Item:
    now = datetime.now(timezone.utc)
    src = DiscoverySource(type=source_type, id="test", provider=provider)
    return Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name=name,
        description=description,
        source=src,
        provenance=[src],
        evidence=[],
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(
            score=reliability, confidence=0.9, scoring_version="1.0"
        ),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        artifacts=ArtifactMetadata(source_available=False, config_files=[]),
    )


class TestSourcePriority:
    def test_mcp_registry_has_highest_priority(self):
        item = _make_item(provider="MCP Registry")
        assert _source_priority(item) == 1.25

    def test_github_is_the_neutral_fallback(self):
        item = _make_item(provider="GitHub")
        assert _source_priority(item) == 1.0

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

    def test_mcp_registry_item_ranks_above_github_with_same_good_match(self, embedder):
        """A good matching official entry wins an otherwise equal GitHub tie."""
        mcp_item = _make_item(provider="MCP Registry", name="same-score-item")
        github_item = _make_item(provider="GitHub", name="same-score-item")

        results = rank_items(
            "test query",
            [mcp_item, github_item],
            embedder=embedder,
            weights=RankingWeights(
                relevance=0.45,
                reliability=0.35,
                freshness=0.1,
                evidence=0.1,
            ),
        )

        # With equal matching and quality scores, the official entry wins.
        assert results[0].item.provenance[0].provider == "MCP Registry"
        assert results[0].source_priority == 1.25

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

    @pytest.mark.parametrize(
        ("query", "relevant_name"),
        [
            ("Google Calendar", "mcp-google-calendar"),
            ("Google Meet MCP", "google-meet-mcp-server"),
            ("Slack", "slack-mcp"),
            ("GitHub", "github-mcp"),
            ("Google Drive", "google-drive-mcp"),
        ],
    )
    def test_strong_service_match_beats_high_reliability_unrelated_result(
        self, embedder, query, relevant_name
    ):
        relevant = _make_item(
            name=relevant_name,
            description=f"MCP integration for {query}",
            provider="GitHub",
            source_type=SourceType.GITHUB,
            reliability=0.05,
        )
        unrelated = _make_item(
            name="gmail-mcp",
            description="A popular Gmail mail server",
            provider="MCP Registry",
            reliability=1.0,
        )

        results = rank_items(query, [unrelated, relevant], embedder=embedder)

        assert results[0].item.name == relevant_name
