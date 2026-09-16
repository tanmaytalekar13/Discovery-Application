"""Scoring engine and status buckets (spec v2 Section 8).

Never a binary drop: hard-fail checks score 0, everything else earns
partial credit across weighted soft checks. `quality_score` (0-100)
and `confidence` are tracked separately - a remote server verified
with only 1 of 3 planned attempts can score high but still be
low-confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.verification.models import Confidence, ServerStatus

# ---------------------------------------------------------------------------
# Weights (Section 8 table). Total soft-check weight = 100 before latency.
# ---------------------------------------------------------------------------

WEIGHT_INSTALL_CONNECT = 25
WEIGHT_HANDSHAKE = 15
WEIGHT_TOOLS_LIST = 15
WEIGHT_TOOL_INVOCATION = 25
WEIGHT_SEMANTIC = 15
WEIGHT_MALFORMED_INPUT = 5

# Latency is a scoring factor, not a rejection criterion (Section 8).
LATENCY_PENALTY = {"fast": 0, "moderate": 3, "slow": 8}

# TTLs (Section 11): remote servers are more volatile.
TTL_DAYS_LOCAL = 30
TTL_DAYS_REMOTE = 7

# Attempts for remote verification spread over time (Section 5.2/6).
REMOTE_ATTEMPTS_PLANNED = 3

# Score-bucket boundaries (configurable; observability Section 12 says the
# `verified` threshold should ultimately be validated against the real
# score distribution - these defaults follow the spec's table).
VERIFIED_MIN_SCORE = 80
REVIEW_MAX_SCORE = 79


@dataclass
class SoftCheckResults:
    """Raw 0..1 results per soft check, plus hard-fail flags."""

    install_connect: float | None = None   # 0..1 (None = not applicable / not attempted)
    handshake_valid: float | None = None
    tools_list_schema: float | None = None
    tool_invocation: float | None = None   # success rate across invoked tools
    semantic_validity: float | None = None
    no_crash_malformed_input: float | None = None
    security_penalty: float = 0.0          # 0..1 fraction of the security weight to deduct
    latency_category: str | None = None    # fast | moderate | slow
    hard_fail: bool = False                # handshake/install never completed
    auth_blocked: bool = False             # could not fully verify due to missing credentials
    invocation_attempted: bool = False     # at least one invocation attempt was made
    checks_run: int = 0                    # how many applicable checks produced evidence
    notes: list[str] = field(default_factory=list)


@dataclass
class ScoreOutcome:
    """The scoring engine's full decision."""

    quality_score: int
    confidence: Confidence
    status: ServerStatus
    hard_fail: bool = False
    breakdown: dict[str, float] = field(default_factory=dict)
    reason: str | None = None


def _confidence_from_attempts(attempts_completed: int, attempts_planned: int) -> Confidence:
    if attempts_planned <= 1:
        return Confidence.HIGH
    ratio = attempts_completed / attempts_planned
    if ratio >= 1.0:
        return Confidence.HIGH
    if ratio >= 0.5:
        return Confidence.MEDIUM
    return Confidence.LOW


def score_server(result: SoftCheckResults, *, attempts_completed: int = 1,
                 attempts_planned: int = 1) -> ScoreOutcome:
    """Turn per-check results into (score, confidence, status)."""
    # ---- hard fail (Section 8): score 0, no partial credit ---------------
    if result.hard_fail:
        return ScoreOutcome(
            quality_score=0,
            confidence=_confidence_from_attempts(attempts_completed, attempts_planned),
            status=ServerStatus.REJECTED,
            hard_fail=True,
            reason="handshake/install never completed",
        )

    breakdown: dict[str, float] = {}

    def add(name: str, value: float | None, weight: int) -> None:
        if value is None:
            return
        breakdown[name] = round(max(0.0, min(1.0, value)) * weight, 2)

    add("install_connect", result.install_connect, WEIGHT_INSTALL_CONNECT)
    add("handshake_valid", result.handshake_valid, WEIGHT_HANDSHAKE)
    add("tools_list_schema", result.tools_list_schema, WEIGHT_TOOLS_LIST)
    add("tool_invocation", result.tool_invocation, WEIGHT_TOOL_INVOCATION)
    add("semantic_validity", result.semantic_validity, WEIGHT_SEMANTIC)
    add("no_crash_malformed_input", result.no_crash_malformed_input, WEIGHT_MALFORMED_INPUT)

    if result.latency_category:
        breakdown["latency_penalty"] = -LATENCY_PENALTY.get(result.latency_category, 0)

    if result.security_penalty > 0:
        breakdown["security_penalty"] = -round(result.security_penalty * 10, 2)

    raw = sum(breakdown.values())
    score = int(round(max(0, min(100, raw))))

    confidence = _confidence_from_attempts(attempts_completed, attempts_planned)

    # ---- status buckets (Section 8 table) --------------------------------
    if result.auth_blocked and not result.invocation_attempted:
        # Auth required and no credentials: structural checks only -
        # never a faked full pass (rule 8).
        status = ServerStatus.PARTIAL_VERIFIED
    elif score >= VERIFIED_MIN_SCORE and confidence is Confidence.HIGH:
        status = ServerStatus.VERIFIED
    elif score >= VERIFIED_MIN_SCORE and confidence is Confidence.LOW:
        status = ServerStatus.REVIEW  # needs another retry pass
    elif REVIEW_MAX_SCORE >= score >= 40:
        status = ServerStatus.REVIEW
    elif score < 40 and confidence is Confidence.HIGH:
        status = ServerStatus.REJECTED
    else:  # score < 40, low confidence
        status = ServerStatus.RETRY_PENDING

    return ScoreOutcome(
        quality_score=score,
        confidence=confidence,
        status=status,
        breakdown=breakdown,
    )


def ttl_days_for(transport: str) -> int:
    return TTL_DAYS_LOCAL if transport == "local" else TTL_DAYS_REMOTE
