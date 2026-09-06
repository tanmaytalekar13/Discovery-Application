from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.models import Item
from app.normalization.pipeline import Phase10Pipeline, Phase10Result
from app.query.planner import PreferredType
from app.query.service import Phase11SearchResult, Phase11SearchService
from app.search.orchestrator import DiscoveryOrchestrator, SearchOrchestratorResult


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
            return ApplicationSearchResult(
                ranked=cached,
                metadata=ApplicationSearchMetadata(
                    mode="cached",
                    sources_attempted=("arcadedb",),
                    sources_succeeded=("arcadedb",),
                    cached_results=len(cached.results),
                ),
            )

        if mode == "live":
            discovery, catalog = await self._run_live_discovery(query, item_type, limit)
            ranked = await self._rank_items(
                query,
                catalog.approved if catalog is not None else (),
                item_type=item_type,
                limit=limit,
            )
            return ApplicationSearchResult(
                ranked=ranked,
                metadata=self._live_metadata(discovery, catalog, mode="live"),
            )

        # Always use the full ranking pool (ranking_candidate_limit) for cached
        # items so that lower-priority sources (npm, awesome-list) are ranked
        # before the result slice. The user's limit applies only to the final
        # ranked output.
        cached = await self._phase11.search(
            query, item_type=item_type, limit=self._settings.ranking_candidate_limit
        )
        discovery, catalog = await self._run_live_discovery(query, item_type, limit)
        live_metadata = self._live_metadata(discovery, catalog, mode="merged")

        # A successful live discovery pass should not be masked by stale seed/demo
        # catalog rows. When live sources ran and produced no approved catalog
        # entries, the cached seed fallback is treated as stale for this query and
        # explicitly excluded from the merged response.
        cached_items = [ranked.item for ranked in cached.results]
        if discovery is not None and discovery.sources_succeeded and (
            catalog is None or not catalog.approved
        ):
            merged_items: list[Item] = []
            cached_result_count = 0
        else:
            merged_items = _merge_items(
                cached_items,
                list(catalog.approved if catalog is not None else ()),
            )
            cached_result_count = len(cached.results)

        ranked = await self._rank_items(
            query,
            merged_items,
            item_type=item_type,
            limit=limit,
            plan=cached.plan,
        )
        return ApplicationSearchResult(
            ranked=ranked,
            metadata=ApplicationSearchMetadata(
                mode="merged",
                sources_attempted=("arcadedb", *live_metadata.sources_attempted),
                sources_succeeded=("arcadedb", *live_metadata.sources_succeeded),
                sources_failed=live_metadata.sources_failed,
                cached_results=cached_result_count,
                live_candidates=live_metadata.live_candidates,
                approved_count=live_metadata.approved_count,
                rejected_count=live_metadata.rejected_count,
            ),
        )

    async def _run_live_discovery(
        self,
        query: str,
        item_type: PreferredType | None,
        limit: int | None,
    ) -> tuple[SearchOrchestratorResult | None, Phase10Result | None]:
        if self._orchestrator is None or self._phase10_pipeline is None:
            return None, None
        return await self._orchestrator.discover_and_catalog(
            query,
            phase10_pipeline=self._phase10_pipeline,
            item_type=item_type or "all",
            max_results=limit or self._settings.discovery_max_results_per_source,
        )

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
    ) -> ApplicationSearchMetadata:
        return ApplicationSearchMetadata(
            mode=mode,
            sources_attempted=discovery.sources_attempted if discovery else (),
            sources_succeeded=discovery.sources_succeeded if discovery else (),
            sources_failed=discovery.sources_failed if discovery else (),
            live_candidates=len(discovery.candidates) if discovery else 0,
            approved_count=len(catalog.approved) if catalog else 0,
            rejected_count=len(catalog.rejected) if catalog else 0,
        )


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
