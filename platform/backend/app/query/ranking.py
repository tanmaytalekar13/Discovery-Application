from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from app.models import Item
from app.query.embeddings import LocalEmbeddingModel, cosine_similarity


# The official Registry receives a preference only after it has earned a good
# relevance and quality score. GitHub remains the sole fallback source.
SOURCE_PRIORITY_MULTIPLIER: dict[str, float] = {
    "MCP Registry": 1.25,
    "GitHub": 1.0,
    # Default for unknown providers
    "": 1.0,
}

_QUERY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_QUERY_NOISE = {
    "mcp",
    "model",
    "context",
    "protocol",
    "server",
    "servers",
    "tool",
    "tools",
    "agent",
    "agents",
    "the",
    "a",
    "an",
    "for",
    "with",
}


def _source_priority(item: Item) -> float:
    """Return the priority multiplier for an item's primary source."""
    # Use the first provenance entry as the primary source
    if item.provenance:
        provider = getattr(item.provenance[0], "provider", None) or ""
    else:
        provider = ""
    return SOURCE_PRIORITY_MULTIPLIER.get(provider, 1.0)


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
    source_priority: float


def rank_items(
    query: str,
    items: list[Item] | tuple[Item, ...],
    *,
    embedder: LocalEmbeddingModel,
    weights: RankingWeights = RankingWeights(),
    now: datetime | None = None,
) -> list[RankedItem]:
    query_vector = embedder.embed_text(query)
    query_terms = _meaningful_query_terms(query)
    ranked: list[RankedItem] = []
    lexical_scores = {
        str(item.item_id): _lexical_relevance(query_terms, item) for item in items
    }
    has_strong_match = any(score >= 0.9 for score in lexical_scores.values())

    for item in items:
        lexical_relevance = lexical_scores[str(item.item_id)]
        # Once the catalog contains an all-service-term result, do not dilute
        # the response with partial matches such as Gmail for "Google Calendar".
        # Partial/semantic matches remain available as a fallback when there is
        # no direct service result at all.
        if has_strong_match and 0.0 < lexical_relevance < 0.9:
            continue
        item_vector = item.embedding or embedder.embed_item(item)
        semantic_relevance = cosine_similarity(query_vector, item_vector)
        # Exact service/name/source matches are deterministic and should not be
        # diluted by hash-embedding collisions. Semantic similarity remains a
        # useful fallback for natural-language searches.
        relevance = max(semantic_relevance, lexical_relevance)
        reliability = item.reliability.score
        freshness = _freshness_score(item, now=now)
        evidence = _evidence_score(item)
        priority = _source_priority(item)
        quality_score = (
            weights.relevance * relevance
            + weights.reliability * reliability
            + weights.freshness * freshness
            + weights.evidence * evidence
        )
        # Relevance is the qualification and primary ordering signal. A full
        # Google Calendar match (10 points) must always beat an item that only
        # mentions Google, regardless of source, rating, freshness, or stars.
        # The official registry is preferred only for results that genuinely
        # match the query and have a solid quality score. This avoids a source
        # label pushing a weak or unrelated registry entry above a strong
        # GitHub result, while reliably breaking close, good-result ties in
        # favour of the official record.
        official_registry_bonus = (
            0.15
            if priority > 1.0 and lexical_relevance >= 0.5 and quality_score >= 0.6
            else 0.0
        )
        final_score = lexical_relevance * 10.0 + quality_score + official_registry_bonus
        ranked.append(
            RankedItem(
                item=item,
                final_score=round(final_score, 6),
                relevance=round(relevance, 6),
                reliability=round(reliability, 6),
                freshness=round(freshness, 6),
                evidence=round(evidence, 6),
                source_priority=priority,
            )
        )

    return sorted(ranked, key=lambda result: result.final_score, reverse=True)


def _meaningful_query_terms(query: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token
            for token in _QUERY_TOKEN_PATTERN.findall(query.lower())
            if token not in _QUERY_NOISE
        )
    )


def _lexical_relevance(query_terms: tuple[str, ...], item: Item) -> float:
    """Return a service-focused lexical score in [0, 1].

    Names, repository/server IDs and descriptions are deliberately included so
    GitHub repositories and Registry servers use the same relevance rule.
    """
    if not query_terms:
        return 0.0
    fields = [item.name, item.description, item.source.id]
    if item.tool is not None:
        fields.extend((item.tool.server_id, item.tool.tool_name))
    fields.extend(source.id for source in item.provenance)
    text = " ".join(field for field in fields if field).lower()
    matched = sum(1 for term in query_terms if term in text)
    if not matched:
        return 0.0
    coverage = matched / len(query_terms)
    phrase = " ".join(query_terms)
    compact_phrase = "-".join(query_terms)
    # A phrase in a server/repository name is a stronger signal than scattered
    # words in a long README-derived description.
    identity = " ".join(
        part
        for part in (
            item.name,
            item.source.id,
            item.tool.server_id if item.tool else "",
        )
        if part
    ).lower()
    if phrase in identity or compact_phrase in identity:
        return 1.0
    if coverage == 1.0:
        return 0.9
    return round(coverage * 0.5, 6)


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
