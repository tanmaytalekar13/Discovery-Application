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
from app.discovery.mcp_registry.service_adapter import discover_services
from app.search.concurrency import gather_source_outcomes
from app.verification.prefilter import KNOWN_INCOMPATIBLE_HOSTS, known_incompatible_reason

DEFAULT_MAX_RESULTS = 20


def _candidate_is_excluded(candidate: CandidateReference) -> bool:
    """True when a candidate is a third-party hosted/aggregator server.

    Spec v2 Section 7.4: Smithery-style platforms are confirmed
    incompatible (or sit behind platform-level gateway auth) and are
    never surfaced to users. Checked on the flattened candidate URLs,
    the raw registry candidate's remote endpoints, and explicit host
    mentions in the title/description (e.g. "hosted on smithery.ai").
    Upstream GitHub repositories are deliberately NOT filtered here -
    they are exactly what the registry-miss fallback should return.
    """
    for url in (str(getattr(candidate, "url", None) or ""), str(getattr(candidate, "repository_url", None) or "")):
        if url and known_incompatible_reason(url):
            return True

    raw = candidate.raw_metadata.get("source_candidate")
    if raw is not None:
        for remote in getattr(raw, "remotes", ()) or ():
            remote_url = remote.get("url") if isinstance(remote, dict) else None
            if remote_url and known_incompatible_reason(str(remote_url)):
                return True

    text = f"{getattr(candidate, 'title', '') or ''} {getattr(candidate, 'description', '') or ''}".lower()
    return any(host in text for host in KNOWN_INCOMPATIBLE_HOSTS)


class MCPDiscoveryAdapter:
    """Aggregate the supported MCP discovery sources: Registry, GitHub, and Service Search."""

    def __init__(
        self,
        *,
        github: GitHubDiscoveryAdapter | None = None,
        mcp_registry: MCPRegistryClient | None = None,
        enable_service_discovery: bool = False,
    ) -> None:
        self._github = github
        self._mcp_registry = mcp_registry
        self._enable_service_discovery = enable_service_discovery

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[SourceOutcome]:
        """Run MCP discovery sources as a sequential trust cascade.

        Spec v2 Section 9.1: the official MCP Registry is the
        higher-trust, cheaper-to-query source (structured, already has
        manifests), so it is consulted first — together with the
        registry-backed service search, which targets the same official
        source. GitHub is only queried when the official sources yield
        *nothing* for the query. A registry hit stops the cascade and
        GitHub is never contacted for that query.

        Returns one `SourceOutcome` per *attempted* source (a disabled
        source, i.e. constructor arg left `None`, is simply absent -
        not attempted, not failed).
        """
        outcomes: list[SourceOutcome] = []

        official_tasks: dict[str, object] = {}
        if self._mcp_registry is not None:
            official_tasks["mcp_registry"] = self._discover_registry(query, max_results)
        if self._enable_service_discovery:
            official_tasks["service"] = self._discover_service(query, max_results)

        if official_tasks:
            official_outcomes = await gather_source_outcomes(official_tasks)
            outcomes.extend(official_outcomes)
            if any(
                outcome.succeeded and outcome.candidates
                for outcome in official_outcomes
            ):
                return outcomes

        if self._github is not None:
            outcomes.extend(
                await gather_source_outcomes(
                    {"github": self._discover_github(query, max_results)}
                )
            )

        return outcomes

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
        return [
            converted
            for candidate in candidates
            if not _candidate_is_excluded(converted := _from_mcp_registry(candidate))
        ]

    async def _discover_service(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        """Search the official MCP Registry for servers matching a service name."""
        candidates = await discover_services(query, max_results=max_results)
        return [c for c in candidates if not _candidate_is_excluded(c)]
