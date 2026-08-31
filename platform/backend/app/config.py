from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    arcadedb_host: str = Field(min_length=1)
    arcadedb_port: int = Field(default=2480, ge=1, le=65535)
    arcadedb_database: str = Field(min_length=1)
    arcadedb_user: str = Field(min_length=1)
    arcadedb_password: str = Field(min_length=1)
    reliability_threshold: float = Field(default=0.75, ge=0, le=1)
    discovery_mode: str = Field(default="mock", pattern="^(mock|live|mixed)$")
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    query_planner_timeout_seconds: float = Field(default=5.0, gt=0)
    embedding_dimensions: int = Field(default=64, ge=8, le=2048)
    ranking_relevance_weight: float = Field(default=0.45, ge=0)
    ranking_reliability_weight: float = Field(default=0.35, ge=0)
    ranking_freshness_weight: float = Field(default=0.10, ge=0)
    ranking_evidence_weight: float = Field(default=0.10, ge=0)
    ranking_candidate_limit: int = Field(default=100, ge=1, le=500)

    # Phase 09: each discovery source is independently enabled/disabled
    # (Section 32/33). A source with no credentials/targets configured
    # is treated as disabled by the orchestrator factory even if its
    # flag is left on, rather than attempted with empty configuration.
    enable_github_discovery: bool = False
    enable_mcp_registry_discovery: bool = False
    enable_a2a_registry_discovery: bool = False
    enable_web_search_discovery: bool = False
    enable_web_extraction: bool = False
    enable_well_known_a2a: bool = True

    github_token: str = ""
    brave_search_api_key: str = ""

    # Comma-separated lists (kept as plain strings so `.env` stays
    # simple - Section 32: "Provider URLs, credentials, limits and
    # concurrency must be configuration-driven.").
    a2a_registry_base_urls: str = ""
    well_known_agent_hosts: str = ""
    configured_mcp_endpoints: str = ""
    configured_agent_card_urls: str = ""

    discovery_max_results_per_source: int = Field(default=20, ge=1, le=100)

    def _split_csv(self, value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @property
    def a2a_registry_base_url_list(self) -> tuple[str, ...]:
        return self._split_csv(self.a2a_registry_base_urls)

    @property
    def well_known_agent_host_list(self) -> tuple[str, ...]:
        return self._split_csv(self.well_known_agent_hosts)

    @property
    def configured_mcp_endpoint_list(self) -> tuple[str, ...]:
        return self._split_csv(self.configured_mcp_endpoints)

    @property
    def configured_agent_card_url_list(self) -> tuple[str, ...]:
        return self._split_csv(self.configured_agent_card_urls)


@lru_cache
def get_settings() -> Settings:
    return Settings()
