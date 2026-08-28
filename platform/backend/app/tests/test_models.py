from datetime import datetime, timezone
from uuid import uuid4

from app.models import (
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemType,
    Reliability,
    SourceType,
)


def test_tool_item_model():
    now = datetime.now(timezone.utc)

    item = Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name="search_tracks",
        description="Search music tracks",
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="spotify-mcp",
        ),
        reliability=Reliability(
            score=0.91,
            confidence=0.88,
        ),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
    )

    assert item.type == ItemType.TOOL
    assert item.reliability.score == 0.91
    assert item.embedding is None