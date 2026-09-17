"""Tests for the verification bridge (Item catalog <-> McpServer registry).

Covers: badge attachment to search results with the three match keys
(source_url, registry_name, repo identity) and the user-verified
persistence path (existing record update + new record creation), plus
the contract that a registry failure never breaks the search flow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import DiscoverySource, Item, Reliability, DiscoveryMetadata, SourceType
from app.verification.bridge import (
    attach_verification_badges,
    record_user_verified_success,
)
from app.verification.models import McpServerRecord, ServerStatus, Transport


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeVerificationRepo:
    """Minimal stand-in for VerificationRepository keyed by identity."""

    def __init__(self) -> None:
        self.records: dict[str, McpServerRecord] = {}
        self.updates: list[tuple[str, dict]] = []
        self._seq = 0

    def add(self, record: McpServerRecord) -> None:
        self.records[record.source_url] = record

    async def get_by_source_url(self, source_url):
        return self.records.get(source_url)

    async def get(self, server_id):
        for record in self.records.values():
            if str(record.server_id) == str(server_id):
                return record
        return None

    async def select_badge_rows(self, *, source_urls, registry_names, repo_identities):
        """Mirror the real repository's batched badge SELECT."""
        from app.verification.ingestion import normalize_repo_identity

        rows: list[dict] = []
        for record in self.records.values():
            identity = normalize_repo_identity(record.repository_url)
            hit = (
                record.source_url in source_urls
                or (record.registry_name and record.registry_name in registry_names)
                or (identity and identity in repo_identities)
            )
            if hit:
                rows.append({
                    "server_id": str(record.server_id),
                    "source_url": record.source_url,
                    "registry_name": record.registry_name,
                    "repository_url": record.repository_url,
                    "status": record.status.value,
                    "quality_score": record.quality_score,
                    "invocation_verified": record.invocation_verified,
                    "verified_badge": record.verified_badge,
                    "verified_via_auth": record.verified_via_auth,
                    "last_verified_via": record.last_verified_via,
                })
        return rows

    async def get_by_registry_name(self, registry_name):
        for record in self.records.values():
            if record.registry_name == registry_name:
                return record
        return None

    async def list_status(self, *statuses, limit=50):
        return [
            r for r in self.records.values() if r.status.value in statuses
        ][:limit]

    async def create(self, record):
        if record.source_url in self.records:
            return self.records[record.source_url]
        self._seq += 1
        record.source_url = record.source_url or f"gen-{self._seq}"
        self.records[record.source_url] = record
        return record

    async def update(self, server_id, updates):
        for record in self.records.values():
            if str(record.server_id) == str(server_id):
                data = record.model_dump()
                for key, value in updates.items():
                    data[key] = value
                updated = McpServerRecord.model_validate(data)
                self.records[updated.source_url] = updated
                self.updates.append((str(server_id), updates))
                return updated
        return None


class _NeverCalledRepo:
    def __getattr__(self, name):
        raise AssertionError("repo should not be touched on failure path")


def _make_item(**overrides) -> Item:
    now = datetime.now(timezone.utc)
    defaults = dict(
        item_id=overrides.pop("item_id", __import__("uuid").uuid4()),
        type="tool",
        name=overrides.pop("name", "Tavily MCP"),
        description=overrides.pop("description", "web search"),
        source=overrides.pop(
            "source",
            DiscoverySource(
                type=SourceType.MCP_REGISTRY,
                id="io.github.tavily-ai/tavily-mcp",
                url="https://github.com/tavily-ai/tavily-mcp",
            ),
        ),
        provenance=overrides.pop("provenance", []),
        reliability=Reliability(score=0.9, confidence=0.9),
        discovery=DiscoveryMetadata(
            first_seen=now, last_seen=now, last_synced=now
        ),
    )
    return Item(**defaults, **overrides)


def _make_row_record(source_url: str, **extra) -> McpServerRecord:
    return McpServerRecord(
        name="Tavily",
        source_url=source_url,
        transport=Transport.REMOTE,
        endpoint_url="https://mcp.tavily.com",
        repository_url=extra.pop("repository_url", None),
        registry_name=extra.pop("registry_name", None),
        status=ServerStatus.VERIFIED,
        quality_score=95,
        invocation_verified=True,
        verified_badge=True,
        verified_via_auth=extra.pop("verified_via_auth", False),
        last_verified_via=extra.pop("last_verified_via", "anonymous"),
        last_verified_at=datetime.now(timezone.utc),
        ttl_expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        **extra,
    )


# ---------------------------------------------------------------------------
# attach_verification_badges
# ---------------------------------------------------------------------------


def _result(item: Item):
    return SimpleNamespace(item=item, verification=None)


class TestAttachBadges:
    @pytest.mark.asyncio
    async def test_matches_by_registry_name(self, monkeypatch):
        repo = FakeVerificationRepo()
        repo.add(_make_row_record(
            "https://registry.modelcontextprotocol.io/v0.1/servers/io.github.tavily-ai/tavily-mcp",
            registry_name="io.github.tavily-ai/tavily-mcp",
        ))
        monkeypatch.setattr(
            "app.verification.bridge._repo", lambda: repo
        )

        results = [_result(_make_item())]
        await attach_verification_badges(results)

        verification = results[0].verification
        assert verification is not None
        assert verification["verified_badge"] is True
        assert verification["quality_score"] == 95
        assert verification["status"] == "verified"

    @pytest.mark.asyncio
    async def test_matches_by_repo_identity_over_registry_url(self, monkeypatch):
        """Item source URL points at GitHub; the McpServer row stores the
        same owner/repo in repository_url -> fork-proof match."""
        repo = FakeVerificationRepo()
        repo.add(_make_row_record(
            "https://registry.example/tavily",
            repository_url="https://github.com/tavily-ai/tavily-mcp.git",
            registry_name="io.github.other/name",
        ))
        monkeypatch.setattr("app.verification.bridge._repo", lambda: repo)

        item = _make_item(
            source=DiscoverySource(
                type=SourceType.GITHUB,
                id="tavily-ai/tavily-mcp",
                url="https://github.com/tavily-ai/tavily-mcp",
            )
        )
        results = [_result(item)]
        await attach_verification_badges(results)
        assert results[0].verification is not None
        assert results[0].verification["verified_badge"] is True

    @pytest.mark.asyncio
    async def test_no_match_leaves_verification_none(self, monkeypatch):
        repo = FakeVerificationRepo()
        repo.add(_make_row_record("https://registry.example/unrelated"))
        monkeypatch.setattr("app.verification.bridge._repo", lambda: repo)

        results = [_result(_make_item())]
        await attach_verification_badges(results)
        assert results[0].verification is None

    @pytest.mark.asyncio
    async def test_registry_failure_never_breaks_search(self, monkeypatch):
        async def boom(*args, **kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(
            "app.verification.bridge._repo", lambda: _NeverCalledRepo()
        )
        # _fetch_badge_rows internally hits repo._db -> make it raise
        repo = SimpleNamespace(_db=SimpleNamespace(command=boom))
        monkeypatch.setattr(
            "app.verification.bridge._repo", lambda: repo
        )

        results = [_result(_make_item())]
        await attach_verification_badges(results)  # must not raise
        assert results[0].verification is None


# ---------------------------------------------------------------------------
# record_user_verified_success
# ---------------------------------------------------------------------------


class TestUserVerifiedPersistence:
    @pytest.mark.asyncio
    async def test_updates_existing_registry_record(self, monkeypatch):
        repo = FakeVerificationRepo()
        existing = McpServerRecord(
            name="Tavily",
            source_url="https://github.com/tavily-ai/tavily-mcp",
            transport=Transport.LOCAL,
            install_cmd="npx -y tavily-mcp",
            repository_url="https://github.com/tavily-ai/tavily-mcp",
            status=ServerStatus.PENDING,
        )
        repo.add(existing)
        monkeypatch.setattr("app.verification.bridge._repo", lambda: repo)

        item = _make_item(
            source=DiscoverySource(
                type=SourceType.GITHUB,
                id="tavily-ai/tavily-mcp",
                url="https://github.com/tavily-ai/tavily-mcp",
            )
        )
        await record_user_verified_success(
            item, via_auth=True, tools=[{"name": "search"}]
        )

        assert repo.updates, "existing record must be updated"
        updates = repo.updates[0][1]
        assert updates["verified_badge"] is True
        assert updates["verified_via_auth"] is True
        assert updates["last_verified_via"] == "user_test"
        assert updates["invocation_verified"] is True
        assert updates["status"] == "verified"

    @pytest.mark.asyncio
    async def test_creates_record_when_unknown(self, monkeypatch):
        repo = FakeVerificationRepo()
        monkeypatch.setattr("app.verification.bridge._repo", lambda: repo)

        item = _make_item()
        await record_user_verified_success(
            item, via_auth=False, tools=[{"name": "search"}]
        )

        assert len(repo.records) == 1
        record = next(iter(repo.records.values()))
        assert record.verified_badge is True
        assert record.verified_via_auth is False
        assert record.last_verified_via == "user_test"
        assert record.status is ServerStatus.VERIFIED
        assert record.transport is Transport.LOCAL  # github source url
        # badge_fields second write also lands
        final = await repo.get(record.server_id)
        assert final is not None
        assert final.confidence.value == "high"

    @pytest.mark.asyncio
    async def test_write_failure_is_swallowed(self, monkeypatch):
        class ExplodingRepo:
            async def get_by_registry_name(self, _):
                raise RuntimeError("db down")

            async def get_by_source_url(self, _):
                raise RuntimeError("db down")

            async def list_status(self, *a, **k):
                raise RuntimeError("db down")

        monkeypatch.setattr(
            "app.verification.bridge._repo", lambda: ExplodingRepo()
        )
        item = _make_item()
        await record_user_verified_success(item, via_auth=False)  # must not raise
