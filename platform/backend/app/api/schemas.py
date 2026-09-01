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


class SearchResponse(BaseModel):
    results: list[SearchResultItem]
    metadata: SearchMetadata


class ItemSchemaResponse(BaseModel):
    item_id: UUID
    type: Literal["tool", "agent"]
    item_schema: dict[str, Any] = Field(serialization_alias="schema")


class ItemProvenanceResponse(BaseModel):
    item_id: UUID
    provenance: list[DiscoverySource]
    evidence: list[DiscoveryEvidence]


class ItemArtifactsResponse(BaseModel):
    item_id: UUID
    artifacts: ArtifactMetadata


class DeferredExecutionResponse(BaseModel):
    item_id: UUID
    status: Literal["not_implemented"]
    detail: str


class TestRunResponse(BaseModel):
    test_run: TestRun
