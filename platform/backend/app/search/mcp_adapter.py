"""MCP-side multi-source discovery adapter (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 7:

    DiscoveryOrchestrator
            |
            +--> MCPDiscoveryAdapter
            |       +--> GitHub
            |       +--> MCP Registry
            |       +--> Web Search
            |       +--> Web Extraction
            |       +--> Direct Endpoint
            |       +--> Optional DNS

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

from app.discovery.common.candidate import CandidateReference, SourceOutcome
from app.discovery.common.candidate import from_configured_endpoint
from app.discovery.common.candidate import from_github_candidate as _from_github
from app.discovery.common.candidate import (
    from_mcp_registry_candidate as _from_mcp_registry,
)
from app.discovery.common.candidate import (
    from_web_extraction_candidate as _from_web_extraction,
)
from app.discovery.common.candidate import (
    from_web_search_candidate as _from_web_search,
)
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
from app.discovery.web_extraction.client import WebExtractionAdapter
from app.discovery.web_extraction.client import WebExtractionError
from app.discovery.web_search.client import WebSearchCandidate
from app.discovery.web_search.client import WebSearchDiscoveryAdapter
from app.search.concurrency import gather_source_outcomes

DEFAULT_MAX_RESULTS = 20


def _mcp_biased_query(query: str) -> str:
    return f'{query} ("MCP server" OR "Model Context Protocol")'


class MCPDiscoveryAdapter:
    """Aggregate every configured MCP discovery source for one query."""

    def __init__(
        self,
        *,
        github: GitHubDiscoveryAdapter | None = None,
        mcp_registry: MCPRegistryClient | None = None,
        web_search: WebSearchDiscoveryAdapter | None = None,
        web_extraction: WebExtractionAdapter | None = None,
        extraction_urls: tuple[str, ...] = (),
        configured_endpoints: tuple[str, ...] = (),
    ) -> None:
        self._github = github
        self._mcp_registry = mcp_registry
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
        """
        tasks: dict[str, object] = {}

        if self._github is not None:
            tasks["github"] = self._discover_github(query, max_results)

        if self._mcp_registry is not None:
            tasks["mcp_registry"] = self._discover_registry(query, max_results)

        if self._web_search is not None:
            tasks["web_search"] = self._discover_web_search(query, max_results)

        if self._web_extraction is not None and self._extraction_urls:
            tasks["web_extraction"] = self._discover_extraction()

        outcomes = await gather_source_outcomes(tasks)

        if self._configured_endpoints:
            outcomes.append(self._configured_outcome())

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
        return [_from_mcp_registry(candidate) for candidate in candidates]

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
