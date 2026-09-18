from __future__ import annotations

import logging
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
    """Application search coordinator for the Phase 09 -> 10 -> 11 runtime path."""

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

        if mode == "live":
            discovery, catalog = await self._run_live_discovery(query, item_type, limit)
            live_approved = list(catalog.approved) if catalog is not None else []
            if live_approved:
                ranked = await self._rank_items(
                    query,
                    live_approved,
                    item_type=item_type,
                    limit=limit,
                )
                return ApplicationSearchResult(
                    ranked=ranked,
                    metadata=self._live_metadata(discovery, catalog, mode="live"),
                )
            # Live discovery failed or approved nothing. The durable catalog
            # is the source of truth for official + verified servers, so
            # serve those from ArcadeDB instead of an empty response.
            return await self._db_fallback_result(
                query,
                item_type=item_type,
                limit=limit,
                mode="live",
                discovery=discovery,
                catalog=catalog,
            )

        return await self._search_mixed(query, item_type=item_type, limit=limit)

    async def _search_mixed(
        self,
        query: str,
        *,
        item_type: PreferredType | None,
        limit: int | None,
    ) -> ApplicationSearchResult:
        """Warm-start search: catalog shortlist + live discovery, merged.

        Spec v2 Section 4.2 (Warm Search): a query already represented in the
        catalog is answered from ArcadeDB immediately, but live discovery
        still runs and its approved results are merged with the cached ones -
        one query should surface every server for that service across the
        catalog (vendor-published official connectors), the official MCP
        Registry, and GitHub (the plan's multi-source merge), not just the
        first catalog hit.

        Inside live discovery the registry -> GitHub trust cascade still
        holds (enforced in `MCPDiscoveryAdapter`): GitHub is only queried
        when the registry yields nothing. Ranking then orders the merge:
        `official_connectors` provenance outranks registry mirrors, which
        outrank GitHub repos, so the vendor's own endpoint lists first while
        community mirrors remain visible below it.

        The merged shortlist is capped (`search_cold_miss_result_cap`,
        default 7) to stay a shortlist, not a firehose. If neither the
        catalog nor live discovery produces a verifiable item, the best
        catalog matches are served stale-but-available rather than leaving
        the user with a blank page.
        """
        cached = await self._phase11.search(
            query, item_type=item_type, limit=self._settings.ranking_candidate_limit
        )
        # Cache gate (spec intent): the catalog stores every
        # reliability-approved discovery hit, but only servers whose
        # McpServer record holds a verified badge - plus the seeded official
        # directory - belong in the search shortlist. Without this, registry
        # metadata alone (never handshaked, never badge-earned) surfaces as
        # a "cached" result next to genuinely verified ones.
        cached_servable = await self._servable_cached_results(
            [entry.item for entry in cached.results]
        )
        verified = [
            entry for entry in cached.results if _cache_key(entry.item) in cached_servable
        ]

        discovery, catalog = await self._run_live_discovery(query, item_type, limit)
        live_metadata = self._live_metadata(discovery, catalog, mode="merged")
        live_items = list(catalog.approved) if catalog is not None else []
        merged_cap = self._settings.search_cold_miss_result_cap

        # Merge verified catalog entries with freshly approved live items.
        # `_merge_items` deduplicates on canonical identity and prefers the
        # live copy when both sources found the same server.
        cached_items = [entry.item for entry in verified]
        merged = _merge_items(cached_items, live_items)
        if merged:
            ranked = await self._rank_items(
                query,
                merged,
                item_type=item_type,
                limit=min(limit or merged_cap, merged_cap),
                plan=cached.plan,
            )
            # "cached" means: served from the catalog. The merge prefers the
            # live copy for servers BOTH sources found, but those items still
            # originate from the catalog, so they count as cached; only items
            # the catalog never had are purely live-discovered. Match on the
            # same identity key the merge used - matching canonical_id alone
            # is wrong here because different cached rows can carry different
            # canonical_id values for the same server while the merge deduped
            # them under ONE key (0-/1- sha256 collisions), which is exactly
            # what underreported 4 cached rows as 1.
            cached_identity_keys = {
                item.canonical_id or str(item.item_id) for item in cached_items
            }
            cached_in_results = sum(
                1
                for entry in ranked.results
                if (entry.item.canonical_id or str(entry.item.item_id))
                in cached_identity_keys
            )
            return ApplicationSearchResult(
                ranked=ranked,
                metadata=ApplicationSearchMetadata(
                    mode="merged",
                    sources_attempted=("arcadedb", *live_metadata.sources_attempted),
                    sources_succeeded=(
                        ("arcadedb", *live_metadata.sources_succeeded)
                        if cached.results
                        else live_metadata.sources_succeeded
                    ),
                    sources_failed=live_metadata.sources_failed,
                    cached_results=cached_in_results,
                    live_candidates=live_metadata.live_candidates,
                    approved_count=live_metadata.approved_count,
                    rejected_count=live_metadata.rejected_count,
                ),
            )

        # Cold miss with no live approval: serve the best verified catalog
        # matches (stale-but-available) instead of an empty response. When
        # live discovery approved nothing there is nothing badge-new to
        # merge, so the cap applies to the verified/official catalog
        # shortlist alone.
        fallback = self._limit_entries(cached, cached_servable, merged_cap)
        ranked = await self._rank_items(
            query,
            [entry.item for entry in fallback],
            item_type=item_type,
            limit=min(limit or merged_cap, merged_cap),
            plan=cached.plan,
        )
        return ApplicationSearchResult(
            ranked=ranked,
            metadata=ApplicationSearchMetadata(
                mode="merged",
                sources_attempted=("arcadedb", *live_metadata.sources_attempted),
                sources_succeeded=("arcadedb", *live_metadata.sources_succeeded),
                sources_failed=live_metadata.sources_failed,
                cached_results=len(ranked.results),
                live_candidates=live_metadata.live_candidates,
                approved_count=live_metadata.approved_count,
                rejected_count=live_metadata.rejected_count,
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
        item_type: PreferredType | None,
        limit: int | None,
    ) -> tuple[SearchOrchestratorResult | None, Phase10Result | None]:
        if self._orchestrator is None or self._phase10_pipeline is None:
            return None, None
        try:
            return await self._orchestrator.discover_and_catalog(
                query,
                phase10_pipeline=self._phase10_pipeline,
                item_type=item_type or "all",
                max_results=limit or self._settings.discovery_max_results_per_source,
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
    def _live_metadata(
        discovery: SearchOrchestratorResult | None,
        catalog: Phase10Result | None,
        *,
        mode: str,
        db_fallback: bool = False,
    ) -> ApplicationSearchMetadata:
        return ApplicationSearchMetadata(
            mode=mode,
            sources_attempted=discovery.sources_attempted if discovery else (),
            sources_succeeded=discovery.sources_succeeded if discovery else (),
            sources_failed=discovery.sources_failed if discovery else (),
            live_candidates=len(discovery.candidates) if discovery else 0,
            approved_count=len(catalog.approved) if catalog else 0,
            rejected_count=len(catalog.rejected) if catalog else 0,
            db_fallback=db_fallback,
        )

    async def _db_fallback_result(
        self,
        query: str,
        *,
        item_type: PreferredType | None,
        limit: int | None,
        mode: str,
        discovery: SearchOrchestratorResult | None,
        catalog: Phase10Result | None,
    ) -> ApplicationSearchResult:
        """Serve official + verified catalog servers when live discovery fails.

        The DB is the durable source of truth: every search already
        persists official connectors (Phase 10 upsert) and verified
        badges (user tests / verification workers write McpServer
        records), so an external outage must degrade to the catalog
        shortlist, never to an empty page. The gate is the same
        verified-badge + official-directory rule the merged path uses,
        so fallback results match what the warm path would serve.
        """
        cap = min(
            limit or self._settings.search_cold_miss_result_cap,
            self._settings.search_cold_miss_result_cap,
        )
        cached = await self._phase11.search(
            query, item_type=item_type, limit=self._settings.ranking_candidate_limit
        )
        servable = await self._servable_cached_results(
            [entry.item for entry in cached.results]
        )
        fallback = self._limit_entries(cached, servable, cap)
        ranked = await self._rank_items(
            query,
            [entry.item for entry in fallback],
            item_type=item_type,
            limit=cap,
            plan=cached.plan,
        )
        metadata = self._live_metadata(
            discovery, catalog, mode=mode, db_fallback=True
        )
        metadata = replace(
            metadata,
            sources_attempted=("arcadedb", *metadata.sources_attempted),
            sources_succeeded=(
                ("arcadedb", *metadata.sources_succeeded)
                if ranked.results
                else metadata.sources_succeeded
            ),
            cached_results=len(ranked.results),
        )
        return ApplicationSearchResult(ranked=ranked, metadata=metadata)


def _is_verified(item: Item) -> bool:
    """A catalog item is 'verified' when its protocol was actually validated.

    Phase 10 only attaches `protocol_validation` evidence to items whose MCP
    resolution (or best-effort source-backed acceptance) succeeded; candidates
    rejected during normalization never enter the catalog with it.

    Vendor-published official connectors (seeded from the curated directory,
    provider='official_connectors') count as verified provenance too: the
    vendor's own directory is first-party evidence that the endpoint exists
    and is the official one for that service. The live protocol check still
    happens on every Test Tool connect — this gate only controls whether the
    item may appear in the search shortlist, where official servers must
    surface above registry/GitHub mirrors (and before any live cascade).
    """
    if any(entry.kind == "protocol_validation" for entry in item.evidence):
        return True
    return any(
        entry.kind == "official_directory"
        and entry.source.provider == "official_connectors"
        for entry in item.evidence
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


def _merge_items(cached: list[Item], live: list[Item]) -> list[Item]:
    merged: dict[str, Item] = {}
    for item in cached:
        key = item.canonical_id or str(item.item_id)
        merged.setdefault(key, item)
    for item in live:
        key = item.canonical_id or str(item.item_id)
        merged[key] = item
    # Prefer live-approved results when discovery succeeded for the active query.
    # This keeps seeded demo entries in the system while preventing them from
    # dominating a real provider-backed search result set.
    ordered = list(live)
    for item in cached:
        key = item.canonical_id or str(item.item_id)
        if key not in {entry.canonical_id or str(entry.item_id) for entry in live}:
            ordered.append(item)
    return ordered
