from __future__ import annotations
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, Field
from app.models import (
    ArtifactMetadata,
    DiscoveryEvidence,
    DiscoverySource,
    Item,
    TestRun,
)
from app.query.planner import QueryPlan
from app.sandbox.schemas import ClassificationResult


class SearchMetadata(BaseModel):
    mode: Literal["cached", "live", "merged"] = "cached"
    sources_attempted: list[str] = Field(default_factory=list)
    sources_succeeded: list[str] = Field(default_factory=list)
    sources_failed: list[str] = Field(default_factory=list)
    cached_results: int = 0
    live_candidates: int = 0
    approved_count: int = 0
    rejected_count: int = 0
    plan: QueryPlan | None = None


class SearchResultItem(BaseModel):
    item: Item
    final_score: float
    relevance: float
    reliability: float
    freshness: float
    evidence: float
    classification: ClassificationResult | None = None


class SearchToolItem(BaseModel):
    item_id: UUID
    name: str
    description: str
    server_id: str
    tool_name: str
    mcp_schema: dict[str, Any] = Field(serialization_alias="schema")
    source: DiscoverySource
    final_score: float
    reliability: float


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    tools: list[SearchToolItem] = Field(default_factory=list)
    metadata: SearchMetadata


class ItemSchemaResponse(BaseModel):
    item_id: UUID
    type: Literal["tool", "agent"]
    item_schema: dict[str, Any] = Field(serialization_alias="schema")


class ItemProvenanceResponse(BaseModel):
    item_id: UUID
    provenance: list[DiscoverySource]
    evidence: list[DiscoveryEvidence]


# --- NEW: View Code / Monaco editor support ---------------------------------

# --- NEW: Tool Test / Classification (Phase 15) ---------------------------------

class ItemClassificationResponse(BaseModel):
    """Classification result for a single item (used by /items/{id}/classification)."""
    item_id: UUID
    classification: ClassificationResult


class ToolConnectResponse(BaseModel):
    """Result of a /test/connect call."""
    connected: bool
    auth_required: bool = False
    transport: str | None = None
    tools: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    requires_auth: bool = False
    # Error classification (populated when connected=False and error is set)
    auth_reason: str | None = None
    user_message: str | None = None
    show_token_input: bool = False
    show_oauth_button: bool = False


class ToolInvokeRequest(BaseModel):
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolInvokeResponse(BaseModel):
    status: Literal["success", "error"]
    result: Any = None
    error: str | None = None
    duration_ms: int | None = None
    requires_auth: bool = False
    # Error classification (populated when status="error")
    auth_reason: str | None = None
    user_message: str | None = None
    show_token_input: bool = False
    show_oauth_button: bool = False


class OAuthStartResponse(BaseModel):
    authorization_url: str
    state: str


class OAuthCallbackResponse(BaseModel):
    success: bool
    message: str


class IntegrationSnippet(BaseModel):
    """Ready-to-paste mcpServers JSON config for the 'Integration' tab."""

    available: bool
    language: str = "json"
    snippet: str | None = None
    source: str | None = None  # "synthesized_from_registry" | "extracted_from_docs" | "cached"
    note: str | None = None


class SourcePreview(BaseModel):
    """README / extracted docs text for the 'Source' tab."""

    available: bool
    language: str | None = None
    content: str | None = None
    note: str | None = None


class RepositoryTreeItem(BaseModel):
    """Single item in repository file tree."""

    path: str
    type: Literal["blob", "tree"]  # "blob" = file, "tree" = directory
    size: int | None = None


class RepositoryTreeResponse(BaseModel):
    """Response containing repository file tree."""

    item_id: UUID
    available: bool
    tree: list[RepositoryTreeItem] = Field(default_factory=list)
    note: str | None = None


class SourceFileResponse(BaseModel):
    """Response containing individual source file content."""

    item_id: UUID
    file_path: str
    available: bool
    language: str | None = None
    content: str | None = None
    note: str | None = None


class ItemArtifactsResponse(BaseModel):
    item_id: UUID
    artifacts: ArtifactMetadata
    integration: IntegrationSnippet
    source_preview: SourcePreview

# ------------------------------------------------------------------------------


class DeferredExecutionResponse(BaseModel):
    item_id: UUID
    status: Literal["not_implemented"]
    detail: str


class TestRunResponse(BaseModel):
    test_run: TestRun