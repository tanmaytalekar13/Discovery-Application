"""Multi-source discovery orchestrator.

The orchestrator fans a user query out to the MCP discovery adapter and
flattens its per-source `SourceOutcome`s into the aggregate result shape
used by the search response metadata
(`sources_attempted`/`sources_succeeded`/`sources_failed`).

What this module deliberately does *not* do:

    - query ArcadeDB / decide cold vs warm search (the DB-first gate
      lives in `app.search.application`);
    - MCP `initialize`/`tools/list` protocol validation of the
      aggregated candidates (Phase 10 normalization);
    - reliability scoring or persistence decisions (Phase 10);
    - ranking (Phase 11).

Live discovery here only ever aggregates untrusted candidates; nothing
in this module writes to ArcadeDB or is treated as an approved catalog
entry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.discovery.common.candidate import CandidateReference
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
    # observability without ever logging secret values.
    source_errors: dict[str, str] = field(default_factory=dict)

    @property
    def tool_candidates(self) -> tuple[CandidateReference, ...]:
        return tuple(c for c in self.candidates if c.protocol == "mcp")

    @property
    def agent_candidates(self) -> tuple[CandidateReference, ...]:
        return tuple(c for c in self.candidates if c.protocol == "a2a")


class DiscoveryOrchestrator:
    """Run the MCP discovery adapter and flatten its per-source outcomes."""

    def __init__(
        self,
        *,
        mcp_adapter: MCPDiscoveryAdapter,
    ) -> None:
        if mcp_adapter is None:
            raise ValueError(
                "DiscoveryOrchestrator requires an mcp_adapter"
            )

        self._mcp_adapter = mcp_adapter

    async def discover_and_catalog(
        self,
        query: str,
        *,
        phase10_pipeline,
        item_type: str = "all",
        max_results: int = DEFAULT_MAX_RESULTS,
        min_results: int = 1,
    ):
        """Run discovery and immediately pass candidates through Phase 10.

        This method is the explicit Phase 10 integration boundary and
        returns both the discovery metadata and catalog result. Phase 10
        validation (MCP resolution / evidence rules) decides what becomes
        an approved item; unvalidated candidates are rejected, never
        fabricated into results.
        """
        discovery = await self.discover(
            query, item_type=item_type, max_results=max_results, min_results=min_results
        )
        catalog = await phase10_pipeline.process(discovery.candidates)
        return discovery, catalog

    async def discover(
        self,
        query: str,
        item_type: str = "all",
        max_results: int = DEFAULT_MAX_RESULTS,
        min_results: int = 1,
    ) -> SearchOrchestratorResult:
        """Run the enabled sources for `query`.

        `item_type` mirrors Section 28's `GET /api/search` contract
        (``all | tool | agent``): it only decides *which* protocol
        group(s) run, never filters an individual source's own
        classification. Only the MCP protocol group has live sources;
        `item_type="agent"` therefore discovers nothing live.

        `min_results` is the shortfall the DB shortlist left behind: the
        MCP adapter uses it as its registry -> GitHub cascade threshold.
        """
        if not query.strip():
            raise ValueError("Discovery query must not be empty")
        if item_type not in ("all", "tool", "agent"):
            raise ValueError("item_type must be 'all', 'tool', or 'agent'")

        want_mcp = item_type in ("all", "tool")

        outcomes = (
            await self._mcp_adapter.discover(query, max_results, min_results)
            if want_mcp
            else []
        )

        candidates: list[CandidateReference] = []
        sources_attempted: list[str] = []
        sources_succeeded: list[str] = []
        sources_failed: list[str] = []
        source_errors: dict[str, str] = {}

        for outcome in outcomes:
            name = f"mcp:{outcome.source}"
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
