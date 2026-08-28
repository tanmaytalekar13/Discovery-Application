import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.db.repositories import ItemRepository, TestRunRepository
from app.models import (
    AgentMetadata,
    ArtifactMetadata,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemStatus,
    ItemType,
    Reliability,
    SourceType,
    TestRun,
    TestRunStatus,
    ToolMetadata,
)


async def seed() -> None:
    settings = get_settings()
    db = ArcadeDBClient(settings)

    item_repository = ItemRepository(db)
    test_run_repository = TestRunRepository(db)

    now = datetime.now(timezone.utc)

    # --------------------------------------------------------
    # Tool
    # --------------------------------------------------------

    tool = Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name="search_tracks",
        description="Search Spotify tracks by query.",
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="spotify-mcp-server",
            url="https://example.com/spotify-mcp",
        ),
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(
            score=0.91,
            confidence=0.88,
            scoring_version="v1",
            last_evaluated=now,
        ),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
        tool=ToolMetadata(
            server_id="spotify-mcp-server",
            tool_name="search_tracks",
            mcp_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                    }
                },
                "required": ["query"],
            },
        ),
        artifacts=ArtifactMetadata(
            source_available=True,
            source_url="https://example.com/spotify-mcp/source",
            source_code=(
                "def search_tracks(query: str):\n"
                "    return search_spotify(query)\n"
            ),
        ),
    )

    # --------------------------------------------------------
    # Agent
    # --------------------------------------------------------

    agent = Item(
        item_id=uuid4(),
        type=ItemType.AGENT,
        name="Financial Research Agent",
        description=(
            "Researches financial information "
            "and produces summaries."
        ),
        source=DiscoverySource(
            type=SourceType.A2A_CATALOG,
            id="financial-research-agent",
            url="https://example.com/a2a",
        ),
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(
            score=0.87,
            confidence=0.82,
            scoring_version="v1",
            last_evaluated=now,
        ),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
        agent=AgentMetadata(
            endpoint="http://financial-agent:9000",
            agent_card={
                "name": "Financial Research Agent",
                "description": (
                    "Researches financial information."
                ),
            },
            skills=[
                "financial-research",
                "market-summary",
            ],
            capabilities=[
                "task-processing",
            ],
            declared_dependencies=[
                "search_tracks",
            ],
        ),
    )

    # --------------------------------------------------------
    # Persist Items
    # --------------------------------------------------------

    await item_repository.create(tool)
    await item_repository.create(agent)

    # --------------------------------------------------------
    # TestRun for Tool
    # --------------------------------------------------------

    tool_test_run = TestRun(
        run_id=uuid4(),
        item_id=tool.item_id,
        type="tool",
        started_at=now,
        completed_at=now,
        status=TestRunStatus.SUCCESS,
        input={
            "query": "Taylor Swift",
        },
        output={
            "tracks": [
                "Love Story",
                "Anti-Hero",
            ],
        },
        duration_ms=420,
        errors=[],
        logs=[
            "Tool execution started",
            "Tool execution completed",
        ],
        dependencies={
            "declared": [],
            "observed": [],
            "unexpected": [],
        },
    )

    # --------------------------------------------------------
    # TestRun for Agent
    # --------------------------------------------------------

    agent_test_run = TestRun(
        run_id=uuid4(),
        item_id=agent.item_id,
        type="agent",
        started_at=now,
        completed_at=now,
        status=TestRunStatus.SUCCESS,
        input={
            "task": "Summarize recent market activity",
        },
        output={
            "summary": (
                "Market activity was summarized successfully."
            ),
        },
        duration_ms=1350,
        errors=[],
        logs=[
            "Agent execution started",
            "Agent execution completed",
        ],
        dependencies={
            "declared": [
                "search_tracks",
            ],
            "observed": [
                "search_tracks",
            ],
            "unexpected": [],
        },
    )

    await test_run_repository.create(tool_test_run)
    await test_run_repository.create(agent_test_run)

    print("Seed completed successfully.")
    print(f"Tool:  {tool.item_id}")
    print(f"Agent: {agent.item_id}")
    print(f"Tool TestRun:  {tool_test_run.run_id}")
    print(f"Agent TestRun: {agent_test_run.run_id}")


if __name__ == "__main__":
    asyncio.run(seed())