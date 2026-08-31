from __future__ import annotations

from collections import OrderedDict
from typing import Iterable

from app.models import DiscoveryEvidence, DiscoverySource, Item
from app.deduplication.identity import canonical_identity


def merge_items(items: Iterable[Item]) -> list[Item]:
    grouped: OrderedDict[str, Item] = OrderedDict()
    for item in items:
        key = str(item.item_id)
        current = grouped.get(key)
        if current is None:
            item.provenance = list(_unique_sources([item.source, *item.provenance]))
            item.evidence = list(_unique_evidence(item.evidence))
            grouped[key] = item
            continue
        current.provenance = list(_unique_sources([*current.provenance, item.source, *item.provenance]))
        current.evidence = list(_unique_evidence([*current.evidence, *item.evidence]))
        if len(item.description) > len(current.description):
            current.description = item.description
        if item.version and not current.version:
            current.version = item.version
        if item.artifacts.source_available:
            current.artifacts = item.artifacts
    return list(grouped.values())


def _unique_sources(sources: Iterable[DiscoverySource]) -> list[DiscoverySource]:
    result: list[DiscoverySource] = []
    seen: set[str] = set()
    for source in sources:
        key = f"{source.type.value}|{source.id}|{source.url}|{source.provider}"
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _unique_evidence(evidence: Iterable[DiscoveryEvidence]) -> list[DiscoveryEvidence]:
    result: list[DiscoveryEvidence] = []
    seen: set[str] = set()
    for entry in evidence:
        if str(entry.evidence_id) not in seen:
            seen.add(str(entry.evidence_id))
            result.append(entry)
    return result
