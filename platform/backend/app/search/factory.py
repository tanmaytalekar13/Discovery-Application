"""Build the discovery stack from `Settings`.

Each discovery source is independently enabled/disabled. This module is
the one place that turns configuration flags into concrete adapter
instances; `MCPDiscoveryAdapter` itself has no knowledge of environment
variables. A source flag left on without the configuration it needs is
treated as not configured rather than attempted and left to fail on
every request - this keeps `sources_failed` meaningful (an actual
provider failure) instead of permanently noisy.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
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

    if (
        github is None
        and mcp_registry is None
        and not settings.enable_mcp_service_discovery
    ):
        return None

    return MCPDiscoveryAdapter(
        github=github,
        mcp_registry=mcp_registry,
        enable_service_discovery=settings.enable_mcp_service_discovery,
    )


def build_orchestrator(
    settings: Settings,
    *,
    httpx_client: httpx.AsyncClient | None = None,
) -> DiscoveryOrchestrator | None:
    """Build the orchestrator, or `None` if no source is configured.

    A `None` result means live discovery is entirely unavailable for
    this deployment (every `ENABLE_*` flag off). Callers (the search
    endpoint) then serve ArcadeDB-only results: the DB is the durable
    source of truth for official + verified servers, so an unconfigured
    or failed external stack must never blank the UI.
    """
    mcp_adapter = build_mcp_adapter(settings, httpx_client=httpx_client)

    if mcp_adapter is None:
        return None

    return DiscoveryOrchestrator(mcp_adapter=mcp_adapter)


def build_phase10_pipeline(settings: Settings, db_client):
    """Construct the Phase 10 persistence/normalization pipeline."""
    from app.db.repositories import ItemRepository
    from app.normalization.pipeline import Phase10Pipeline
    from app.query.embeddings import LocalEmbeddingModel

    return Phase10Pipeline(
        repository=ItemRepository(db_client),
        settings=settings,
        embedder=LocalEmbeddingModel(settings.embedding_dimensions),
    )


def build_phase11_search_service(settings: Settings, db_client):
    """Construct the query planner/embedding/ranking service."""
    from app.db.repositories import ItemRepository
    from app.query.embeddings import LocalEmbeddingModel
    from app.query.service import Phase11SearchService

    return Phase11SearchService(
        repository=ItemRepository(db_client),
        settings=settings,
        embedder=LocalEmbeddingModel(settings.embedding_dimensions),
    )


def build_application_search_service(settings: Settings, db_client):
    """Construct the user-facing search pipeline (DB-first, external completion)."""
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
