"""Build a `DiscoveryOrchestrator` from `Settings` (Phase 09).

Each discovery source is independently enabled/disabled (Section
32/33). This module is the one place that turns configuration flags
into concrete adapter instances; `DiscoveryOrchestrator` itself has no
knowledge of environment variables or which concrete provider backs
`web_search`, per Section 7's "Do not put provider-specific logic
into the Search Orchestrator."

A source flag left on without the configuration it needs (e.g.
`ENABLE_WEB_SEARCH_DISCOVERY=true` but no `BRAVE_SEARCH_API_KEY`) is
treated as not configured rather than attempted and left to fail on
every request - this keeps `sources_failed` meaningful (an actual
provider failure) instead of permanently noisy.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.discovery.a2a_registry.client import A2ARegistryClient
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
from app.discovery.web_extraction.client import WebExtractionAdapter
from app.discovery.web_search.client import (
    BraveWebSearchProvider,
    WebSearchDiscoveryAdapter,
)
from app.search.a2a_adapter import A2ADiscoveryAdapter
from app.search.mcp_adapter import MCPDiscoveryAdapter
from app.search.orchestrator import DiscoveryOrchestrator


def build_mcp_adapter(
    settings: Settings,
    *,
    httpx_client: httpx.AsyncClient | None = None,
) -> MCPDiscoveryAdapter | None:
    github = (
        GitHubDiscoveryAdapter(
            token=settings.github_token,
            httpx_client=httpx_client,
        )
        if settings.enable_github_discovery
        else None
    )

    mcp_registry = (
        MCPRegistryClient(httpx_client=httpx_client)
        if settings.enable_mcp_registry_discovery
        else None
    )

    web_search = None
    if settings.enable_web_search_discovery and settings.brave_search_api_key:
        provider = BraveWebSearchProvider(
            api_key=settings.brave_search_api_key,
            httpx_client=httpx_client,
        )
        web_search = WebSearchDiscoveryAdapter(provider)

    web_extraction_urls = settings.web_extraction_url_list
    web_extraction = _build_web_extraction_adapter(settings, httpx_client=httpx_client)

    if (
        github is None
        and mcp_registry is None
        and web_search is None
        and (web_extraction is None or not web_extraction_urls)
        and not settings.configured_mcp_endpoint_list
    ):
        return None

    return MCPDiscoveryAdapter(
        github=github,
        mcp_registry=mcp_registry,
        web_search=web_search,
        web_extraction=web_extraction,
        extraction_urls=web_extraction_urls,
        configured_endpoints=settings.configured_mcp_endpoint_list,
    )


def build_a2a_adapter(
    settings: Settings,
    *,
    httpx_client: httpx.AsyncClient | None = None,
) -> A2ADiscoveryAdapter | None:
    github = (
        GitHubDiscoveryAdapter(
            token=settings.github_token,
            httpx_client=httpx_client,
        )
        if settings.enable_github_discovery
        else None
    )

    a2a_registries: tuple[A2ARegistryClient, ...] = ()
    if settings.enable_a2a_registry_discovery:
        a2a_registries = tuple(
            A2ARegistryClient(base_url=base_url, httpx_client=httpx_client)
            for base_url in settings.a2a_registry_base_url_list
        )

    web_search = None
    if settings.enable_web_search_discovery and settings.brave_search_api_key:
        provider = BraveWebSearchProvider(
            api_key=settings.brave_search_api_key,
            httpx_client=httpx_client,
        )
        web_search = WebSearchDiscoveryAdapter(provider)

    web_extraction_urls = settings.web_extraction_url_list
    web_extraction = _build_web_extraction_adapter(settings, httpx_client=httpx_client)

    well_known_hosts: tuple[str, ...] = ()
    if settings.enable_well_known_a2a:
        well_known_hosts = settings.well_known_agent_host_list

    if (
        github is None
        and not a2a_registries
        and web_search is None
        and (web_extraction is None or not web_extraction_urls)
        and not well_known_hosts
        and not settings.configured_agent_card_url_list
    ):
        return None

    return A2ADiscoveryAdapter(
        github=github,
        a2a_registries=a2a_registries,
        web_search=web_search,
        web_extraction=web_extraction,
        extraction_urls=web_extraction_urls,
        well_known_hosts=well_known_hosts,
        configured_agent_card_urls=settings.configured_agent_card_url_list,
    )


def build_orchestrator(
    settings: Settings,
    *,
    httpx_client: httpx.AsyncClient | None = None,
) -> DiscoveryOrchestrator | None:
    """Build the orchestrator, or `None` if no source is configured.

    A `None` result means live discovery is entirely unavailable for
    this deployment (e.g. every `ENABLE_*` flag is off, which is the
    default - Section 32's example configuration ships every live
    source disabled). Callers (Phase 17's search endpoint) fall back
    to ArcadeDB-only results in that case, per Section 31: "If all
    live sources fail but ArcadeDB has valid results: return cached
    results + show live discovery unavailable."
    """
    mcp_adapter = build_mcp_adapter(settings, httpx_client=httpx_client)
    a2a_adapter = build_a2a_adapter(settings, httpx_client=httpx_client)

    if mcp_adapter is None and a2a_adapter is None:
        return None

    return DiscoveryOrchestrator(mcp_adapter=mcp_adapter, a2a_adapter=a2a_adapter)


def build_phase10_pipeline(settings: Settings, db_client):
    """Construct the Phase 10 persistence pipeline without changing Phase 09 source wiring."""
    from app.db.repositories import ItemRepository
    from app.normalization.pipeline import Phase10Pipeline
    from app.query.embeddings import LocalEmbeddingModel

    return Phase10Pipeline(
        repository=ItemRepository(db_client),
        settings=settings,
        embedder=LocalEmbeddingModel(settings.embedding_dimensions),
    )


def build_phase11_search_service(settings: Settings, db_client):
    """Construct the Phase 11 planner/embedding/ranking service."""
    from app.db.repositories import ItemRepository
    from app.query.embeddings import LocalEmbeddingModel
    from app.query.service import Phase11SearchService

    return Phase11SearchService(
        repository=ItemRepository(db_client),
        settings=settings,
        embedder=LocalEmbeddingModel(settings.embedding_dimensions),
    )


def build_application_search_service(settings: Settings, db_client):
    """Construct the user-facing search pipeline across Phases 09, 10, and 11."""
    from app.search.application import ApplicationSearchService

    phase11 = build_phase11_search_service(settings, db_client)
    orchestrator = build_orchestrator(settings)
    phase10 = build_phase10_pipeline(settings, db_client) if orchestrator else None
    return ApplicationSearchService(
        settings=settings,
        phase11=phase11,
        orchestrator=orchestrator,
        phase10_pipeline=phase10,
    )


def _build_web_extraction_adapter(
    settings: Settings,
    *,
    httpx_client: httpx.AsyncClient | None,
) -> WebExtractionAdapter | None:
    if not settings.enable_web_extraction or not settings.web_extraction_url_list:
        return None
    return WebExtractionAdapter(
        httpx_client=httpx_client,
        timeout_seconds=settings.web_extraction_timeout_seconds,
        max_redirects=settings.web_extraction_max_redirects,
        max_response_bytes=settings.web_extraction_max_response_bytes,
        user_agent=settings.web_extraction_user_agent,
        respect_robots=settings.web_extraction_respect_robots,
    )
