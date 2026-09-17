"""Bridge between the Item search catalog and the verification registry.

The search API reads the generic `Item` catalog while verification
status/badges live in the separate `McpServer` registry. This module is
the ONLY coupling point between the two:

- `attach_verification_badges`   : batch-reads badge fields from the
  verification registry and attaches them to ranked search results
  (one SELECT total, never blocks search on failure);
- `record_user_verified_success` : when a real user test (remote connect
  + tool invocation, or a local sandbox session that actually started
  and served tools) succeeds, persist a user-verified badge into the
  McpServer registry by source identity (registry_name, source_url, or
  canonical repo identity) so the server shows the badge everywhere.

Golden rule preserved: these helpers never trigger verification or any
network fan-out - they are either a pure DB read (search path) or a
single upsert triggered by a genuinely successful user test (write
path).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.models import SourceType
from app.verification.models import (
    Confidence,
    McpServerRecord,
    ServerStatus,
    Transport,
)
from app.verification.repository import VerificationRepository
from app.verification.scoring import ttl_days_for

logger = logging.getLogger(__name__)

# Cross-subsystem DB client cache: the search path would otherwise
# construct a fresh ArcadeDBClient per request.
_client_cache: dict[str, ArcadeDBClient] = {}


def _db_client() -> ArcadeDBClient | None:
    """A cached ArcadeDB client, or None when construction fails.

    The bridge is best-effort by design: search never fails because the
    verification registry is unreachable.
    """
    try:
        settings = get_settings()
        key = (
            f"{settings.arcadedb_host}:{settings.arcadedb_port}:"
            f"{settings.arcadedb_database}"
        )
        client = _client_cache.get(key)
        if client is None:
            client = ArcadeDBClient(settings)
            _client_cache[key] = client
        return client
    except Exception:  # noqa: BLE001 - config problems must not break search
        return None


def _repo() -> VerificationRepository | None:
    client = _db_client()
    if client is None:
        return None
    return VerificationRepository(client)


def _looks_like_http(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


# ---------------------------------------------------------------------------
# Search-side: badge lookup
# ---------------------------------------------------------------------------


async def _fetch_badge_rows(
    repo: VerificationRepository,
    *,
    source_urls: list[str],
    registry_names: list[str],
    repo_identities: list[str],
) -> list[dict[str, Any]]:
    """One SELECT for all identity candidates; each row carries the
    identity fields plus the badge fields."""
    return await repo.select_badge_rows(
        source_urls=source_urls,
        registry_names=registry_names,
        repo_identities=repo_identities,
    )


def _item_identities(item: Any) -> tuple[list[str], list[str], list[str]]:
    """(source_urls, registry_names, repo_identities) for one Item."""
    from app.verification.ingestion import normalize_repo_identity

    source_urls: list[str] = []
    registry_names: list[str] = []
    repo_identities: list[str] = []

    for url in (item.source.url, *(s.url for s in (item.provenance or []))):
        if url:
            source_urls.append(str(url))

    source = item.source
    if source is not None and source.type is SourceType.MCP_REGISTRY and source.id:
        registry_names.append(source.id)
    tool = getattr(item, "tool", None)
    if tool is not None and tool.server_id and "/" in tool.server_id:
        registry_names.append(tool.server_id)

    for url in source_urls:
        identity = normalize_repo_identity(url)
        if identity:
            repo_identities.append(identity)

    return source_urls, registry_names, repo_identities


async def attach_verification_badges(ranked_results: list[Any]) -> None:
    """Attach `verification` info to ranked SearchResultItems in place.

    Match keys, in priority order:
      1. Item source/provenance URL == McpServer.source_url (exact)
      2. Item's registry identity == McpServer.registry_name
      3. canonical owner/repo identity of the URLs vs the McpServer's
         repository_url (fork/mirror-proof)
    """
    if not ranked_results:
        return
    repo = _repo()
    if repo is None:
        return

    source_urls: list[str] = []
    registry_names: list[str] = []
    repo_identities: list[str] = []
    for result in ranked_results:
        urls, names, identities = _item_identities(result.item)
        source_urls.extend(urls)
        registry_names.extend(names)
        repo_identities.extend(identities)

    try:
        rows = await _fetch_badge_rows(
            repo,
            source_urls=sorted(set(source_urls)),
            registry_names=sorted(set(registry_names)),
            repo_identities=sorted(set(repo_identities)),
        )
    except Exception:  # noqa: BLE001 - search must never fail on badge lookup
        logger.warning(
            "Verification badge lookup failed; continuing without", exc_info=True
        )
        return

    if not rows:
        return

    from app.verification.ingestion import normalize_repo_identity

    by_source_url = {r.get("source_url"): r for r in rows if r.get("source_url")}
    by_registry_name = {
        r.get("registry_name"): r for r in rows if r.get("registry_name")
    }
    by_repo_identity: dict[str, dict[str, Any]] = {}
    for row in rows:
        identity = normalize_repo_identity(row.get("repository_url"))
        if identity:
            by_repo_identity.setdefault(identity, row)

    for result in ranked_results:
        match = None
        urls, names, identities = _item_identities(result.item)
        for url in urls:
            if url in by_source_url:
                match = by_source_url[url]
                break
        if match is None:
            for name in names:
                if name in by_registry_name:
                    match = by_registry_name[name]
                    break
        if match is None:
            for identity in identities:
                if identity in by_repo_identity:
                    match = by_repo_identity[identity]
                    break
        if match is None:
            continue

        result.verification = {
            "verified_badge": bool(match.get("verified_badge", False)),
            "verified_via_auth": bool(match.get("verified_via_auth", False)),
            "last_verified_via": match.get("last_verified_via"),
            "status": str(match.get("status") or ""),
            "quality_score": int(match.get("quality_score") or 0),
            "invocation_verified": bool(match.get("invocation_verified", False)),
        }


# ---------------------------------------------------------------------------
# Search-side: presentation ordering (official > verified > rest)
# ---------------------------------------------------------------------------

TIER_UNVERIFIED = 0
TIER_VERIFIED = 1
TIER_OFFICIAL = 2

_OFFICIAL_PROVIDER = "official_connectors"


def _result_tier(row: Any) -> int:
    """Presentation tier for one search result row.

    Official (vendor-published connector) outranks verified, which
    outranks everything else. The verification badge is whatever the
    bridge attached from the McpServer registry (pipeline, auth flow,
    or the user's own successful test).
    """
    provenance = getattr(row.item, "provenance", None) or []
    if any(
        getattr(source, "provider", None) == _OFFICIAL_PROVIDER
        for source in provenance
    ):
        return TIER_OFFICIAL
    verification = getattr(row, "verification", None)
    if verification and verification.get("verified_badge"):
        return TIER_VERIFIED
    return TIER_UNVERIFIED


def order_results_by_tier(rows: list[Any]) -> list[Any]:
    """Order search rows: official first, then verified, then the rest.

    Stable sort (Python guarantees stability): within each tier the
    ranking engine's final_score order is preserved, so this only
    regroups tiers - it never re-shuffles equal-tier results.
    """
    rows.sort(key=_result_tier, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Test-side: persist a user-verified success
# ---------------------------------------------------------------------------


async def record_user_verified_success(
    item: Any,
    *,
    via_auth: bool,
    tools: list[dict[str, Any]] | None = None,
) -> None:
    """Persist a verified badge for `item` after a REAL successful test.

    Called from the sandbox test endpoints when a user-driven connect +
    tool invocation actually succeeded (with credentials when the server
    demands them). Never invented: only a genuinely working session
    earns this.

    Match precedence against the McpServer registry:
      1. registry_name (Item source / tool.server_id when it looks like one)
      2. source_url exact
      3. canonical repo identity
    Creates the McpServer record when no row matches yet, so the badge
    is never lost just because the background pipeline hasn't ingested
    the server yet.
    """
    repo = _repo()
    if repo is None or item is None:
        return
    try:
        await _record_user_verified_success_inner(
            repo, item, via_auth=via_auth, tools=tools
        )
    except Exception:  # noqa: BLE001 - a badge write must never break a test flow
        logger.warning(
            "Could not persist user-verified badge for item %s",
            getattr(item, "item_id", "?"),
            exc_info=True,
        )


async def _record_user_verified_success_inner(
    repo: VerificationRepository,
    item: Any,
    *,
    via_auth: bool,
    tools: list[dict[str, Any]] | None,
) -> None:
    from app.verification.ingestion import normalize_repo_identity

    source_url = str(item.source.url) if item.source.url else ""
    registry_names: list[str] = []
    if item.source is not None and item.source.type is SourceType.MCP_REGISTRY \
            and item.source.id:
        registry_names.append(item.source.id)
    tool = getattr(item, "tool", None)
    if tool is not None and tool.server_id and "/" in tool.server_id:
        registry_names.append(tool.server_id)

    existing: McpServerRecord | None = None
    for name in registry_names:
        existing = await repo.get_by_registry_name(name)
        if existing is not None:
            break
    if existing is None and source_url:
        existing = await repo.get_by_source_url(source_url)
    if existing is None:
        identity = normalize_repo_identity(source_url or None)
        if identity:
            for candidate in await repo.list_status(
                *[s.value for s in ServerStatus], limit=500
            ):
                if normalize_repo_identity(candidate.repository_url) == identity:
                    existing = candidate
                    break

    now = datetime.now(timezone.utc)
    # Follow the ingestion convention: a GitHub repository URL means a
    # locally-run server; only a bare http(s) endpoint is remote.
    identity = normalize_repo_identity(source_url or None)
    transport = existing.transport.value if existing else (
        "remote"
        if source_url and _looks_like_http(source_url) and identity is None
        else "local"
    )
    updates: dict[str, Any] = {
        "status": ServerStatus.VERIFIED.value,
        "verified_badge": True,
        "verified_via_auth": via_auth,
        "last_verified_via": "user_test",
        "invocation_verified": True,
        "quality_score": max(existing.quality_score if existing else 0, 90),
        "confidence": Confidence.HIGH.value,
        "attempts_completed": 1,
        "attempts_planned": 1,
        "last_verified_at": now.isoformat(),
        "ttl_expires_at": (
            now + timedelta(days=ttl_days_for(transport))
        ).isoformat(),
    }
    if tools:
        updates["declared_tools"] = tools

    if existing is not None:
        await repo.update(existing.server_id, updates)
        return

    record = McpServerRecord(
        name=item.name,
        source_url=source_url or f"user-test:{item.item_id}",
        # GitHub-sourced servers run locally (npx/uvx from the repo),
        # matching the ingestion convention; only plain http(s) endpoints
        # without a repository identity are remote.
        transport=Transport(transport),
        endpoint_url=source_url or None,
        description=item.description or "",
        registry_name=registry_names[0] if registry_names else None,
        repository_url=source_url or None,
        declared_tools=tools or [],
        auth_required=via_auth,
        status=ServerStatus.VERIFIED,
        quality_score=90,
        confidence=Confidence.HIGH,
        invocation_verified=True,
        verified_badge=True,
        verified_via_auth=via_auth,
        last_verified_via="user_test",
        last_verified_at=now,
        ttl_expires_at=now + timedelta(days=ttl_days_for(transport)),
    )
    # create() dedupes by source_url and returns the pre-existing row
    # when one matches - update whichever row is now authoritative.
    persisted = await repo.create(record)
    await repo.update(persisted.server_id, updates)
