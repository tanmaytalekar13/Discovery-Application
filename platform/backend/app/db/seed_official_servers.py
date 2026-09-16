"""Seed the catalog with the official MCP connector directory.

Reads ``official_mcp_servers.json`` (same directory) and upserts one
``Item(type=tool)`` per entry:

  - name                 -> "Official: <display_name>" so official servers
                            are visually and lexically distinct from
                            registry/GitHub results.
  - canonical_id         -> "official:<id>" — the idempotency key. Re-running
                            this seeder updates in place instead of
                            duplicating (lookup happens on canonical_id).
  - source provider      -> "official_connectors" — the ranking layer treats
                            this as the highest-priority source.
  - artifacts.config_files -> [{"kind": "mcp_registry_remotes", ...}] with the
                            connector's URL + transport, so the existing
                            classifier marks the item `remote` and the whole
                            Test Tool flow (connect -> 401 -> OAuth authorize
                            -> callback -> tools list) works unchanged.
  - reliability          -> 0.95/0.9 for first-party official entries. This is
                            the directory's own claim (an official vendor URL),
                            not a live protocol verification; the platform's
                            verification pipeline still gates actual usage.

Non-STATIC entries (tenant/store-specific URLs with a placeholder) are seeded
with ``source_code`` notes and are NOT given a dialable remote candidate —
they surface in search results but the classifier will not offer a live test
until the user's own placeholder is known. Platform-excluded entries
(WayStation/Rube/Zapier gateways) are skipped entirely, matching the
verification prefilter.

Usage:
    python -m app.db.seed_official_servers
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.db.repositories import ItemRepository
from app.models import (
    ArtifactMetadata,
    DiscoveryEvidence,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemStatus,
    ItemType,
    Reliability,
    SourceType,
    ToolMetadata,
)

JSON_PATH = Path(__file__).parent / "official_mcp_servers.json"
PROVIDER = "official_connectors"


def _canonical_id(entry_id: int) -> str:
    return f"official:{entry_id}"


def _server_id(entry: dict) -> str:
    """Stable, human-readable server identity used by ToolMetadata."""
    slug = str(entry.get("display_name") or entry.get("vendor") or "server")
    slug = "".join(c if c.isalnum() else "-" for c in slug.lower())
    slug = "-".join(part for part in slug.split("-") if part)
    return f"official-{entry.get('id')}-{slug}"


def _transport(raw: str | None) -> str:
    """Map the directory's transport labels to the classifier's vocabulary."""
    value = (raw or "").lower()
    if "sse" in value:
        return "sse"
    return "streamable-http"


def _is_static(entry: dict) -> bool:
    return (entry.get("url_type") or "").upper() == "STATIC"


def _is_dialable(entry: dict) -> bool:
    """True when the URL can be dialed as-is.

    The only blockers are a `{placeholder}` the user must fill (tenant /
    store / API-key templates) and platform-excluded operators. A
    REGION_SPECIFIC URL with the region pinned in the URL itself (AWS,
    LangSmith EU) is perfectly dialable.
    """
    url = entry.get("mcp_url") or ""
    return bool(url) and "{" not in url


def _build_item(entry: dict, now: datetime) -> Item:
    dialable = _is_dialable(entry)
    url = entry.get("mcp_url") or ""
    transport = _transport(entry.get("transport"))

    remotes = (
        [{"type": transport, "url": url}]
        if dialable
        else []
    )
    config_files: list[dict] = []
    if remotes:
        config_files.append({"kind": "mcp_registry_remotes", "remotes": remotes})

    description = (
        f"Official {entry.get('display_name', '')} MCP server operated by "
        f"{entry.get('vendor', 'the vendor')}. "
    )
    if entry.get("auth_kind") == "oauth2":
        description += (
            "Requires OAuth sign-in: clicking Test opens the provider's "
            "consent screen (like Claude's connector flow), and the tool "
            "list appears after authorization."
        )
    elif entry.get("auth_kind") in {"api_key", "oauth_or_api_key"}:
        description += (
            "Accepts a credential at connect time (API key/token entry is "
            "offered in the test dialog)."
        )
    elif entry.get("auth_kind") == "none":
        description += "No sign-in required to test."
    else:
        description += f"Authentication: {entry.get('auth') or 'provider-dependent'}."
    if not dialable:
        description += (
            f" URL is {entry.get('url_type', 'account-specific').lower()} — "
            "replace the placeholder with your own account value before testing."
        )
    if entry.get("notes"):
        description += f" ({entry['notes']})"

    source_url = url or None
    reliability = Reliability(
        score=0.95 if entry.get("auth_kind") != "unknown" else 0.85,
        confidence=0.9 if dialable else 0.6,
        scoring_version="official-seed-v1",
        last_evaluated=now,
        security_validation=0.0,  # untouched by hand — real security checks still apply
        signals={
            "first_party": 1.0 if not entry.get("platform_excluded") else 0.0,
            "static_endpoint": 1.0 if dialable else 0.0,
        },
        reasons=[
            "Official vendor-published MCP endpoint from the connector directory"
        ],
    )

    item = Item(
        item_id=uuid4(),
        canonical_id=_canonical_id(entry["id"]),
        type=ItemType.TOOL,
        name=f"Official: {entry.get('display_name', entry.get('vendor', 'Unknown'))}",
        description=description,
        source=DiscoverySource(
            type=SourceType.CONFIGURED,
            id=_server_id(entry),
            url=source_url,
            provider=PROVIDER,
        ),
        provenance=[
            DiscoverySource(
                type=SourceType.CONFIGURED,
                id=_server_id(entry),
                url=source_url,
                provider=PROVIDER,
            )
        ],
        evidence=[
            DiscoveryEvidence(
                evidence_id=uuid4(),
                kind="official_directory",
                statement=(
                    f"Listed as an official MCP connector "
                    f"(auth={entry.get('auth') or 'unknown'}, "
                    f"transport={entry.get('transport') or 'streamable-http'})."
                ),
                source=DiscoverySource(
                    type=SourceType.CONFIGURED,
                    id=_server_id(entry),
                    url=source_url,
                    provider=PROVIDER,
                ),
                observed_at=now,
                details={
                    "auth_kind": entry.get("auth_kind"),
                    "oauth_flow": entry.get("oauth_flow"),
                    "url_type": entry.get("url_type"),
                    "testable": bool(dialable),
                },
            )
        ],
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=reliability,
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        tool=ToolMetadata(
            server_id=_server_id(entry),
            tool_name="mcp",
            mcp_schema={
                "type": "object",
                "properties": {},
                "official": True,
                "auth_kind": entry.get("auth_kind"),
            },
        ),
        artifacts=ArtifactMetadata(
            source_available=bool(source_url),
            source_url=source_url,
            source_code=None,
            config_files=config_files,
        ),
    )
    return item


async def _find_by_canonical_id(repository: ItemRepository, canonical_id: str) -> Item | None:
    response = await repository._db.command(
        "sql",
        "SELECT FROM Item WHERE canonical_id = :canonical_id LIMIT 1",
        {"canonical_id": canonical_id},
    )
    rows = response.get("result", [])
    return repository._to_item(rows[0]) if rows else None


async def seed(limit: int | None = None, dry_run: bool = False) -> dict:
    """Upsert every testable official connector into the catalog.

    Returns a summary dict (seeded/updated/skipped counts) so tests and the
    caller can assert on behavior without inspecting the DB.
    """
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    entries = data.get("connectors", [])
    if limit is not None:
        entries = entries[:limit]

    now = datetime.now(timezone.utc)
    summary = {"seeded": 0, "updated": 0, "skipped_excluded": 0, "skipped_no_url": 0}

    if dry_run:
        for entry in entries:
            if entry.get("platform_excluded"):
                summary["skipped_excluded"] += 1
            elif not entry.get("mcp_url"):
                summary["skipped_no_url"] += 1
            else:
                summary["seeded"] += 1
        return summary

    settings = get_settings()
    db = ArcadeDBClient(settings)
    repository = ItemRepository(db)

    for entry in entries:
        if entry.get("platform_excluded"):
            # Same gate as the verification prefilter: third-party hosted
            # aggregator gateways never enter the catalog.
            summary["skipped_excluded"] += 1
            continue
        if not entry.get("mcp_url"):
            summary["skipped_no_url"] += 1
            continue

        item = _build_item(entry, now)
        existing = await _find_by_canonical_id(repository, item.canonical_id or "")
        if existing is None:
            await repository.create(item)
            summary["seeded"] += 1
        else:
            item.item_id = existing.item_id
            item.discovery.first_seen = existing.discovery.first_seen
            await repository.update(existing.item_id, {
                "name": item.name,
                "description": item.description,
                "source_id": item.source.id,
                "source_url": str(item.source.url) if item.source.url else None,
                "source_provider": PROVIDER,
                "reliability_score": item.reliability.score,
                "reliability_confidence": item.reliability.confidence,
                "reliability_signals": item.reliability.signals,
                "reliability_reasons": item.reliability.reasons,
                "scoring_version": item.reliability.scoring_version,
                "last_evaluated": now,
                "last_seen": now,
                "last_synced": now,
                "tool": item.tool.model_dump(mode="json"),
                "artifacts": item.artifacts.model_dump(mode="json"),
            })
            summary["updated"] += 1

    return summary


async def main() -> None:
    summary = await seed()
    print("Official MCP connector seed completed.")
    print(f"  created:  {summary['seeded']}")
    print(f"  updated:  {summary['updated']}")
    print(f"  excluded: {summary['skipped_excluded']} (third-party gateways)")
    print(f"  no-url:   {summary['skipped_no_url']}")


if __name__ == "__main__":
    asyncio.run(main())
