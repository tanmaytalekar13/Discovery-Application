from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db.repositories import ItemRepository
from app.query.embeddings import LocalEmbeddingModel
from app.query.planner import PreferredType, QueryPlan, plan_query
from app.query.ranking import RankedItem, RankingWeights, rank_items


@dataclass(frozen=True)
class Phase11SearchResult:
    plan: QueryPlan
    results: tuple[RankedItem, ...]


class Phase11SearchService:
    """Phase 11 query planning, local embeddings, and ranking boundary."""

    def __init__(
        self,
        repository: ItemRepository,
        settings: Settings,
        *,
        embedder: LocalEmbeddingModel | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._embedder = embedder or LocalEmbeddingModel(settings.embedding_dimensions)

    async def search(
        self,
        query: str,
        *,
        item_type: PreferredType | None = None,
        limit: int | None = None,
    ) -> Phase11SearchResult:
        plan = await plan_query(query, self._settings, preferred_type=item_type)
        effective_type = item_type or plan.preferred_type
        candidates = await self._repository.search_rankable(
            item_type=effective_type,
            keywords=plan.keywords,
            limit=self._settings.ranking_candidate_limit,
        )

        for item in candidates:
            expected_dimensions = self._settings.embedding_dimensions
            if (
                item.embedding is None
                or len(item.embedding) != expected_dimensions
                or all(v == 0.0 for v in item.embedding)
            ):
                item.embedding = self._embedder.embed_item(item)
                await self._repository.update_embedding(item.item_id, item.embedding)

        weights = RankingWeights(
            relevance=self._settings.ranking_relevance_weight,
            reliability=self._settings.ranking_reliability_weight,
            freshness=self._settings.ranking_freshness_weight,
            evidence=self._settings.ranking_evidence_weight,
        )
        ranked = rank_items(
            plan.expanded_query,
            candidates,
            embedder=self._embedder,
            weights=weights,
        )
        result_limit = limit or self._settings.discovery_max_results_per_source
        return Phase11SearchResult(plan=plan, results=tuple(ranked[:result_limit]))

    async def rank_catalog_items(
        self,
        query: str,
        items: list,
        *,
        item_type: PreferredType | None = None,
        limit: int | None = None,
        plan: QueryPlan | None = None,
    ) -> Phase11SearchResult:
        """Rank an already selected catalog item set through Phase 11 scoring."""
        plan = plan or await plan_query(query, self._settings, preferred_type=item_type)
        effective_type = item_type or plan.preferred_type
        candidates = [
            item
            for item in items
            if effective_type == "all" or item.type.value == effective_type
        ]

        # Regenerate zero-vector embeddings so that items seeded before the
        # zero-vector guard was added (or accepted without a live MCP protocol
        # resolution) are still ranked by semantic relevance.
        for item in candidates:
            if (
                item.embedding is None
                or len(item.embedding) != self._embedder.dimensions
                or all(v == 0.0 for v in item.embedding)
            ):
                item.embedding = self._embedder.embed_item(item)
                await self._repository.update_embedding(item.item_id, item.embedding)

        weights = RankingWeights(
            relevance=self._settings.ranking_relevance_weight,
            reliability=self._settings.ranking_reliability_weight,
            freshness=self._settings.ranking_freshness_weight,
            evidence=self._settings.ranking_evidence_weight,
        )
        ranked = rank_items(
            plan.expanded_query,
            candidates,
            embedder=self._embedder,
            weights=weights,
        )
        result_limit = limit or self._settings.discovery_max_results_per_source
        return Phase11SearchResult(plan=plan, results=tuple(ranked[:result_limit]))
