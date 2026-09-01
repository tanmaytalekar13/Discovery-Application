from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.config import Settings
from app.discovery.common.candidate import CandidateReference
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.discovery.web_extraction.client import WebExtractionCandidate
from app.discovery.web_search.client import WebSearchCandidate
from app.models import ItemType, SourceType
from app.models import DiscoverySource
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
async def test_phase10_accepts_web_search_result_as_best_effort_live_hit():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    web_candidate = CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.WEB_SEARCH,
        source_provider="web_search",
        source_id="https://example.com/mcp-server",
        url="https://example.com/mcp-server",
        title="Example MCP Server",
        description="Model Context Protocol server for example tools",
        evidence=("result title/snippet explicitly identifies an MCP server",),
    )

    result = await Phase10Pipeline(repo, settings).process([web_candidate])

    assert len(result.approved) == 1
    item = result.approved[0]
    assert item.type is ItemType.TOOL
    assert item.source.type is SourceType.WEB_SEARCH
    assert item.name == "Example MCP Server"


@pytest.mark.asyncio
async def test_phase10_stores_extracted_web_search_content_as_artifact_source_code():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    raw = WebSearchCandidate(
        title="Example MCP Server",
        url="https://example.com/mcp-server",
        snippet="Model Context Protocol server for example tools",
        item_type=ItemType.TOOL,
        evidence=("result title/snippet explicitly identifies an MCP server",),
        source=DiscoverySource(
            type=SourceType.WEB_SEARCH,
            id="https://example.com/mcp-server",
            url="https://example.com/mcp-server",
        ),
        raw_result={"title": "Example MCP Server", "url": "https://example.com/mcp-server"},
    )
    extracted = WebExtractionCandidate(
        title="Example MCP Server",
        url="https://example.com/mcp-server",
        text_excerpt="Short page text",
        item_type=ItemType.TOOL,
        evidence=("page text explicitly identifies an MCP server",),
        source=DiscoverySource(
            type=SourceType.WEB_PAGE,
            id="https://example.com/mcp-server",
            url="https://example.com/mcp-server",
        ),
        content_type="text/html",
        text_content="Full extracted page text with Model Context Protocol details.",
    )
    web_candidate = CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.WEB_SEARCH,
        source_provider="Web Search",
        source_id="https://example.com/mcp-server",
        url="https://example.com/mcp-server",
        title="Example MCP Server",
        description="Model Context Protocol server for example tools",
        evidence=raw.evidence,
        raw_metadata={
            "source_candidate": raw,
            "web_extraction_candidate": extracted,
        },
    )

    result = await Phase10Pipeline(repo, settings).process([web_candidate])

    assert len(result.approved) == 1
    item = result.approved[0]
    assert item.source.type is SourceType.WEB_SEARCH
    assert item.artifacts.source_code == (
        "Full extracted page text with Model Context Protocol details."
    )
    assert {entry["kind"] for entry in item.artifacts.config_files} == {
        "web_search_result",
        "web_extraction",
    }


@pytest.mark.asyncio
async def test_phase10_accepts_github_mcp_candidate_and_exposes_repository_artifacts():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    raw = GitHubCandidate(
        repository="example/mcp-server",
        name="mcp-server",
        html_url="https://github.com/example/mcp-server",
        clone_url="https://github.com/example/mcp-server.git",
        default_branch="main",
        description="Model Context Protocol server",
        item_type=ItemType.TOOL,
        evidence=("repository metadata/documentation explicitly identifies an MCP server",),
        source=DiscoverySource(
            type=SourceType.GITHUB,
            id="example/mcp-server",
            url="https://github.com/example/mcp-server",
        ),
    )
    github_candidate = CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.GITHUB,
        source_provider="GitHub",
        source_id="example/mcp-server",
        url="https://github.com/example/mcp-server",
        repository_url="https://github.com/example/mcp-server",
        title="mcp-server",
        description="Model Context Protocol server",
        evidence=raw.evidence,
        raw_metadata={"source_candidate": raw},
    )

    result = await Phase10Pipeline(repo, settings).process([github_candidate])

    assert len(result.approved) == 1
    item = result.approved[0]
    assert item.source.type is SourceType.GITHUB
    assert str(item.artifacts.source_url) == "https://github.com/example/mcp-server"
    assert item.artifacts.config_files[0]["kind"] == "github_repository"
    assert item.artifacts.config_files[0]["clone_url"].endswith(".git")


@pytest.mark.asyncio
async def test_phase10_accepts_mcp_registry_candidate_and_exposes_registry_artifacts():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    raw = MCPRegistryCandidate(
        server_name="io.example/weather",
        title="Weather MCP",
        description="Weather Model Context Protocol server",
        version="1.0.0",
        repository_url="https://github.com/example/weather-mcp",
        packages=({"registryType": "npm", "identifier": "@example/weather-mcp"},),
        remotes=({"transportType": "sse", "url": "https://example.com/mcp"},),
        raw_server={"name": "io.example/weather", "version": "1.0.0"},
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="io.example/weather",
            url="https://github.com/example/weather-mcp",
        ),
    )
    registry_candidate = CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="MCP Registry",
        source_id="io.example/weather",
        url="https://github.com/example/weather-mcp",
        repository_url="https://github.com/example/weather-mcp",
        title="Weather MCP",
        description="Weather Model Context Protocol server",
        evidence=("returned by the official MCP Registry as version 1.0.0",),
        raw_metadata={"source_candidate": raw},
    )

    result = await Phase10Pipeline(repo, settings).process([registry_candidate])

    assert len(result.approved) == 1
    item = result.approved[0]
    assert item.source.type is SourceType.MCP_REGISTRY
    assert str(item.artifacts.source_url) == "https://github.com/example/weather-mcp"
    assert {entry["kind"] for entry in item.artifacts.config_files} == {
        "mcp_registry_packages",
        "mcp_registry_remotes",
        "mcp_registry_server",
    }


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
