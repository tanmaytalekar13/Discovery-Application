"""MCP discovery source cascade (Registry -> GitHub).

This module contains no provider-specific logic itself (that lives in
each discovery client); it runs the official MCP Registry (plus the
registry-backed service search) first and falls back to GitHub only
when the combined DB-known + registry-validated candidate count is
still below the caller's shortfall (`min_results`). Each source's
failure is isolated from the others via
`app.search.concurrency.gather_source_outcomes`.

`min_results` is the shortfall the DB shortlist left behind: the
cascade keeps GitHub out of the picture while the registry alone
covers it, and stops treating sources once enough candidates exist.
`max_results` remains each source's own page size.

Every constructor argument is optional: a `None` adapter means that
source is disabled, matching the `ENABLE_*_DISCOVERY` configuration
flags.
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
from app.discovery.github.client import GitHubDiscoveryAdapter, github_mcp_search_query
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
    """Registry-first MCP discovery cascade with a GitHub completion step."""

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
        min_results: int = 1,
    ) -> list[SourceOutcome]:
        """Run the official-first trust cascade for one query.

        Step 1: the official MCP Registry (and registry-backed service
        search) run concurrently - same official source, one trust tier.

        Step 2: GitHub runs only when the DB-known shortfall
        (`min_results`) is not already covered by the registry/service
        candidates. The caller passes the DB shortlist size in, so
        `DB + Registry >= cap` skips GitHub entirely while
        `DB + Registry < cap` pulls until the gap is closed.

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

        official_candidates: list[CandidateReference] = []
        if official_tasks:
            official_outcomes = await gather_source_outcomes(official_tasks)
            outcomes.extend(official_outcomes)
            for outcome in official_outcomes:
                if outcome.succeeded:
                    official_candidates.extend(outcome.candidates)
            # Enough validated official candidates: cascade stops here and
            # GitHub is never contacted for this query.
            if len(official_candidates) >= min_results:
                return outcomes

        if self._github is not None:
            # Fetch only what the shortfall still needs after the registry
            # results (at least one candidate's worth; a full page is
            # pointless when the gap is 1-2 servers).
            github_fetch = max(min_results - len(official_candidates), 1)
            outcomes.extend(
                await gather_source_outcomes(
                    {"github": self._discover_github(query, github_fetch)}
                )
            )

        return outcomes

    async def _discover_github(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._github is not None
        candidates = await self._github.discover(
            github_mcp_search_query(query), max_results
        )
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
