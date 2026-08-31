from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config import Settings
from app.discovery.common.candidate import CandidateReference
from app.models import ItemType, SourceType
from app.normalization.pipeline import Phase10Pipeline
from app.reliability.engine import evaluate


class FakeRepository:
    def __init__(self):
        self.items = []
        self.rejections = []

    async def upsert_catalog_item(self, item, evaluation):
        self.items.append((item, evaluation))
        return item

    async def persist_rejection(self, rejection):
        self.rejections.append(rejection)


def candidate(protocol="a2a"):
    return CandidateReference(
        protocol=protocol,
        item_type=ItemType.AGENT if protocol == "a2a" else ItemType.TOOL,
        source_type=SourceType.A2A_CATALOG if protocol == "a2a" else SourceType.MCP_REGISTRY,
        source_provider="test",
        source_id="agent-1" if protocol == "a2a" else "server-1",
        url="https://example.com/agent-card.json",
        title="Test Agent",
        description="Test",
        evidence=("registry evidence",),
    )


@pytest.mark.asyncio
async def test_phase10_rejects_unvalidated_mcp_and_persists_rejection_evidence():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost", arcadedb_database="test", arcadedb_user="root", arcadedb_password="root"
    )
    result = await Phase10Pipeline(repo, settings).process([candidate("mcp")])
    assert not result.approved
    assert len(result.rejected) == 1
    assert repo.rejections[0].evidence
    assert "MCP Registry metadata alone" in " ".join(repo.rejections[0].evidence)


@pytest.mark.asyncio
async def test_phase10_normalizes_validated_a2a_with_full_provenance_and_evidence():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost", arcadedb_database="test", arcadedb_user="root", arcadedb_password="root",
        reliability_threshold=0.75,
    )
    async def resolver(_):
        return {
            "endpoint": "https://example.com/rpc",
            "protocol_version": "1.0",
            "raw_agent_card": {"name": "Test Agent", "description": "Validated agent", "url": "https://example.com/rpc", "skills": []},
            "agent": {"endpoint": "https://example.com/rpc", "skills": [], "capabilities": [], "declared_dependencies": []},
        }
    result = await Phase10Pipeline(repo, settings, a2a_resolver=resolver).process([candidate()])
    assert len(result.approved) == 1
    item = result.approved[0]
    assert item.canonical_id
    assert item.provenance[0].provider == "test"
    assert len(item.evidence) >= 2
    assert item.reliability.security_validation > 0
    assert repo.items


def test_reliability_includes_security_signal_and_explainable_reasons():
    from app.models import DiscoveryMetadata, DiscoverySource, Item, Reliability
    now = datetime.now(timezone.utc)
    item = Item(
        item_id=uuid4(), type=ItemType.AGENT, name="agent", description="agent",
        source=DiscoverySource(type=SourceType.A2A_CATALOG, id="agent", url="https://example.com", provider="registry"),
        provenance=[], evidence=[], reliability=Reliability(score=0, confidence=0),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
    )
    result = evaluate(item)
    assert "security_validation" in result.signals
    assert any("security" in reason for reason in result.reasons)
