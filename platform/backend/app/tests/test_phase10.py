from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from app.config import Settings
from app.discovery.common.candidate import CandidateReference
from app.discovery.github.client import GitHubCandidate
from app.discovery.mcp_registry.client import MCPRegistryCandidate
from app.models import ItemType, SourceType
from app.models import DiscoverySource
from app.normalization.pipeline import Phase10Pipeline
from app.reliability.engine import evaluate


class FakeRepository:
    def __init__(self):
        self.items = []

    async def upsert_catalog_item(self, item, evaluation):
        self.items.append((item, evaluation))
        return item


def candidate():
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="test",
        source_id="server-1",
        url="https://github.com/example/weather-mcp",
        title="Weather MCP",
        description="Test",
        evidence=("registry evidence",),
    )


@pytest.mark.asyncio
async def test_phase10_rejects_unvalidated_mcp_and_reports_rejection_evidence():
    """Rejections stay in the pipeline result; nothing is written to the DB."""
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost", arcadedb_database="test", arcadedb_user="root", arcadedb_password="root"
    )
    result = await Phase10Pipeline(repo, settings).process([candidate()])
    assert not result.approved
    assert len(result.rejected) == 1
    assert result.rejected[0].evidence
    assert "MCP Registry metadata alone" in " ".join(result.rejected[0].evidence)
    assert repo.items == []  # rejection was not persisted as an Item either


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
async def test_phase10_merges_same_repository_from_multiple_discovery_sources():
    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
    )
    candidates = [
        CandidateReference(
            protocol="mcp",
            item_type=ItemType.TOOL,
            source_type=source_type,
            source_provider=provider,
            source_id=source_id,
            url="https://github.com/A1-x-Tech/mcp-google-calendar",
            repository_url="https://github.com/A1-x-Tech/mcp-google-calendar",
            title="mcp-google-calendar",
            description="Google Calendar Model Context Protocol server",
            evidence=("repository explicitly identifies an MCP server",),
        )
        for source_type, provider, source_id in (
            (SourceType.GITHUB, "GitHub", "A1-x-Tech/mcp-google-calendar"),
            (SourceType.CONFIGURED, "MCP Registry", "io.github.A1-x-Tech/calendar"),
        )
    ]

    result = await Phase10Pipeline(repo, settings).process(candidates)

    assert len(result.approved) == 1
    assert {source.provider for source in result.approved[0].provenance} == {
        "GitHub",
        "MCP Registry",
    }


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






# ---------------------------------------------------------------------------
# Sandbox-footprint catalog gate: oversized registry packages never become
# Items, so they never appear in search results at all.
# ---------------------------------------------------------------------------


def _npm_packument(size_bytes: int, version: str = "1.0.0") -> dict:
    return {
        "dist-tags": {"latest": version},
        "versions": {
            version: {
                "version": version,
                "dist": {"unpackedSize": size_bytes},
            }
        },
    }


def _registry_candidate(identifier: str, registry_type: str = "npm") -> CandidateReference:
    raw = MCPRegistryCandidate(
        server_name="io.example/heavy",
        title="Heavy MCP",
        description="Some Model Context Protocol server",
        version="1.0.0",
        repository_url="https://github.com/example/heavy-mcp",
        packages=({"registryType": registry_type, "identifier": identifier},),
        remotes=(),
        raw_server={"name": "io.example/heavy", "version": "1.0.0"},
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="io.example/heavy",
            url="https://github.com/example/heavy-mcp",
        ),
    )
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="MCP Registry",
        source_id="io.example/heavy",
        url="https://github.com/example/heavy-mcp",
        repository_url="https://github.com/example/heavy-mcp",
        title="Heavy MCP",
        description="Some Model Context Protocol server",
        evidence=("returned by the official MCP Registry as version 1.0.0",),
        raw_metadata={"source_candidate": raw},
    )


@pytest.mark.asyncio
async def test_phase10_rejects_oversized_npm_package_before_catalog(monkeypatch):
    """A package over the sandbox footprint limit must be REJECTED at the
    catalog boundary - no Item, so it can never appear in search results."""
    from app.sandbox.package_size import NPM_UNPACKED_SIZE_LIMIT_BYTES

    oversized = NPM_UNPACKED_SIZE_LIMIT_BYTES + 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_npm_packument(oversized))

    async def fake_estimate(self, registry_type, identifier):
        from app.sandbox.package_size import PackageSizeEstimate

        return PackageSizeEstimate(
            identifier=identifier, registry="npm", bytes_estimate=oversized
        )

    monkeypatch.setattr(
        "app.sandbox.package_size.PackageSizeEstimator.estimate", fake_estimate
    )

    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    result = await Phase10Pipeline(repo, settings).process(
        [_registry_candidate("@example/huge-mcp")]
    )

    assert len(result.approved) == 0
    assert len(result.rejected) == 1
    assert "sandbox" in result.rejected[0].reason.lower()
    assert any("install closure" in r for r in result.rejected[0].evidence)
    assert repo.items == []  # never persisted -> never searchable


@pytest.mark.asyncio
async def test_phase10_rejects_oversized_pypi_package(monkeypatch):
    from app.sandbox.package_size import PYPI_DEPENDENCY_LIMIT, PackageSizeEstimate

    async def fake_estimate(self, registry_type, identifier):
        return PackageSizeEstimate(
            identifier=identifier,
            registry="pypi",
            dependency_count=PYPI_DEPENDENCY_LIMIT + 5,
        )

    monkeypatch.setattr(
        "app.sandbox.package_size.PackageSizeEstimator.estimate", fake_estimate
    )

    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    result = await Phase10Pipeline(repo, settings).process(
        [_registry_candidate("huge-pypi-server", registry_type="pypi")]
    )

    assert len(result.approved) == 0
    assert len(result.rejected) == 1
    assert repo.items == []


@pytest.mark.asyncio
async def test_phase10_size_gate_fails_open_on_unknown_size(monkeypatch):
    """Registry error / missing metadata must NOT reject the candidate."""
    from app.sandbox.package_size import PackageSizeEstimate

    async def fake_estimate(self, registry_type, identifier):
        return PackageSizeEstimate(identifier=identifier, registry="npm")

    monkeypatch.setattr(
        "app.sandbox.package_size.PackageSizeEstimator.estimate", fake_estimate
    )

    repo = FakeRepository()
    settings = Settings(
        arcadedb_host="localhost",
        arcadedb_database="test",
        arcadedb_user="root",
        arcadedb_password="root",
        reliability_threshold=0.75,
    )
    result = await Phase10Pipeline(repo, settings).process(
        [_registry_candidate("@example/some-mcp")]
    )

    # Unknown size: candidate proceeds into the normal pipeline and is
    # either approved or rejected for OTHER reasons - never the size gate.
    size_rejections = [
        r for r in result.rejected if "sandbox" in (r.reason or "").lower()
    ]
    assert size_rejections == []


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
