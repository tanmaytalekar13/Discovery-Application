import os

import pytest

from app.config import Settings
from app.db.client import ArcadeDBClient
from app.db.repositories import ItemRepository
from app.models import DiscoveryMetadata, DiscoverySource, Item, ItemType, Reliability, SourceType
from datetime import datetime, timezone
from uuid import uuid4


@pytest.mark.asyncio
async def test_phase10_real_arcadedb_persistence():
    if os.getenv("RUN_ARCADEDB_INTEGRATION") != "1":
        pytest.skip("set RUN_ARCADEDB_INTEGRATION=1 to run against a real ArcadeDB instance")
    settings = Settings(
        arcadedb_host=os.getenv("ARCADEDB_HOST", "127.0.0.1"),
        arcadedb_port=int(os.getenv("ARCADEDB_PORT", "2480")),
        arcadedb_database=os.getenv("ARCADEDB_DATABASE", "agentic_discovery"),
        arcadedb_user=os.getenv("ARCADEDB_USER", "root"),
        arcadedb_password=os.getenv("ARCADEDB_PASSWORD", "root"),
    )
    db = ArcadeDBClient(settings)
    assert await db.check_connection()
    repo = ItemRepository(db)
    now = datetime.now(timezone.utc)
    item = Item(
        item_id=uuid4(), canonical_id=f"integration-{uuid4()}", type=ItemType.AGENT,
        name="phase10-integration", description="Phase 10 integration test",
        source=DiscoverySource(type=SourceType.CONFIGURED, id="integration", url="https://example.com", provider="integration-test"),
        reliability=Reliability(score=.9, confidence=.9),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
    )
    class Evaluation:
        score=.9; confidence=.9; signals={"protocol_validation":1.0}; reasons=["integration"]; security_validation=1.0; approved=True
    await repo.upsert_catalog_item(item, Evaluation())
    loaded = await repo.get(item.item_id)
    assert loaded is not None
    assert loaded.canonical_id == item.canonical_id
    assert loaded.source.provider == "integration-test"
