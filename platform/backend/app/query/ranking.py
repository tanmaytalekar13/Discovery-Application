from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import Item
from app.query.embeddings import LocalEmbeddingModel, cosine_similarity


@dataclass(frozen=True)
class RankingWeights:
    relevance: float = 0.45
    reliability: float = 0.35
    freshness: float = 0.10
    evidence: float = 0.10


@dataclass(frozen=True)
class RankedItem:
    item: Item
    final_score: float
    relevance: float
    reliability: float
    freshness: float
    evidence: float


def rank_items(
    query: str,
    items: list[Item] | tuple[Item, ...],
    *,
    embedder: LocalEmbeddingModel,
    weights: RankingWeights = RankingWeights(),
    now: datetime | None = None,
) -> list[RankedItem]:
    query_vector = embedder.embed_text(query)
    ranked: list[RankedItem] = []

    for item in items:
        item_vector = item.embedding or embedder.embed_item(item)
        relevance = cosine_similarity(query_vector, item_vector)
        reliability = item.reliability.score
        freshness = _freshness_score(item, now=now)
        evidence = _evidence_score(item)
        final_score = (
            weights.relevance * relevance
            + weights.reliability * reliability
            + weights.freshness * freshness
            + weights.evidence * evidence
        )
        ranked.append(
            RankedItem(
                item=item,
                final_score=round(final_score, 6),
                relevance=round(relevance, 6),
                reliability=round(reliability, 6),
                freshness=round(freshness, 6),
                evidence=round(evidence, 6),
            )
        )

    return sorted(ranked, key=lambda result: result.final_score, reverse=True)


def _freshness_score(item: Item, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    last_seen = item.discovery.last_seen

    # Normalize naive timestamps to UTC-aware timestamps.
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    else:
        last_seen = last_seen.astimezone(timezone.utc)

    age_days = max(
        0.0,
        (current - last_seen).total_seconds() / 86400,
    )

    return max(
        0.0,
        min(1.0, 1.0 - age_days / 90.0),
    )

def _evidence_score(item: Item) -> float:
    evidence_keys = {
        f"{entry.source.type.value}|{entry.source.id}|{entry.kind}|{entry.statement}"
        for entry in item.evidence
    }
    source_keys = {
        f"{source.type.value}|{source.id}|{source.url}|{source.provider}"
        for source in item.provenance
    }
    return min(1.0, (len(evidence_keys) + len(source_keys)) / 6.0)
