"""
Tests for source caching and resolution.
"""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.artifacts import disk_cache, source_resolver
from app.models import (
    ArtifactMetadata,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemStatus,
    ItemType,
    Reliability,
    SourceType,
)


def create_test_item(source_url: str | None = None) -> Item:
    """Helper to create a test item."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    return Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name="test-tool",
        description="Test tool",
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="test-tool",
        ),
        reliability=Reliability(score=0.9, confidence=0.8),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
        artifacts=ArtifactMetadata(
            source_url=source_url,
            source_code="print('hello')",
        ),
    )


@pytest.mark.asyncio
async def test_get_repository_tree_cache_hit(tmp_path, monkeypatch):
    """Test cache hit for repository tree."""
    monkeypatch.setattr(disk_cache, "CACHE_BASE_DIR", tmp_path)

    item = create_test_item("https://github.com/owner/repo")
    cache_path = disk_cache.generate_cache_path(item.item_id, "source_tree", "json")
    item.artifacts.source_tree_cache_path = cache_path

    # Pre-populate cache
    cached_tree = [{"path": "README.md", "type": "blob", "size": 100}]
    disk_cache.write_cached_json(cache_path, {"tree": cached_tree})

    # Fetch tree - should hit cache
    result, returned_path = await source_resolver.get_repository_tree(item)

    assert result.available is True
    assert result.tree == cached_tree
    assert returned_path == cache_path


@pytest.mark.asyncio
async def test_get_source_file_cache_hit(tmp_path, monkeypatch):
    """Test cache hit for individual source file."""
    monkeypatch.setattr(disk_cache, "CACHE_BASE_DIR", tmp_path)

    item = create_test_item("https://github.com/owner/repo")
    file_path = "src/main.py"
    cache_path = disk_cache.generate_source_file_cache_path(item.item_id, file_path)

    # Pre-populate cache
    cached_content = "def main(): pass"
    disk_cache.write_cached_file(cache_path, cached_content)

    # Fetch file - should hit cache
    result = await source_resolver.get_source_file(item, file_path)

    assert result.available is True
    assert result.content == cached_content
    assert result.language == "python"
