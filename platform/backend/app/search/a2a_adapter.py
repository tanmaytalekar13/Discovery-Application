"""A2A-side multi-source discovery adapter (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 7:

    DiscoveryOrchestrator
            |
            +--> A2ADiscoveryAdapter
                    +--> GitHub
                    +--> Agent Registry
                    +--> Web Search
                    +--> Web Extraction
                    +--> Well-Known
                    +--> Direct Agent Card

Mirrors `app.search.mcp_adapter.MCPDiscoveryAdapter`: no provider
logic lives here, only fan-out/aggregation/conversion into the common
`CandidateReference`, with per-source failure isolation (rule #21) via
`gather_source_outcomes`. `A2ARegistryClient` accepts a *list* because,
unlike the single official MCP Registry (Section 9), Section 6 notes
there is no single canonical A2A registry - operators may configure
several catalog base URLs.
"""

from __future__ import annotations

from app.discovery.a2a.well_known import (
    discover_well_known_agents,
    resolve_configured_agent_card,
)
from app.discovery.a2a_registry.client import A2ARegistryClient
from app.discovery.common.candidate import CandidateReference, SourceOutcome
from app.discovery.common.candidate import from_a2a_registry_candidate as _from_registry
from app.discovery.common.candidate import from_configured_endpoint
from app.discovery.common.candidate import from_github_candidate as _from_github
from app.discovery.common.candidate import (
    from_web_extraction_candidate as _from_web_extraction,
)
from app.discovery.common.candidate import (
    from_web_search_candidate as _from_web_search,
)
from app.discovery.common.candidate import from_well_known_probe as _from_well_known
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.web_extraction.client import WebExtractionAdapter
from app.discovery.web_extraction.client import WebExtractionError
from app.discovery.web_search.client import WebSearchCandidate
from app.discovery.web_search.client import WebSearchDiscoveryAdapter
from app.search.concurrency import gather_source_outcomes

DEFAULT_MAX_RESULTS = 20


def _a2a_biased_query(query: str) -> str:
    return f'{query} ("A2A agent" OR "Agent Card")'


class A2ADiscoveryAdapter:
    """Aggregate every configured A2A discovery source for one query."""

    def __init__(
        self,
        *,
        github: GitHubDiscoveryAdapter | None = None,
        a2a_registries: tuple[A2ARegistryClient, ...] = (),
        web_search: WebSearchDiscoveryAdapter | None = None,
        web_extraction: WebExtractionAdapter | None = None,
        extraction_urls: tuple[str, ...] = (),
        well_known_hosts: tuple[str, ...] = (),
        configured_agent_card_urls: tuple[str, ...] = (),
    ) -> None:
        self._github = github
        self._a2a_registries = a2a_registries
        self._web_search = web_search
        self._web_extraction = web_extraction
        self._extraction_urls = extraction_urls
        self._well_known_hosts = well_known_hosts
        self._configured_agent_card_urls = configured_agent_card_urls

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[SourceOutcome]:
        """Run every enabled A2A source concurrently for `query`."""
        tasks: dict[str, object] = {}

        if self._github is not None:
            tasks["github"] = self._discover_github(query, max_results)

        for index, registry in enumerate(self._a2a_registries):
            tasks[f"a2a_registry[{index}]"] = self._discover_registry(
                registry, query, max_results
            )

        if self._web_search is not None:
            tasks["web_search"] = self._discover_web_search(query, max_results)

        if self._web_extraction is not None and self._extraction_urls:
            tasks["web_extraction"] = self._discover_extraction()

        if self._well_known_hosts:
            tasks["well_known"] = self._discover_well_known()

        if self._configured_agent_card_urls:
            tasks["configured_agent_card"] = self._discover_configured_agent_cards()

        return await gather_source_outcomes(tasks)

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
        registry: A2ARegistryClient,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        candidates = await registry.search(query, max_results)
        return [_from_registry(candidate) for candidate in candidates]

    async def _discover_web_search(
        self,
        query: str,
        max_results: int,
    ) -> list[CandidateReference]:
        assert self._web_search is not None
        candidates = await self._web_search.discover(
            _a2a_biased_query(query),
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

    async def _discover_well_known(self) -> list[CandidateReference]:
        probes = await discover_well_known_agents(list(self._well_known_hosts))
        candidates = [_from_well_known(probe) for probe in probes]
        return [candidate for candidate in candidates if candidate is not None]

    async def _discover_configured_agent_cards(self) -> list[CandidateReference]:
        candidates: list[CandidateReference] = []
        for url in self._configured_agent_card_urls:
            probe = await resolve_configured_agent_card(url)
            candidate = _from_well_known(probe)
            if candidate is not None:
                candidates.append(candidate)
            else:
                candidates.append(from_configured_endpoint(url=url, protocol="a2a"))
        return candidates


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
