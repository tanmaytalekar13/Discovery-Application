"""Application search coordinator: DB-first discovery.

Target architecture (single source of truth per subsystem):

    USER SEARCH
        |
        +-- ArcadeDB catalog (official + verified servers = durable truth)
        |
        +-- external discovery (MCP Registry -> GitHub cascade)
                only when the DB shortlist is smaller than
                search_verified_result_cap, and stopped as soon as the
                DB can answer on its own

The DB is the persistent knowledge of reliable MCP servers; external
sources are a completion mechanism, never the primary answer when the
catalog already has enough. Every live candidate still passes the full
Phase 10 validation (MCP resolution / best-effort evidence rules)
before it can appear in a result, and live-approved items are merged
into the response WITHOUT being marked as trusted catalog content -
only a real successful test/verification run persists a verified
server (verification.bridge / verification workers).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, replace

from app.config import Settings
from app.models import Item
from app.normalization.pipeline import Phase10Pipeline, Phase10Result
from app.query.planner import PreferredType
from app.query.service import Phase11SearchResult, Phase11SearchService
from app.search.orchestrator import DiscoveryOrchestrator, SearchOrchestratorResult
from app.verification.bridge import verified_catalog_keys

_OFFICIAL_PROVIDER = "official_connectors"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApplicationSearchMetadata:
    mode: str
    sources_attempted: tuple[str, ...] = ()
    sources_succeeded: tuple[str, ...] = ()
    sources_failed: tuple[str, ...] = ()
    cached_results: int = 0
    live_candidates: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    # True when the response was served from the durable ArcadeDB catalog
    # (official + verified servers) because live discovery failed or
    # returned nothing. The DB is the source of truth; external sources
    # only update it, so an outage must never blank the UI.
    db_fallback: bool = False


@dataclass(frozen=True)
class ApplicationSearchResult:
    ranked: Phase11SearchResult
    metadata: ApplicationSearchMetadata


class ApplicationSearchService:
    """DB-first search coordinator with external discovery as a completion mechanism."""

    def __init__(
        self,
        *,
        settings: Settings,
        phase11: Phase11SearchService,
        orchestrator: DiscoveryOrchestrator | None = None,
        phase10_pipeline: Phase10Pipeline | None = None,
    ) -> None:
        self._settings = settings
        self._phase11 = phase11
        self._orchestrator = orchestrator
        self._phase10_pipeline = phase10_pipeline

    async def search(
        self,
        query: str,
        *,
        item_type: PreferredType | None = None,
        limit: int | None = None,
    ) -> ApplicationSearchResult:
        mode = self._settings.discovery_mode
        if mode == "cached":
            # Explicit DB-only deployment: external discovery never runs.
            cached = await self._phase11.search(query, item_type=item_type, limit=limit)
            servable = await self._servable_cached_results(
                [entry.item for entry in cached.results]
            )
            ranked = self._limit_entries(cached, servable, limit)
            return ApplicationSearchResult(
                ranked=Phase11SearchResult(plan=cached.plan, results=ranked),
                metadata=ApplicationSearchMetadata(
                    mode="cached",
                    sources_attempted=("arcadedb",),
                    sources_succeeded=("arcadedb",),
                    cached_results=len(ranked),
                ),
            )

        return await self._search_db_first(query, item_type=item_type, limit=limit)

    async def _search_db_first(
        self,
        query: str,
        *,
        item_type: PreferredType | None,
        limit: int | None,
    ) -> ApplicationSearchResult:
        """Answer from the durable catalog first; discover externally only to fill.

        Early stop: when the DB already holds at least
        `search_verified_result_cap` servable (official or verified-badge)
        hits for this query, external discovery is never started. The cap
        is the application's own existing shortlist-size requirement
        ("1 official + 2-3 verified"), not an invented threshold.

        When the DB is short, discovery runs with the shortfall
        (`min_results`) so the registry -> GitHub cascade inside
        MCPDiscoveryAdapter keeps pulling until the shortlist is full -
        and GitHub is only queried when the registry alone is not enough.

        The merged shortlist is deduplicated on the existing canonical
        identity strategy and capped. If live discovery approves nothing,
        the best catalog matches serve stale-but-available instead of
        leaving the user with a blank page.
        """
        cap = min(
            limit or self._settings.search_cold_miss_result_cap,
            self._settings.search_cold_miss_result_cap,
        )
        cached = await self._phase11.search(
            query, item_type=item_type, limit=self._settings.ranking_candidate_limit
        )
        # Servability gate: the catalog stores every reliability-approved
        # discovery hit, but only servers whose McpServer record holds a
        # verified badge - plus the seeded official directory - belong in
        # the search shortlist. Without this, registry metadata alone
        # (never handshaked, never badge-earned) would surface as a result
        # next to genuinely verified ones.
        servable = await self._servable_cached_results(
            [entry.item for entry in cached.results]
        )
        verified_db = [
            entry for entry in cached.results if _cache_key(entry.item) in servable
        ]

        # Early stop: enough reliable DB results -> external discovery is
        # not required at all.
        if len(verified_db) >= self._settings.search_verified_result_cap:
            ranked = await self._rank_items(
                query,
                [entry.item for entry in verified_db],
                item_type=item_type,
                limit=cap,
                plan=cached.plan,
            )
            return ApplicationSearchResult(
                ranked=ranked,
                metadata=ApplicationSearchMetadata(
                    mode="merged",
                    sources_attempted=("arcadedb",),
                    sources_succeeded=("arcadedb",),
                    cached_results=len(ranked.results),
                ),
            )

        # DB insufficient: fill the shortfall from the registry -> GitHub
        # cascade (GitHub only queried when the registry is not enough).
        shortfall = cap - len(verified_db)
        discovery, catalog = await self._run_live_discovery(
            query, item_type=item_type, limit=limit, min_results=shortfall
        )
        live_items = list(catalog.approved) if catalog is not None else []

        merged = _merge_items([entry.item for entry in verified_db], live_items)
        if merged:
            ranked = await self._rank_items(
                query,
                merged,
                item_type=item_type,
                limit=cap,
                plan=cached.plan,
            )
            # Count what the shortlist actually shows: rows that came from
            # the catalog vs rows only live discovery produced. Match on
            # the same identity keys the merge used - matching
            # canonical_id alone is wrong when several catalog rows carry
            # different canonical_id values for the same server while the
            # merge collapsed them under one key.
            cached_identity_keys = {
                item.canonical_id or str(item.item_id)
                for item in (entry.item for entry in verified_db)
            }
            cached_in_results = sum(
                1
                for entry in ranked.results
                if (entry.item.canonical_id or str(entry.item.item_id))
                in cached_identity_keys
            )
            return ApplicationSearchResult(
                ranked=ranked,
                metadata=self._discovery_metadata(
                    discovery,
                    catalog,
                    mode="merged",
                    cached_results=cached_in_results,
                    # Discovery attempted but validated nothing: the
                    # response is the durable catalog, not live content.
                    db_fallback=catalog is None or not catalog.approved,
                    sources_attempted_prefix=("arcadedb",),
                    sources_succeeded_prefix=("arcadedb",),
                ),
            )

        # Cold miss: discovery ran and approved nothing. Serve the best
        # servable catalog matches (stale-but-available) - an external
        # outage must degrade to the durable catalog, never to an empty
        # page.
        fallback = self._limit_entries(cached, servable, cap)
        ranked = await self._rank_items(
            query,
            [entry.item for entry in fallback],
            item_type=item_type,
            limit=cap,
            plan=cached.plan,
        )
        return ApplicationSearchResult(
            ranked=ranked,
            metadata=self._discovery_metadata(
                discovery,
                catalog,
                mode="merged",
                db_fallback=True,
                cached_results=len(ranked.results),
                sources_attempted_prefix=("arcadedb",),
                # The DB genuinely served the response (the fallback rows
                # are its answer), so the catalog counts as a succeeded
                # source even though the external sources failed.
                sources_succeeded_prefix=("arcadedb",),
            ),
        )

    async def _servable_cached_results(self, items: list[Item]) -> set[str]:
        """Identity keys of cached items allowed into the shortlist.

        Verified per the McpServer registry badge, plus every official
        connector (vendor directory = first-party evidence; the live
        protocol check still happens at connect time).
        """
        keys = await verified_catalog_keys(items)
        return {
            _cache_key(item)
            for item in items
            if _cache_key(item) in keys or _is_official(item)
        }

    @staticmethod
    def _limit_entries(cached, servable_keys: set[str], limit: int | None):
        """Cached RankedItems whose item key is servable, capped."""
        entries = [
            entry
            for entry in cached.results
            if _cache_key(entry.item) in servable_keys
        ]
        return entries[: limit or len(entries)]

    async def _run_live_discovery(
        self,
        query: str,
        *,
        item_type: PreferredType | None,
        limit: int | None,
        min_results: int,
    ) -> tuple[SearchOrchestratorResult | None, Phase10Result | None]:
        if self._orchestrator is None or self._phase10_pipeline is None:
            return None, None
        try:
            return await self._orchestrator.discover_and_catalog(
                query,
                phase10_pipeline=self._phase10_pipeline,
                item_type=item_type or "all",
                max_results=limit or self._settings.discovery_max_results_per_source,
                min_results=max(min_results, 1),
            )
        except Exception:  # noqa: BLE001 - discovery outage must never fail search
            # Sources report their own failures via sources_failed; anything
            # reaching here is an unexpected outage. The DB catalog remains
            # the source of truth, so the caller serves the stored
            # official + verified servers instead of raising.
            logger.warning(
                "Live discovery failed for query %r; falling back to the "
                "durable DB catalog",
                query,
                exc_info=True,
            )
            return None, None

    async def _rank_items(
        self,
        query: str,
        items,
        *,
        item_type: PreferredType | None,
        limit: int | None,
        plan=None,
    ) -> Phase11SearchResult:
        return await self._phase11.rank_catalog_items(
            query,
            list(items),
            item_type=item_type,
            limit=limit,
            plan=plan,
        )

    @staticmethod
    def _discovery_metadata(
        discovery: SearchOrchestratorResult | None,
        catalog: Phase10Result | None,
        *,
        mode: str,
        cached_results: int = 0,
        db_fallback: bool = False,
        sources_attempted_prefix: tuple[str, ...] = (),
        sources_succeeded_prefix: tuple[str, ...] = (),
    ) -> ApplicationSearchMetadata:
        """Metadata for a search that (attempted) external discovery."""
        attempted = discovery.sources_attempted if discovery else ()
        succeeded = discovery.sources_succeeded if discovery else ()
        failed = discovery.sources_failed if discovery else ()
        return ApplicationSearchMetadata(
            mode=mode,
            sources_attempted=(*sources_attempted_prefix, *attempted),
            sources_succeeded=(*sources_succeeded_prefix, *succeeded),
            sources_failed=failed,
            cached_results=cached_results,
            live_candidates=len(discovery.candidates) if discovery else 0,
            approved_count=len(catalog.approved) if catalog else 0,
            rejected_count=len(catalog.rejected) if catalog else 0,
            db_fallback=db_fallback,
        )


def _is_official(item: Item) -> bool:
    """A vendor-published official connector (seeded official directory)."""
    return any(
        getattr(source, "provider", None) == _OFFICIAL_PROVIDER
        for source in (item.provenance or [])
    )


def _cache_key(item: Item) -> str:
    """The identity key used across catalog shortlist, badge gate and merge."""
    return item.canonical_id or str(item.item_id)


def _item_repo_identities(item: Item) -> set[str]:
    """Canonical owner/repo identities for one item's URLs.

    Reuses `verification.ingestion.normalize_repo_identity` - the same
    owner/repo normalizer the verification bridge and badge lookups use -
    so deduplication and verification can never disagree about what
    "the same repository" means.
    """
    from app.verification.ingestion import normalize_repo_identity

    urls: list[str] = []
    if item.source is not None and item.source.url:
        urls.append(str(item.source.url))
    urls.extend(str(s.url) for s in (item.provenance or []) if s.url)
    artifacts = getattr(item, "artifacts", None)
    if artifacts is not None and artifacts.source_url:
        urls.append(str(artifacts.source_url))
    return {
        identity
        for identity in (normalize_repo_identity(url) for url in urls)
        if identity
    }


def _merge_items(cached: list[Item], live: list[Item]) -> list[Item]:
    """Deduplicate DB + live items into one unique, ordered shortlist.

    Primary key: the existing canonical identity (`canonical_id`, the
    protocol-aware sha256 from deduplication.identity; seeded rows carry
    their own stable ids). Secondary key: the canonical owner/repo
    identity from `normalize_repo_identity`, so a registry mirror and a
    GitHub repo describing the SAME repository collapse into one result
    while genuinely different servers stay distinct.

    Semantics: cached rows claim their keys first (stable order), live
    rows with the same canonical identity win the content of that key
    (fresher evidence) without adding a duplicate row. A live row that
    matches only via repository identity never replaces the durable
    catalog row. Order: cached originals first, then live items the
    catalog never had.
    """
    merged: OrderedDict[str, Item] = OrderedDict()
    repo_owner: dict[str, str] = {}  # repo identity -> owning merged key

    def _claim(item: Item, *, prefer_new: bool) -> None:
        key = _cache_key(item)
        identities = _item_repo_identities(item)
        for identity in identities:
            owner = repo_owner.get(identity)
            if owner is not None:
                # Same repository already merged. The durable catalog row
                # stays (the DB is the source of truth); only an exact
                # canonical-identity match may refresh the content below.
                return
        if key in merged:
            if prefer_new:
                merged[key] = item
            return
        merged[key] = item
        for identity in identities:
            repo_owner.setdefault(identity, key)

    for item in cached:
        _claim(item, prefer_new=False)
    for item in live:
        _claim(item, prefer_new=True)
    return list(merged.values())
