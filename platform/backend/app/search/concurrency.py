"""Per-source concurrent execution with failure isolation (Phase 09).

Implements CODEX_EXECUTION_PLAN.md rule #21 ("One discovery-source
failure must not fail the complete search.") and Section 39's Phase 09
Definition of Done ("sources execute concurrently; one source can fail
without breaking search; candidates aggregate correctly.") as a single
reusable primitive, so `MCPDiscoveryAdapter`, `A2ADiscoveryAdapter` and
`DiscoveryOrchestrator` all isolate failures the same way instead of
each re-implementing `asyncio.gather(..., return_exceptions=True)`
bookkeeping.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Mapping

from app.discovery.common.candidate import CandidateReference, SourceOutcome


async def gather_source_outcomes(
    tasks: Mapping[str, Awaitable[list[CandidateReference]]],
) -> list[SourceOutcome]:
    """Run every `tasks` coroutine concurrently; isolate each failure.

    Each key is a source name (e.g. ``"github"``, ``"mcp_registry"``)
    and each value is a coroutine that resolves to that source's
    `CandidateReference` list. A source whose coroutine raises never
    prevents the other sources from completing, and is reported back
    as a failed `SourceOutcome` rather than raised - the caller
    decides how to surface `sources_failed`/`source_errors` (Section
    28's search response `metadata`).
    """
    if not tasks:
        return []

    names = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    outcomes: list[SourceOutcome] = []
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            outcomes.append(
                SourceOutcome(
                    source=name,
                    succeeded=False,
                    candidates=(),
                    error=f"{type(result).__name__}: {result}",
                )
            )
        else:
            outcomes.append(
                SourceOutcome(
                    source=name,
                    succeeded=True,
                    candidates=tuple(result),
                )
            )

    return outcomes
