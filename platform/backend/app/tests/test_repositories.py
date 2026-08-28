from datetime import datetime, timezone
from uuid import uuid4

import pytest

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


class FakeArcadeDB:
    """
    In-memory fake for repository unit tests.

    It records commands sent by the repository without
    requiring a running ArcadeDB instance.
    """

    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.item_records: dict[str, dict] = {}
        self.test_run_records: dict[str, dict] = {}

    async def command(
        self,
        language: str,
        command: str,
        params: dict | None = None,
    ) -> dict:
        self.commands.append(
            {
                "language": language,
                "command": command,
                "params": params,
            }
        )

        normalized = " ".join(command.split()).upper()

        if "CREATE DOCUMENT ITEM" in normalized:
            assert params is not None
            item_id = params["item_id"]
            self.item_records[item_id] = self._item_record(params)

            return {
                "result": [
                    self.item_records[item_id],
                ]
            }

        if "SELECT FROM ITEM" in normalized:
            assert params is not None
            record = self.item_records.get(
                params["item_id"]
            )

            return {
                "result": [record] if record else []
            }

        if "UPDATE ITEM" in normalized:
            assert params is not None

            item_id = params["item_id"]
            record = self.item_records.get(item_id)

            if record is None:
                return {"result": []}

            for key, value in params.items():
                if key != "item_id":
                    record[key] = value

            return {
                "result": [record],
            }

        if "DELETE FROM ITEM" in normalized:
            assert params is not None

            item_id = params["item_id"]

            if item_id in self.item_records:
                del self.item_records[item_id]
                return {"count": 1}

            return {"count": 0}

        if "CREATE EDGE" in normalized:
            return {"result": []}

        if "CREATE DOCUMENT TESTRUN" in normalized:
            assert params is not None

            run_id = params["run_id"]

            record = {
                "run_id": run_id,
                "item_id": params["item_id"],
                "type": params["type"],
                "started_at": params["started_at"],
                "completed_at": params["completed_at"],
                "status": params["status"],
                "input": params["input"],
                "output": params["output"],
                "duration_ms": params["duration_ms"],
                "errors": params["errors"],
                "logs": params["logs"],
                "dependencies": params["dependencies"],
            }

            self.test_run_records[run_id] = record

            return {
                "result": [record],
            }

        if "SELECT FROM TESTRUN" in normalized:
            assert params is not None

            record = self.test_run_records.get(
                params["run_id"]
            )

            return {
                "result": [record] if record else []
            }

        if "UPDATE TESTRUN" in normalized:
            assert params is not None

            run_id = params["run_id"]
            record = self.test_run_records.get(run_id)

            if record is None:
                return {"result": []}

            for key, value in params.items():
                if key != "run_id":
                    record[key] = value

            return {
                "result": [record],
            }

        if "DELETE FROM TESTRUN" in normalized:
            assert params is not None

            run_id = params["run_id"]

            if run_id in self.test_run_records:
                del self.test_run_records[run_id]
                return {"count": 1}

            return {"count": 0}

        raise AssertionError(
            f"Unexpected command in FakeArcadeDB:\n{command}"
        )

    @staticmethod
    def _item_record(params: dict) -> dict:
        return {
            "item_id": params["item_id"],
            "type": params["type"],
            "name": params["name"],
            "description": params["description"],
            "source_type": params["source_type"],
            "source_id": params["source_id"],
            "source_url": params["source_url"],
            "version": params["version"],
            "status": params["status"],
            "reliability_score": params[
                "reliability_score"
            ],
            "reliability_confidence": params[
                "reliability_confidence"
            ],
            "scoring_version": params[
                "scoring_version"
            ],
            "last_evaluated": params[
                "last_evaluated"
            ],
            "first_seen": params["first_seen"],
            "last_seen": params["last_seen"],
            "last_synced": params["last_synced"],
            "tool": params["tool"],
            "agent": params["agent"],
            "artifacts": params["artifacts"],
            "embedding": params["embedding"],
        }


@pytest.fixture
def fake_db() -> FakeArcadeDB:
    return FakeArcadeDB()


@pytest.fixture
def tool_item() -> Item:
    now = datetime.now(timezone.utc)

    return Item(
        item_id=uuid4(),
        type=ItemType.TOOL,
        name="search_tracks",
        description="Search Spotify tracks",
        source=DiscoverySource(
            type=SourceType.MCP_REGISTRY,
            id="spotify-mcp",
        ),
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(
            score=0.91,
            confidence=0.88,
            last_evaluated=now,
        ),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
        tool=ToolMetadata(
            server_id="spotify-mcp",
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
            source_url="https://example.com/source",
            source_code="def search_tracks(): pass",
        ),
    )


@pytest.fixture
def agent_item() -> Item:
    now = datetime.now(timezone.utc)

    return Item(
        item_id=uuid4(),
        type=ItemType.AGENT,
        name="Financial Research Agent",
        description="Researches financial information.",
        source=DiscoverySource(
            type=SourceType.A2A_CATALOG,
            id="financial-agent",
        ),
        version="1.0.0",
        status=ItemStatus.ACTIVE,
        reliability=Reliability(
            score=0.87,
            confidence=0.82,
            last_evaluated=now,
        ),
        discovery=DiscoveryMetadata(
            first_seen=now,
            last_seen=now,
            last_synced=now,
        ),
        agent=AgentMetadata(
            endpoint="http://financial-agent",
            agent_card={
                "name": "Financial Research Agent",
            },
            skills=[
                "financial-research",
            ],
            capabilities=[
                "task-processing",
            ],
            declared_dependencies=[],
        ),
    )


@pytest.fixture
def test_run(tool_item: Item) -> TestRun:
    return TestRun(
        run_id=uuid4(),
        item_id=tool_item.item_id,
        type="tool",
        started_at=datetime.now(timezone.utc),
        status=TestRunStatus.RUNNING,
        input={
            "query": "Taylor Swift"
        },
    )


# ============================================================
# Item Repository
# ============================================================


@pytest.mark.asyncio
async def test_create_item(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    result = await repository.create(tool_item)

    assert result.item_id == tool_item.item_id
    assert result.name == "search_tracks"

    stored = fake_db.item_records[
        str(tool_item.item_id)
    ]

    assert stored["type"] == "tool"
    assert stored["name"] == "search_tracks"
    assert stored["tool"] is not None
    assert stored["artifacts"] is not None


@pytest.mark.asyncio
async def test_get_item(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    result = await repository.get(
        tool_item.item_id
    )

    assert result is not None
    assert result.item_id == tool_item.item_id
    assert result.name == tool_item.name
    assert result.description == tool_item.description
    assert result.type == ItemType.TOOL


@pytest.mark.asyncio
async def test_get_missing_item(
    fake_db: FakeArcadeDB,
) -> None:
    repository = ItemRepository(fake_db)

    result = await repository.get(uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_update_item(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    result = await repository.update(
        tool_item.item_id,
        {
            "name": "updated_search_tracks",
            "status": "deprecated",
        },
    )

    assert result is not None
    assert result.name == "updated_search_tracks"
    assert result.status == ItemStatus.DEPRECATED


@pytest.mark.asyncio
async def test_delete_item(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    deleted = await repository.delete(
        tool_item.item_id
    )

    assert deleted is True

    result = await repository.get(
        tool_item.item_id
    )

    assert result is None


@pytest.mark.asyncio
async def test_delete_missing_item(
    fake_db: FakeArcadeDB,
) -> None:
    repository = ItemRepository(fake_db)

    deleted = await repository.delete(uuid4())

    assert deleted is False


# ============================================================
# Agent Persistence
# ============================================================


@pytest.mark.asyncio
async def test_create_agent_item(
    fake_db: FakeArcadeDB,
    agent_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    result = await repository.create(agent_item)

    assert result.type == ItemType.AGENT

    stored = fake_db.item_records[
        str(agent_item.item_id)
    ]

    assert stored["agent"] is not None
    assert (
        stored["agent"]["endpoint"]
        == "http://financial-agent"
    )


# ============================================================
# Graph Edge
# ============================================================


@pytest.mark.asyncio
async def test_create_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
    agent_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)
    await repository.create(agent_item)

    await repository.create_edge(
        "USES_TOOL",
        agent_item.item_id,
        tool_item.item_id,
    )

    command = fake_db.commands[-1]

    assert "CREATE EDGE USES_TOOL" in command[
        "command"
    ]
    assert command["params"]["from_id"] == str(
        agent_item.item_id
    )
    assert command["params"]["to_id"] == str(
        tool_item.item_id
    )


@pytest.mark.asyncio
async def test_create_edge_rejects_unknown_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    with pytest.raises(ValueError):
        await repository.create_edge(
            "INVALID_EDGE",
            tool_item.item_id,
            uuid4(),
        )


# ============================================================
# TestRun Repository
# ============================================================


@pytest.mark.asyncio
async def test_create_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRun,
) -> None:
    repository = TestRunRepository(fake_db)

    result = await repository.create(test_run)

    assert result.run_id == test_run.run_id
    assert result.status == TestRunStatus.RUNNING

    stored = fake_db.test_run_records[
        str(test_run.run_id)
    ]

    assert stored["item_id"] == str(
        test_run.item_id
    )
    assert stored["status"] == "running"


@pytest.mark.asyncio
async def test_get_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRun,
) -> None:
    repository = TestRunRepository(fake_db)

    await repository.create(test_run)

    result = await repository.get(
        test_run.run_id
    )

    assert result is not None
    assert result.run_id == test_run.run_id
    assert result.item_id == test_run.item_id
    assert result.status == TestRunStatus.RUNNING


@pytest.mark.asyncio
async def test_get_missing_test_run(
    fake_db: FakeArcadeDB,
) -> None:
    repository = TestRunRepository(fake_db)

    result = await repository.get(uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_update_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRun,
) -> None:
    repository = TestRunRepository(fake_db)

    await repository.create(test_run)

    result = await repository.update(
        test_run.run_id,
        {
            "status": "success",
            "duration_ms": 1250,
            "output": {
                "tracks": 10,
            },
        },
    )

    assert result is not None
    assert result.status == TestRunStatus.SUCCESS
    assert result.duration_ms == 1250
    assert result.output == {
        "tracks": 10,
    }


@pytest.mark.asyncio
async def test_delete_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRun,
) -> None:
    repository = TestRunRepository(fake_db)

    await repository.create(test_run)

    deleted = await repository.delete(
        test_run.run_id
    )

    assert deleted is True

    result = await repository.get(
        test_run.run_id
    )

    assert result is None


@pytest.mark.asyncio
async def test_delete_missing_test_run(
    fake_db: FakeArcadeDB,
) -> None:
    repository = TestRunRepository(fake_db)

    deleted = await repository.delete(uuid4())

    assert deleted is False