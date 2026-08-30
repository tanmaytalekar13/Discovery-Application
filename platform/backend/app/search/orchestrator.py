"""Multi-source discovery orchestrator (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 39 (Phase 09 Definition of Done):

    - sources execute concurrently;
    - one source can fail without breaking search;
    - candidates aggregate correctly.

and Section 7's architecture diagram, this is the top-level
`DiscoveryOrchestrator` that runs `MCPDiscoveryAdapter` and
`A2ADiscoveryAdapter` (each of which already fans out to its own
sources concurrently - see `app.search.mcp_adapter` /
`app.search.a2a_adapter`) at the same time, and flattens their
per-source `SourceOutcome`s into the aggregate result shape used by
Section 28's search response `metadata`
(`sources_attempted`/`sources_succeeded`/`sources_failed`).

What this module deliberately does *not* do yet, because it belongs to
a later phase and Section 44's Codex protocol says "Implement only
that phase":

    - query ArcadeDB / decide cold vs warm search (Phase 17/Section 17-18);
    - MCP `initialize`/`tools/list` or Agent Card protocol validation
      of the aggregated candidates (already partly done inside the
      Well-Known source, Section 9/10 for the rest - Phase 10);
    - deduplication, reliability scoring, or persistence (Phase 10);
    - ranking (Phase 11).

Live discovery here only ever aggregates untrusted candidates; nothing
in this module writes to ArcadeDB or is treated as an approved catalog
entry (rule #7/#8).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.discovery.common.candidate import CandidateReference
from app.search.a2a_adapter import A2ADiscoveryAdapter
from app.search.mcp_adapter import MCPDiscoveryAdapter

DEFAULT_MAX_RESULTS = 20


@dataclass(frozen=True)
class SearchOrchestratorResult:
    """Aggregated output of one orchestrator run for one query."""

    candidates: tuple[CandidateReference, ...]

    sources_attempted: tuple[str, ...]
    sources_succeeded: tuple[str, ...]
    sources_failed: tuple[str, ...]

    # Keyed by the same source names as `sources_failed`, for
    # observability (Section 36) without ever logging secret values.
    source_errors: dict[str, str] = field(default_factory=dict)

    @property
    def tool_candidates(self) -> tuple[CandidateReference, ...]:
        return tuple(c for c in self.candidates if c.protocol == "mcp")

    @property
    def agent_candidates(self) -> tuple[CandidateReference, ...]:
        return tuple(c for c in self.candidates if c.protocol == "a2a")


class DiscoveryOrchestrator:
    """Fan a query out to the MCP and A2A discovery adapters concurrently."""

    def __init__(
        self,
        *,
        mcp_adapter: MCPDiscoveryAdapter | None = None,
        a2a_adapter: A2ADiscoveryAdapter | None = None,
    ) -> None:
        if mcp_adapter is None and a2a_adapter is None:
            raise ValueError(
                "DiscoveryOrchestrator requires at least one of "
                "mcp_adapter/a2a_adapter to be configured"
            )

        self._mcp_adapter = mcp_adapter
        self._a2a_adapter = a2a_adapter

    async def discover(
        self,
        query: str,
        item_type: str = "all",
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> SearchOrchestratorResult:
        """Run every enabled protocol group concurrently for `query`.

        `item_type` mirrors Section 28's `GET /api/search` contract
        (``all | tool | agent``): it only decides *which* protocol
        group(s) run, never filters an individual source's own
        classification.
        """
        if not query.strip():
            raise ValueError("Discovery query must not be empty")
        if item_type not in ("all", "tool", "agent"):
            raise ValueError("item_type must be 'all', 'tool', or 'agent'")

        want_mcp = item_type in ("all", "tool") and self._mcp_adapter is not None
        want_a2a = item_type in ("all", "agent") and self._a2a_adapter is not None

        mcp_task = (
            asyncio.ensure_future(self._mcp_adapter.discover(query, max_results))
            if want_mcp
            else None
        )
        a2a_task = (
            asyncio.ensure_future(self._a2a_adapter.discover(query, max_results))
            if want_a2a
            else None
        )

        mcp_outcomes = await mcp_task if mcp_task is not None else []
        a2a_outcomes = await a2a_task if a2a_task is not None else []

        candidates: list[CandidateReference] = []
        sources_attempted: list[str] = []
        sources_succeeded: list[str] = []
        sources_failed: list[str] = []
        source_errors: dict[str, str] = {}

        for prefix, outcomes in (("mcp", mcp_outcomes), ("a2a", a2a_outcomes)):
            for outcome in outcomes:
                name = f"{prefix}:{outcome.source}"
                sources_attempted.append(name)

                if outcome.succeeded:
                    sources_succeeded.append(name)
                    candidates.extend(outcome.candidates)
                else:
                    sources_failed.append(name)
                    if outcome.error:
                        source_errors[name] = outcome.error

        return SearchOrchestratorResult(
            candidates=tuple(candidates),
            sources_attempted=tuple(sources_attempted),
            sources_succeeded=tuple(sources_succeeded),
            sources_failed=tuple(sources_failed),
            source_errors=source_errors,
        )
