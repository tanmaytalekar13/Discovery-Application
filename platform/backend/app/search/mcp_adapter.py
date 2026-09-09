"""MCP-side multi-source discovery adapter (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 7:

    DiscoveryOrchestrator
            |
            +--> MCPDiscoveryAdapter
            |       +--> GitHub
    |       +--> MCP Registry

This module contains no provider-specific logic itself (that already
lives in each Phase 04/05/07/08 adapter); it only fans a single user
query out to whichever MCP discovery sources are configured, converts
each source's own candidate type into the common `CandidateReference`
(Phase 09's `app.discovery.common.candidate`), and isolates one
source's failure from the others (rule #21) via
`app.search.concurrency.gather_source_outcomes`.

Every constructor argument is optional: a `None` adapter means that
source is disabled (Section 32/33 - each source must be independently
enabled/disabled), matching the `ENABLE_*_DISCOVERY` configuration
flags. The "Optional DNS" branch from Section 7 has no adapter yet
(Section 6 lists it under "Additional", not "Mandatory/core") and is
intentionally not implemented here rather than stubbed out (rule #35 -
Codex protocol: "Do not create placeholder classes/endpoints merely to
make a phase appear complete.").
"""

from __future__ import annotations

from app.discovery.common.candidate import (
    CandidateReference,
    SourceOutcome,
)
from app.discovery.common.candidate import from_github_candidate as _from_github
from app.discovery.common.candidate import (
    from_mcp_registry_candidate as _from_mcp_registry,
)
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
from app.search.concurrency import gather_source_outcomes

DEFAULT_MAX_RESULTS = 20


class MCPDiscoveryAdapter:
    """Aggregate the only supported MCP discovery sources: Registry and GitHub."""

    def __init__(
        self,
        *,
        github: GitHubDiscoveryAdapter | None = None,
        mcp_registry: MCPRegistryClient | None = None,
    ) -> None:
        self._github = github
        self._mcp_registry = mcp_registry

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[SourceOutcome]:
        """Run every enabled MCP source concurrently for `query`.

        Returns one `SourceOutcome` per *attempted* source (a disabled
        source, i.e. constructor arg left `None`, is simply absent -
        not attempted, not failed).

        """
        tier1_tasks: dict[str, object] = {}

        if self._github is not None:
            tier1_tasks["github"] = self._discover_github(query, max_results)

        if self._mcp_registry is not None:
            tier1_tasks["mcp_registry"] = self._discover_registry(query, max_results)
        return await gather_source_outcomes(tier1_tasks)

    async def _discover_github(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._github is not None
        candidates = await self._github.discover(query, max_results)
        return [_from_github(candidate) for candidate in candidates]

    async def _discover_registry(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._mcp_registry is not None
        candidates = await self._mcp_registry.search(query, max_results)
        return [_from_mcp_registry(candidate) for candidate in candidates]
