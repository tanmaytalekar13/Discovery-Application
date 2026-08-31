from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from ipaddress import ip_address
from urllib.parse import urlparse
from uuid import uuid4

from app.config import Settings
from app.models import DiscoverySource, Item

SCORING_VERSION = "v1"


@dataclass(frozen=True)
class ReliabilityEvaluationResult:
    score: float
    confidence: float
    approved: bool
    signals: dict[str, float]
    reasons: list[str]
    security_validation: float


def _security_signal(source: DiscoverySource) -> tuple[float, str]:
    if source.url is None:
        return 0.5, "source has no URL; security validation is partial"
    parsed = urlparse(str(source.url))
    if parsed.username or parsed.password:
        return 0.0, "source URL contains embedded credentials"
    if parsed.scheme not in {"http", "https"}:
        return 0.0, "source URL uses an unsupported scheme"
    try:
        if parsed.hostname:
            ip = ip_address(parsed.hostname)
            if ip.is_loopback or ip.is_link_local or ip.is_multicast:
                return 0.5, "source targets a local/link-local address; deployment policy must allow it"
    except ValueError:
        pass
    return 1.0, "source URL passed basic security validation"


def evaluate(item: Item, settings: Settings | None = None, *, protocol_validated: bool = True, available: bool = True) -> ReliabilityEvaluationResult:
    threshold = settings.reliability_threshold if settings else 0.75
    security, security_reason = _security_signal(item.source)
    source_quality = {
        "mcp_registry": 1.0,
        "a2a_catalog": 0.9,
        "well_known": 0.95,
        "configured": 0.85,
        "github": 0.65,
        "web_search": 0.45,
        "web_page": 0.5,
    }.get(item.source.type.value, 0.4)
    provenance = min(1.0, max(0.0, len(item.provenance) / 3.0))
    freshness = 1.0
    if item.discovery.last_seen:
        age_days = max(0.0, (datetime.now(timezone.utc) - item.discovery.last_seen).total_seconds() / 86400)
        freshness = max(0.0, 1.0 - age_days / 90.0)
    protocol = 1.0 if protocol_validated else 0.0
    availability_signal = 1.0 if available else 0.0
    signals = {
        "protocol_validation": protocol,
        "availability": availability_signal,
        "provenance": provenance,
        "source_quality": source_quality,
        "freshness": freshness,
        "security_validation": security,
    }
    weights = {
        "protocol_validation": 0.30,
        "availability": 0.15,
        "provenance": 0.10,
        "source_quality": 0.15,
        "freshness": 0.10,
        "security_validation": 0.20,
    }
    score = round(sum(signals[k] * weights[k] for k in weights), 4)
    confidence = round(min(1.0, 0.5 + 0.1 * len(item.evidence) + 0.1 * len(item.provenance)), 4)
    reasons = [
        "protocol validation succeeded" if protocol_validated else "protocol validation did not succeed",
        "availability check succeeded" if available else "availability check was not confirmed",
        f"source quality={source_quality:.2f}",
        f"provenance sources={len(item.provenance)}",
        f"freshness={freshness:.2f}",
        security_reason,
    ]
    return ReliabilityEvaluationResult(score, confidence, score >= threshold, signals, reasons, security)


def apply_evaluation(item: Item, result: ReliabilityEvaluationResult, now: datetime | None = None) -> None:
    evaluated_at = now or datetime.now(timezone.utc)
    item.reliability.score = result.score
    item.reliability.confidence = result.confidence
    item.reliability.scoring_version = SCORING_VERSION
    item.reliability.last_evaluated = evaluated_at
    item.reliability.security_validation = result.security_validation
    item.reliability.signals = result.signals
    item.reliability.reasons = result.reasons
