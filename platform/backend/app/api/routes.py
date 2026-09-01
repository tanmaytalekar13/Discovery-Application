from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sse_starlette.sse import EventSourceResponse

from app.api.dependencies import (
    get_application_search_service,
    get_item_repository,
    get_test_run_repository,
)
from app.api.schemas import (
    DeferredExecutionResponse,
    ItemArtifactsResponse,
    ItemProvenanceResponse,
    ItemSchemaResponse,
    SearchMetadata,
    SearchResponse,
    SearchResultItem,
    TestRunResponse,
)
from app.db.repositories import ItemRepository, TestRunRepository
from app.models import Item, TestRun
from app.query.planner import PreferredType
from app.search.application import ApplicationSearchService

router = APIRouter(prefix="/api", tags=["discovery"])

SearchType = Literal["all", "tool", "agent"]


@router.get("/search", response_model=SearchResponse)
async def search(
    q: Annotated[str, Query(min_length=1)],
    type: Annotated[SearchType, Query()] = "all",
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    service: ApplicationSearchService = Depends(get_application_search_service),
) -> SearchResponse:
    """Search through the configured application discovery pipeline."""
    result = await service.search(q, item_type=_preferred_type(type), limit=limit)
    rows = [
        SearchResultItem(
            item=ranked.item,
            final_score=ranked.final_score,
            relevance=ranked.relevance,
            reliability=ranked.reliability,
            freshness=ranked.freshness,
            evidence=ranked.evidence,
        )
        for ranked in result.ranked.results
    ]
    return SearchResponse(
        results=rows,
        metadata=SearchMetadata(
            mode=result.metadata.mode,
            sources_attempted=list(result.metadata.sources_attempted),
            sources_succeeded=list(result.metadata.sources_succeeded),
            sources_failed=list(result.metadata.sources_failed),
            cached_results=result.metadata.cached_results,
            live_candidates=result.metadata.live_candidates,
            approved_count=result.metadata.approved_count,
            rejected_count=result.metadata.rejected_count,
            plan=result.ranked.plan,
        ),
    )


@router.get("/items/{item_id}", response_model=Item)
async def get_item(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> Item:
    return await _require_item(item_id, repository)


@router.get("/items/{item_id}/artifacts", response_model=ItemArtifactsResponse)
async def get_item_artifacts(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> ItemArtifactsResponse:
    item = await _require_item(item_id, repository)
    return ItemArtifactsResponse(item_id=item.item_id, artifacts=item.artifacts)


@router.get("/items/{item_id}/schema", response_model=ItemSchemaResponse)
async def get_item_schema(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> ItemSchemaResponse:
    item = await _require_item(item_id, repository)
    if item.tool is not None:
        return ItemSchemaResponse(
            item_id=item.item_id,
            type="tool",
            item_schema=item.tool.mcp_schema,
        )
    if item.agent is not None:
        return ItemSchemaResponse(
            item_id=item.item_id,
            type="agent",
            item_schema=item.agent.agent_card,
        )
    raise HTTPException(status_code=404, detail="Item schema is not available")


@router.get("/items/{item_id}/provenance", response_model=ItemProvenanceResponse)
async def get_item_provenance(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> ItemProvenanceResponse:
    item = await _require_item(item_id, repository)
    return ItemProvenanceResponse(
        item_id=item.item_id,
        provenance=item.provenance,
        evidence=item.evidence,
    )


@router.post(
    "/items/{item_id}/test",
    response_model=DeferredExecutionResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def test_tool(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> DeferredExecutionResponse:
    item = await _require_item(item_id, repository)
    if item.tool is None:
        raise HTTPException(status_code=409, detail="Item is not an MCP tool")
    return DeferredExecutionResponse(
        item_id=item.item_id,
        status="not_implemented",
        detail="MCP sandbox execution is implemented in Phase 15.",
    )


@router.post(
    "/items/{item_id}/agent-test",
    response_model=DeferredExecutionResponse,
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def test_agent(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> DeferredExecutionResponse:
    item = await _require_item(item_id, repository)
    if item.agent is None:
        raise HTTPException(status_code=409, detail="Item is not an A2A agent")
    return DeferredExecutionResponse(
        item_id=item.item_id,
        status="not_implemented",
        detail="A2A sandbox execution is implemented in Phase 16.",
    )


@router.get("/items/{item_id}/test/{run_id}", response_model=TestRunResponse)
async def get_test_run(
    item_id: UUID,
    run_id: UUID,
    repository: TestRunRepository = Depends(get_test_run_repository),
) -> TestRunResponse:
    test_run = await _require_test_run(item_id, run_id, repository)
    return TestRunResponse(test_run=test_run)


@router.get("/items/{item_id}/test/{run_id}/logs")
async def stream_test_run_logs(
    item_id: UUID,
    run_id: UUID,
    repository: TestRunRepository = Depends(get_test_run_repository),
) -> EventSourceResponse:
    test_run = await _require_test_run(item_id, run_id, repository)

    async def events():
        for index, line in enumerate(test_run.logs):
            yield {
                "event": "log",
                "id": str(index),
                "data": json.dumps({"message": line}),
            }
        yield {
            "event": "status",
            "data": json.dumps({"status": test_run.status.value}),
        }

    return EventSourceResponse(events())


async def _require_item(item_id: UUID, repository: ItemRepository) -> Item:
    item = await repository.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


async def _require_test_run(
    item_id: UUID,
    run_id: UUID,
    repository: TestRunRepository,
) -> TestRun:
    test_run = await repository.get(run_id)
    if test_run is None or test_run.item_id != item_id:
        raise HTTPException(status_code=404, detail="Test run not found")
    return test_run


def _preferred_type(value: SearchType) -> PreferredType:
    return value
