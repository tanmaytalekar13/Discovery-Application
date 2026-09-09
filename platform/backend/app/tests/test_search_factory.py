from app.config import Settings
from app.search.factory import (
    build_application_search_service,
    build_a2a_adapter,
    build_mcp_adapter,
    build_orchestrator,
    build_phase11_search_service,
)


def _settings(**overrides) -> Settings:
    base = {
        "arcadedb_host": "arcadedb",
        "arcadedb_database": "platform",
        "arcadedb_user": "root",
        "arcadedb_password": "secret",
        "enable_github_discovery": False,
        "enable_mcp_registry_discovery": False,
        "enable_mcp_service_discovery": False,
        "enable_a2a_registry_discovery": False,
        "enable_web_search_discovery": False,
        "enable_web_extraction": False,
        "enable_npm_discovery": False,
        "enable_awesome_list_discovery": False,
        "github_token": "",
        "firecrawl_api_key": "",
        "a2a_registry_base_urls": "",
        "well_known_agent_hosts": "",
        "configured_mcp_endpoints": "",
        "configured_agent_card_urls": "",
        "web_extraction_urls": "",
    }
    base.update(overrides)
    return Settings(**base)


def test_all_sources_disabled_by_default_yields_no_orchestrator():
    settings = _settings()

    assert build_mcp_adapter(settings) is None
    assert build_a2a_adapter(settings) is None
    assert build_orchestrator(settings) is None


def test_enabling_github_builds_an_mcp_adapter():
    settings = _settings(enable_github_discovery=True)

    adapter = build_mcp_adapter(settings)

    assert adapter is not None
    assert adapter._github is not None
    assert adapter._mcp_registry is None
    assert build_a2a_adapter(settings) is None


def test_build_orchestrator_uses_only_the_mcp_adapter_when_configured():
    settings = _settings(
        enable_github_discovery=True,
    )

    orchestrator = build_orchestrator(settings)

    assert orchestrator is not None
    assert orchestrator._mcp_adapter is not None
    assert orchestrator._a2a_adapter is None


def test_service_discovery_enabled_without_other_sources():
    """Service discovery alone is enough to build an adapter."""
    settings = _settings(enable_mcp_service_discovery=True)

    adapter = build_mcp_adapter(settings)

    assert adapter is not None
    assert adapter._enable_service_discovery is True
    assert adapter._github is None
    assert adapter._mcp_registry is None


def test_build_phase11_search_service_uses_local_embedding_dimensions():
    service = build_phase11_search_service(_settings(embedding_dimensions=32), object())

    assert service._embedder.dimensions == 32


def test_application_search_service_wires_phase9_phase10_and_phase11():
    settings = _settings(
        discovery_mode="mixed",
        enable_mcp_registry_discovery=True,
    )

    service = build_application_search_service(settings, object())

    assert service._orchestrator is not None
    assert service._phase10_pipeline is not None
    assert service._phase11 is not None
