from __future__ import annotations

import json
from datetime import datetime, timezone
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
    IntegrationSnippet,
    ItemArtifactsResponse,
    ItemClassificationResponse,
    ItemProvenanceResponse,
    ItemSchemaResponse,
    RepositoryTreeItem,
    RepositoryTreeResponse,
    SearchMetadata,
    SearchResponse,
    SearchResultItem,
    SearchToolItem,
    SourceFileResponse,
    SourcePreview,
    TestRunResponse,
)
from app.artifacts.integration_synthesizer import synthesize_integration
from app.artifacts.source_resolver import get_repository_tree, get_source_file, resolve_source
from app.db.repositories import ItemRepository, TestRunRepository
from app.models import Item, TestRun
from app.query.planner import PreferredType
from app.sandbox.classifier import classify_tool
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
            source_priority=ranked.source_priority,
            classification=classify_tool(ranked.item),
        )
        for ranked in result.ranked.results
    ]
    tools = [
        SearchToolItem(
            item_id=ranked.item.item_id,
            name=ranked.item.name,
            description=ranked.item.description,
            server_id=ranked.item.tool.server_id,
            tool_name=ranked.item.tool.tool_name,
            mcp_schema=ranked.item.tool.mcp_schema,
            source=ranked.item.source,
            final_score=ranked.final_score,
            reliability=ranked.reliability,
        )
        for ranked in result.ranked.results
        if ranked.item.tool is not None
    ]
    return SearchResponse(
        results=rows,
        tools=tools,
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


@router.get(
    "/items/{item_id}/classification",
    response_model=ItemClassificationResponse,
    summary="Classify tool for live testing",
    tags=["tool-test"],
)
async def get_item_classification(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Return the testability classification for a specific item.

    The frontend uses this to decide which action button (Test Tool /
    Run Locally / hidden) to render. The classification is derived
    from the item's `artifacts.config_files` at request time and is
    not persisted.
    """
    item = await _require_item(item_id, repository)
    return ItemClassificationResponse(
        item_id=item.item_id,
        classification=classify_tool(item),
    )


@router.get("/items/{item_id}/artifacts", response_model=ItemArtifactsResponse)
async def get_item_artifacts(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> ItemArtifactsResponse:
    item = await _require_item(item_id, repository)

    integration_result, integration_cache_path = synthesize_integration(item)
    source_result, source_readme_cache_path = await resolve_source(item)

    # Persist any newly-written disk cache pointers in a single DB update
    # (avoid two sequential writes stomping on each other's stale snapshot
    # of item.artifacts).
    artifact_updates: dict = {}
    if integration_cache_path and integration_cache_path != item.artifacts.integration_cache_path:
        artifact_updates["integration_cache_path"] = integration_cache_path
    if (
        source_readme_cache_path
        and source_readme_cache_path != item.artifacts.source_readme_cache_path
    ):
        artifact_updates["source_readme_cache_path"] = source_readme_cache_path

    if artifact_updates:
        await repository.update(item_id, {
            "artifacts": {
                **item.artifacts.model_dump(mode="json"),
                **artifact_updates,
            }
        })

    return ItemArtifactsResponse(
        item_id=item.item_id,
        artifacts=item.artifacts,
        integration=IntegrationSnippet(
            available=integration_result.available,
            snippet=integration_result.snippet,
            source=integration_result.source,
            note=integration_result.note,
        ),
        source_preview=SourcePreview(
            available=source_result.available,
            language=source_result.language,
            content=source_result.content,
            note=source_result.note,
        ),
    )


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


@router.get("/items/{item_id}/source/tree", response_model=RepositoryTreeResponse)
async def get_source_tree(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
) -> RepositoryTreeResponse:
    """Fetch repository file tree structure with caching."""
    item = await _require_item(item_id, repository)

    tree_result, tree_cache_path = await get_repository_tree(item)

    # Update cache path (and full-download timestamp) in DB if a new tree
    # was just fetched+cached. From here on, /source/files/{path} calls for
    # this item are served straight from disk - GitHub isn't hit again.
    if tree_cache_path and tree_cache_path != item.artifacts.source_tree_cache_path:
        await repository.update(item_id, {
            "artifacts": {
                **item.artifacts.model_dump(mode="json"),
                "source_tree_cache_path": tree_cache_path,
                "source_downloaded_at": datetime.now(timezone.utc).isoformat(),
            }
        })

    tree_items = [
        RepositoryTreeItem(
            path=node["path"],
            type=node["type"],
            size=node.get("size"),
        )
        for node in (tree_result.tree or [])
    ]

    return RepositoryTreeResponse(
        item_id=item.item_id,
        available=tree_result.available,
        tree=tree_items,
        note=tree_result.note,
    )


@router.get("/items/{item_id}/source/files/{file_path:path}", response_model=SourceFileResponse)
async def get_source_file_content(
    item_id: UUID,
    file_path: str,
    repository: ItemRepository = Depends(get_item_repository),
) -> SourceFileResponse:
    """Fetch individual source file content with caching."""
    item = await _require_item(item_id, repository)

    file_result = await get_source_file(item, file_path)

    return SourceFileResponse(
        item_id=item.item_id,
        file_path=file_path,
        available=file_result.available,
        language=file_result.language,
        content=file_result.content,
        note=file_result.note,
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