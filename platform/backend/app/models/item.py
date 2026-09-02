from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl


class ItemType(str, Enum):
    TOOL = "tool"
    AGENT = "agent"


class ItemStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    UNAVAILABLE = "unavailable"


class SourceType(str, Enum):
    MCP_REGISTRY = "mcp_registry"
    A2A_CATALOG = "a2a_catalog"
    WELL_KNOWN = "well_known"
    CONFIGURED = "configured"
    GITHUB = "github"
    WEB_SEARCH = "web_search"
    WEB_PAGE = "web_page"


class Reliability(BaseModel):
    score: float = Field(
        ge=0.0,
        le=1.0,
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )
    scoring_version: str = "v1"
    last_evaluated: datetime | None = None
    security_validation: float = Field(default=0.0, ge=0.0, le=1.0)
    signals: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)


class DiscoveryMetadata(BaseModel):
    first_seen: datetime
    last_seen: datetime
    last_synced: datetime


class DiscoverySource(BaseModel):
    type: SourceType
    id: str
    url: HttpUrl | None = None
    provider: str | None = None


class DiscoveryEvidence(BaseModel):
    evidence_id: UUID
    kind: str
    statement: str
    source: DiscoverySource
    observed_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class ReliabilityEvaluation(BaseModel):
    evaluation_id: UUID
    score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    scoring_version: str = "v1"
    approved: bool = False
    signals: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    security_validation: float = Field(default=0.0, ge=0.0, le=1.0)
    evaluated_at: datetime


class ToolMetadata(BaseModel):
    server_id: str
    tool_name: str
    mcp_schema: dict[str, Any] = Field(
        default_factory=dict
    )


class AgentMetadata(BaseModel):
    endpoint: HttpUrl
    agent_card: dict[str, Any] = Field(
        default_factory=dict
    )
    skills: list[str] = Field(
        default_factory=list
    )
    capabilities: list[str] = Field(
        default_factory=list
    )
    declared_dependencies: list[str] = Field(
        default_factory=list
    )


class ArtifactMetadata(BaseModel):
    source_available: bool = False
    source_url: HttpUrl | None = None
    source_code: str | None = None
    config_files: list[dict[str, Any]] = Field(
        default_factory=list
    )

    # Cache paths (relative paths stored in DB, actual content on disk)
    integration_cache_path: str | None = None
    source_tree_cache_path: str | None = None


class Item(BaseModel):
    item_id: UUID
    canonical_id: str | None = None

    type: ItemType
    name: str
    description: str

    source: DiscoverySource
    provenance: list[DiscoverySource] = Field(default_factory=list)
    evidence: list[DiscoveryEvidence] = Field(default_factory=list)

    version: str | None = None
    status: ItemStatus = ItemStatus.ACTIVE

    reliability: Reliability
    discovery: DiscoveryMetadata

    # Tool-specific metadata.
    tool: ToolMetadata | None = None

    # Agent-specific metadata.
    agent: AgentMetadata | None = None

    # Source code, source URL and configuration metadata.
    artifacts: ArtifactMetadata = Field(
        default_factory=ArtifactMetadata
    )

    # Phase 5 will populate this field using the
    # local embedding pipeline.
    embedding: list[float] | None = None
