"""MCP-side multi-source discovery adapter (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 7:

    DiscoveryOrchestrator
            |
            +--> MCPDiscoveryAdapter
            |       +--> GitHub
            |       +--> GitHub Topics
            |       +--> MCP Registry
            |       +--> npm Registry
            |       +--> Awesome Lists
            |       +--> Web Search (FALLBACK - only if structured sources < 5 results)
            |       +--> Web Extraction
            |       +--> Direct Endpoint

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

from app.discovery.awesome_list.client import AwesomeListAdapter
from app.discovery.common.candidate import (
    CandidateReference,
    SourceOutcome,
)
from app.discovery.common.candidate import from_awesome_list_candidate as _from_awesome_list
from app.discovery.common.candidate import from_configured_endpoint
from app.discovery.common.candidate import from_github_candidate as _from_github
from app.discovery.common.candidate import (
    from_github_topics_candidate as _from_github_topics,
)
from app.discovery.common.candidate import (
    from_mcp_registry_candidate as _from_mcp_registry,
)
from app.discovery.common.candidate import from_npm_candidate as _from_npm
from app.discovery.common.candidate import (
    from_web_extraction_candidate as _from_web_extraction,
)
from app.discovery.common.candidate import (
    from_web_search_candidate as _from_web_search,
)
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.github_topics.client import GitHubTopicsAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
from app.discovery.npm.client import NpmDiscoveryAdapter
from app.discovery.web_extraction.client import WebExtractionAdapter
from app.discovery.web_extraction.client import WebExtractionError
from app.discovery.web_search.client import WebSearchCandidate
from app.discovery.web_search.client import WebSearchDiscoveryAdapter
from app.search.concurrency import gather_source_outcomes

DEFAULT_MAX_RESULTS = 20
# Web search only fires if structured sources return fewer than this many results
WEB_SEARCH_FALLBACK_THRESHOLD = 5


def _mcp_biased_query(query: str) -> str:
    return f'{query} ("MCP server" OR "Model Context Protocol")'


class MCPDiscoveryAdapter:
    """Aggregate every configured MCP discovery source for one query.

    Sources are divided into two tiers:

    **Tier 1 (always run):**
      - MCP Registry
      - GitHub (keyword search)
      - GitHub Topics (topic:mcp-server)
      - npm Registry
      - Awesome Lists

    **Tier 2 (fallback only):**
      - Web Search (Firecrawl) — only runs if Tier 1 returns < 5 results.
        Requires `firecrawl_api_key` to be configured.

    This ensures structured, free sources dominate the results while
    web search provides coverage when those sources come up short.
    """

    def __init__(
        self,
        *,
        github: GitHubDiscoveryAdapter | None = None,
        github_topics: GitHubTopicsAdapter | None = None,
        mcp_registry: MCPRegistryClient | None = None,
        npm: NpmDiscoveryAdapter | None = None,
        awesome_list: AwesomeListAdapter | None = None,
        web_search: WebSearchDiscoveryAdapter | None = None,
        web_extraction: WebExtractionAdapter | None = None,
        extraction_urls: tuple[str, ...] = (),
        configured_endpoints: tuple[str, ...] = (),
    ) -> None:
        self._github = github
        self._github_topics = github_topics
        self._mcp_registry = mcp_registry
        self._npm = npm
        self._awesome_list = awesome_list
        self._web_search = web_search
        self._web_extraction = web_extraction
        self._extraction_urls = extraction_urls
        self._configured_endpoints = configured_endpoints

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[SourceOutcome]:
        """Run every enabled MCP source concurrently for `query`.

        Returns one `SourceOutcome` per *attempted* source (a disabled
        source, i.e. constructor arg left `None`, is simply absent -
        not attempted, not failed).

        Web search (Tier 2) is only run if the Tier 1 structured
        sources combined return fewer than 5 results.
        """
        # ---- Tier 1: structured sources (always run) ----
        tier1_tasks: dict[str, object] = {}

        if self._github is not None:
            tier1_tasks["github"] = self._discover_github(query, max_results)

        if self._github_topics is not None:
            tier1_tasks["github_topics"] = self._discover_github_topics(query, max_results)

        if self._mcp_registry is not None:
            tier1_tasks["mcp_registry"] = self._discover_registry(query, max_results)

        if self._npm is not None:
            tier1_tasks["npm"] = self._discover_npm(query, max_results)

        if self._awesome_list is not None:
            tier1_tasks["awesome_list"] = self._discover_awesome_list(query, max_results)

        tier1_outcomes = await gather_source_outcomes(tier1_tasks)

        # Count total candidates from Tier 1
        tier1_candidate_count = sum(
            len(o.candidates) for o in tier1_outcomes if o.succeeded
        )

        # ---- Tier 2: web search fallback ----
        tier2_outcomes: list[SourceOutcome] = []
        if (
            self._web_search is not None
            and tier1_candidate_count < WEB_SEARCH_FALLBACK_THRESHOLD
        ):
            tier2_outcomes = await gather_source_outcomes({
                "web_search": self._discover_web_search(query, max_results),
            })

        # ---- Web extraction (if configured) ----
        extraction_outcomes: list[SourceOutcome] = []
        if self._web_extraction is not None and self._extraction_urls:
            extraction_outcomes = await gather_source_outcomes({
                "web_extraction": self._discover_extraction(),
            })

        # ---- Configured endpoints ----
        configured_outcomes: list[SourceOutcome] = []
        if self._configured_endpoints:
            configured_outcomes.append(self._configured_outcome())

        return (
            tier1_outcomes
            + tier2_outcomes
            + extraction_outcomes
            + configured_outcomes
        )

    async def _discover_github(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._github is not None
        candidates = await self._github.discover(query, max_results)
        return [_from_github(candidate) for candidate in candidates]

    async def _discover_github_topics(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._github_topics is not None
        candidates = await self._github_topics.discover(query, max_results)
        return [_from_github_topics(candidate) for candidate in candidates]

    async def _discover_registry(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._mcp_registry is not None
        candidates = await self._mcp_registry.search(query, max_results)
        return [_from_mcp_registry(candidate) for candidate in candidates]

    async def _discover_npm(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._npm is not None
        candidates = await self._npm.discover(query, max_results)
        return [_from_npm(candidate) for candidate in candidates]

    async def _discover_awesome_list(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._awesome_list is not None
        candidates = await self._awesome_list.discover(query, max_results)
        return [_from_awesome_list(candidate) for candidate in candidates]

    async def _discover_web_search(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._web_search is not None
        candidates = await self._web_search.discover(
            _mcp_biased_query(query),
            max_results,
        )
        return [
            await _web_search_reference_with_extracted_content(
                candidate,
                self._web_extraction,
            )
            for candidate in candidates
        ]

    async def _discover_extraction(self) -> list[CandidateReference]:
        assert self._web_extraction is not None
        candidates = await self._web_extraction.extract_many(
            list(self._extraction_urls)
        )
        return [_from_web_extraction(candidate) for candidate in candidates]

    def _configured_outcome(self) -> SourceOutcome:
        candidates = tuple(
            from_configured_endpoint(url=url, protocol="mcp")
            for url in self._configured_endpoints
        )
        return SourceOutcome(
            source="configured_endpoint",
            succeeded=True,
            candidates=candidates,
        )


async def _web_search_reference_with_extracted_content(
    candidate: WebSearchCandidate,
    extractor: WebExtractionAdapter | None,
) -> CandidateReference:
    reference = _from_web_search(candidate)
    if extractor is None:
        return reference

    try:
        extracted = await extractor.extract(candidate.url)
    except WebExtractionError as exc:
        reference.raw_metadata["web_extraction_error"] = str(exc)
        return reference

    if extracted is not None:
        reference.raw_metadata["web_extraction_candidate"] = extracted
    return reference
