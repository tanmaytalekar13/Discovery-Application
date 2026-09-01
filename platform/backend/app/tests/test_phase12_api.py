from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_application_search_service,
    get_item_repository,
    get_test_run_repository,
)
from app.main import app
from app.models import (
    AgentMetadata,
    DiscoveryEvidence,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemType,
    Reliability,
    SourceType,
    TestRun as CatalogTestRun,
    TestRunStatus as CatalogTestRunStatus,
    ToolMetadata,
)
from app.query.planner import QueryPlan
from app.query.ranking import RankedItem
from app.query.service import Phase11SearchResult
from app.search.application import ApplicationSearchMetadata, ApplicationSearchResult


def source(name: str = "weather") -> DiscoverySource:
    return DiscoverySource(
        type=SourceType.MCP_REGISTRY,
        id=name,
        url=f"https://example.com/{name}",
        provider="test",
    )


def item(
    *,
    item_id: UUID | None = None,
    item_type: ItemType = ItemType.TOOL,
) -> Item:
    now = datetime.now(timezone.utc)
    src = source()
    return Item(
        item_id=item_id or uuid4(),
        type=item_type,
        name="weather_search",
        description="Find live weather forecast data",
        source=src,
        provenance=[src],
        evidence=[
            DiscoveryEvidence(
                evidence_id=uuid4(),
                kind="protocol_validation",
                statement="validated",
                source=src,
                observed_at=now,
            )
        ],
        reliability=Reliability(score=0.91, confidence=0.82),
        discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
        tool=(
            ToolMetadata(
                server_id="weather-server",
                tool_name="weather_search",
                mcp_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            )
            if item_type is ItemType.TOOL
            else None
        ),
        agent=(
            AgentMetadata(
                endpoint="https://example.com/a2a",
                agent_card={"name": "weather_search"},
                skills=["forecast"],
            )
            if item_type is ItemType.AGENT
            else None
        ),
    )


class FakeApplicationSearchService:
    def __init__(self, result_item: Item):
        self.result_item = result_item
        self.calls = []

    async def search(self, query, *, item_type=None, limit=None):
        self.calls.append({"query": query, "item_type": item_type, "limit": limit})
        plan = QueryPlan(
            keywords=["weather"],
            preferred_type=item_type or "all",
            expanded_query=query,
            used_fallback=True,
        )
        ranked = Phase11SearchResult(
            plan=plan,
            results=(
                RankedItem(
                    item=self.result_item,
                    final_score=0.92,
                    relevance=0.88,
                    reliability=0.91,
                    freshness=1.0,
                    evidence=0.5,
                ),
            ),
        )
        return ApplicationSearchResult(
            ranked=ranked,
            metadata=ApplicationSearchMetadata(
                mode="cached",
                sources_attempted=("arcadedb",),
                sources_succeeded=("arcadedb",),
                cached_results=len(ranked.results),
            ),
        )


class FakeItemRepository:
    def __init__(self, entries: list[Item]):
        self.entries = {entry.item_id: entry for entry in entries}

    async def get(self, item_id):
        return self.entries.get(item_id)


class FakeTestRunRepository:
    def __init__(self, test_run: CatalogTestRun | None):
        self.test_run = test_run

    async def get(self, run_id):
        if self.test_run is not None and self.test_run.run_id == run_id:
            return self.test_run
        return None


def client_with_overrides(overrides):
    app.dependency_overrides.update(overrides)
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_search_contract_returns_ranked_results_and_cached_metadata():
    result_item = item()
    service = FakeApplicationSearchService(result_item)
    client = client_with_overrides({get_application_search_service: lambda: service})

    response = client.get(
        "/api/search", params={"q": "weather tool", "type": "tool", "limit": 5}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["item"]["item_id"] == str(result_item.item_id)
    assert payload["results"][0]["final_score"] == 0.92
    assert payload["metadata"]["mode"] == "cached"
    assert payload["metadata"]["sources_attempted"] == ["arcadedb"]
    assert payload["metadata"]["cached_results"] == 1
    assert service.calls == [{"query": "weather tool", "item_type": "tool", "limit": 5}]


def test_item_detail_artifacts_schema_and_provenance_contracts():
    result_item = item()
    repository = FakeItemRepository([result_item])
    client = client_with_overrides({get_item_repository: lambda: repository})

    detail = client.get(f"/api/items/{result_item.item_id}")
    artifacts = client.get(f"/api/items/{result_item.item_id}/artifacts")
    schema = client.get(f"/api/items/{result_item.item_id}/schema")
    provenance = client.get(f"/api/items/{result_item.item_id}/provenance")

    assert detail.status_code == 200
    assert detail.json()["name"] == "weather_search"
    assert artifacts.status_code == 200
    assert artifacts.json()["artifacts"]["source_available"] is False
    assert schema.status_code == 200
    assert schema.json()["schema"]["properties"]["city"]["type"] == "string"
    assert provenance.status_code == 200
    assert provenance.json()["evidence"][0]["kind"] == "protocol_validation"


def test_execution_routes_are_contract_placeholders_until_sandbox_phases():
    tool = item()
    agent = item(item_type=ItemType.AGENT)
    repository = FakeItemRepository([tool, agent])
    client = client_with_overrides({get_item_repository: lambda: repository})

    tool_response = client.post(
        f"/api/items/{tool.item_id}/test", json={"city": "Pune"}
    )
    agent_response = client.post(
        f"/api/items/{agent.item_id}/agent-test", json={"task": "forecast"}
    )

    assert tool_response.status_code == 501
    assert (
        tool_response.json()["detail"]
        == "MCP sandbox execution is implemented in Phase 15."
    )
    assert agent_response.status_code == 501
    assert (
        agent_response.json()["detail"]
        == "A2A sandbox execution is implemented in Phase 16."
    )


def test_test_run_and_sse_log_contracts():
    item_id = uuid4()
    run_id = uuid4()
    run = CatalogTestRun(
        run_id=run_id,
        item_id=item_id,
        type="mcp",
        started_at=datetime.now(timezone.utc),
        status=CatalogTestRunStatus.SUCCESS,
        logs=["Sandbox created", "test completed"],
    )
    repository = FakeTestRunRepository(run)
    client = client_with_overrides({get_test_run_repository: lambda: repository})

    detail = client.get(f"/api/items/{item_id}/test/{run_id}")
    logs = client.get(f"/api/items/{item_id}/test/{run_id}/logs")

    assert detail.status_code == 200
    assert detail.json()["test_run"]["status"] == "success"
    assert logs.status_code == 200
    assert logs.headers["content-type"].startswith("text/event-stream")
    assert "event: log" in logs.text
    assert '"message": "Sandbox created"' in logs.text
    assert "event: status" in logs.text
    assert '"status": "success"' in logs.text
