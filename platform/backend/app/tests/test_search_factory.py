from app.config import Settings
from app.search.factory import build_a2a_adapter, build_mcp_adapter, build_orchestrator


def _settings(**overrides) -> Settings:
    base = {
        "arcadedb_host": "arcadedb",
        "arcadedb_database": "platform",
        "arcadedb_user": "root",
        "arcadedb_password": "secret",
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
    assert adapter._web_search is None


def test_web_search_requires_an_api_key_even_if_enabled():
    settings = _settings(enable_web_search_discovery=True, brave_search_api_key="")

    assert build_mcp_adapter(settings) is None

    settings_with_key = _settings(
        enable_web_search_discovery=True, brave_search_api_key="secret-key"
    )
    adapter = build_mcp_adapter(settings_with_key)

    assert adapter is not None
    assert adapter._web_search is not None


def test_a2a_registry_urls_are_parsed_from_csv():
    settings = _settings(
        enable_a2a_registry_discovery=True,
        a2a_registry_base_urls="https://a.example.com, https://b.example.com",
    )

    adapter = build_a2a_adapter(settings)

    assert adapter is not None
    assert len(adapter._a2a_registries) == 2


def test_well_known_enabled_by_default_but_needs_hosts_to_build_an_adapter():
    settings = _settings()

    # enable_well_known_a2a defaults to True, but with no configured
    # hosts and nothing else enabled there is still no A2A source.
    assert build_a2a_adapter(settings) is None

    settings_with_hosts = _settings(well_known_agent_hosts="https://agents.example.com")
    adapter = build_a2a_adapter(settings_with_hosts)

    assert adapter is not None
    assert adapter._well_known_hosts == ("https://agents.example.com",)


def test_build_orchestrator_combines_both_adapters_when_configured():
    settings = _settings(
        enable_github_discovery=True,
        well_known_agent_hosts="https://agents.example.com",
    )

    orchestrator = build_orchestrator(settings)

    assert orchestrator is not None
    assert orchestrator._mcp_adapter is not None
    assert orchestrator._a2a_adapter is not None
