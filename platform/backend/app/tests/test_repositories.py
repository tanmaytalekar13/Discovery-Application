from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.db.repositories import (
    ItemRepository,
    TestRunRepository as TestRunRepo,
)
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
    TestRun as TestRunModel,
    TestRunStatus as TestRunStatusModel,
    ToolMetadata,
)


class FakeArcadeDB:
    """
    In-memory fake ArcadeDB client used for repository unit tests.
    """

    def __init__(self) -> None:
        self.commands: list[dict] = []
        self.item_records: dict[str, dict] = {}
        self.test_run_records: dict[str, dict] = {}
        self.edges: list[dict] = []

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

        # ----------------------------------------------------
        # Graph edges
        #
        # IMPORTANT:
        # This must be checked before SELECT FROM ITEM
        # because CREATE EDGE contains SELECT statements.
        # ----------------------------------------------------

        if normalized.startswith("CREATE EDGE"):
            # Real repositories.py embeds resolved @rid values
            # directly in the SQL text (ArcadeDB's CREATE EDGE
            # FROM/TO clause requires literal RIDs, not bind
            # params) — so params is None here, by design.
            self.edges.append(
                {
                    "command": command,
                    "params": params,
                }
            )

            return {
                "result": [],
            }

        # ----------------------------------------------------
        # @rid lookups (used by create_edge to resolve
        # logical ids like item_id/run_id to an ArcadeDB @rid)
        # ----------------------------------------------------

        if normalized.startswith("SELECT @RID FROM ITEM"):
            assert params is not None

            item_id = str(params["value"])

            if item_id in self.item_records:
                return {
                    "result": [{"@rid": f"#1:{item_id}"}],
                }

            return {
                "result": [],
            }

        if normalized.startswith("SELECT @RID FROM TESTRUN"):
            assert params is not None

            run_id = str(params["value"])

            if run_id in self.test_run_records:
                return {
                    "result": [{"@rid": f"#2:{run_id}"}],
                }

            return {
                "result": [],
            }

        # ----------------------------------------------------
        # Item
        # ----------------------------------------------------

        if "CREATE VERTEX ITEM" in normalized:
            assert params is not None

            payload = params["payload"]
            item_id = str(payload["item_id"])

            record = {
                "item_id": item_id,
                "type": payload["type"],
                "name": payload["name"],
                "description": payload["description"],
                "source_type": payload["source_type"],
                "source_id": payload["source_id"],
                "source_url": payload["source_url"],
                "version": payload["version"],
                "status": payload["status"],
                "reliability_score": payload[
                    "reliability_score"
                ],
                "reliability_confidence": payload[
                    "reliability_confidence"
                ],
                "scoring_version": payload[
                    "scoring_version"
                ],
                "last_evaluated": payload[
                    "last_evaluated"
                ],
                "first_seen": payload["first_seen"],
                "last_seen": payload["last_seen"],
                "last_synced": payload["last_synced"],
                "tool": payload["tool"],
                "agent": payload["agent"],
                "artifacts": payload["artifacts"],
                "embedding": payload["embedding"],
            }

            self.item_records[item_id] = record

            return {
                "result": [record],
            }

        if "SELECT FROM ITEM" in normalized:
            assert params is not None

            record = self.item_records.get(
                str(params["item_id"])
            )

            return {
                "result": [record] if record else [],
            }

        if "UPDATE ITEM" in normalized:
            assert params is not None

            item_id = str(params["item_id"])
            record = self.item_records.get(item_id)

            if record is None:
                return {
                    "result": [],
                }

            for key, value in params.items():
                if key != "item_id":
                    record[key] = value

            return {
                "result": [record],
            }

        if "DELETE FROM ITEM" in normalized:
            assert params is not None

            item_id = str(params["item_id"])

            if item_id in self.item_records:
                del self.item_records[item_id]

                return {
                    "count": 1,
                }

            return {
                "count": 0,
            }

        # ----------------------------------------------------
        # TestRun
        # ----------------------------------------------------

        if "CREATE VERTEX TESTRUN" in normalized:
            assert params is not None

            payload = params["payload"]
            run_id = str(payload["run_id"])

            input_data = dict(payload["input"])
            input_data.pop("@type", None)

            output_data = dict(payload["output"])
            output_data.pop("@type", None)

            dependencies_data = dict(
                payload["dependencies"]
            )
            dependencies_data.pop("@type", None)

            record = {
                "run_id": run_id,
                "item_id": str(payload["item_id"]),
                "type": payload["type"],
                "started_at": payload["started_at"],
                "completed_at": payload["completed_at"],
                "status": payload["status"],
                "input": input_data,
                "output": output_data,
                "duration_ms": payload["duration_ms"],
                "errors": payload["errors"],
                "logs": payload["logs"],
                "dependencies": dependencies_data,
            }

            self.test_run_records[run_id] = record

            return {
                "result": [record],
            }

        if "SELECT FROM TESTRUN" in normalized:
            assert params is not None

            record = self.test_run_records.get(
                str(params["run_id"])
            )

            return {
                "result": [record] if record else [],
            }

        if "UPDATE TESTRUN" in normalized:
            assert params is not None

            run_id = str(params["run_id"])
            record = self.test_run_records.get(run_id)

            if record is None:
                return {
                    "result": [],
                }

            for key, value in params.items():
                if key != "run_id":
                    record[key] = value

            return {
                "result": [record],
            }

        if "DELETE FROM TESTRUN" in normalized:
            assert params is not None

            run_id = str(params["run_id"])

            if run_id in self.test_run_records:
                del self.test_run_records[run_id]

                return {
                    "count": 1,
                }

            return {
                "count": 0,
            }

        raise AssertionError(
            f"Unexpected ArcadeDB command:\n{command}"
        )


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
                    },
                },
                "required": [
                    "query",
                ],
            },
        ),
        artifacts=ArtifactMetadata(
            source_available=True,
            source_url=(
                "https://example.com/spotify-mcp/source"
            ),
            source_code=(
                "def search_tracks(query: str):\n"
                "    return search_spotify(query)\n"
            ),
        ),
    )


@pytest.fixture
def agent_item() -> Item:
    now = datetime.now(timezone.utc)

    return Item(
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


@pytest.fixture
def test_run(
    tool_item: Item,
) -> TestRunModel:
    return TestRunModel(
        run_id=uuid4(),
        item_id=tool_item.item_id,
        type="tool",
        started_at=datetime.now(timezone.utc),
        status=TestRunStatusModel.RUNNING,
        input={
            "query": "Taylor Swift",
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
    assert stored["embedding"] is None


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

    assert result.tool is not None
    assert result.tool.tool_name == "search_tracks"

    assert result.artifacts.source_code is not None


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
async def test_update_empty_item(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    result = await repository.update(
        tool_item.item_id,
        {},
    )

    assert result is not None
    assert result.item_id == tool_item.item_id


@pytest.mark.asyncio
async def test_update_rejects_invalid_field(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    with pytest.raises(ValueError):
        await repository.update(
            tool_item.item_id,
            {
                "invalid_field": "value",
            },
        )


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

    # Pydantic HttpUrl normalizes the URL with a trailing slash.
    assert (
        stored["agent"]["endpoint"]
        == "http://financial-agent:9000/"
    )

    assert stored["agent"]["skills"] == [
        "financial-research",
        "market-summary",
    ]


# ============================================================
# Graph Edges
# ============================================================


@pytest.mark.asyncio
async def test_create_uses_tool_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
    agent_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)
    await repository.create(agent_item)

    await repository.create_edge(
        edge_type="USES_TOOL",
        from_type="Item",
        from_id=agent_item.item_id,
        to_type="Item",
        to_id=tool_item.item_id,
    )

    assert len(fake_db.edges) == 1

    edge = fake_db.edges[0]

    assert "CREATE EDGE USES_TOOL" in edge[
        "command"
    ]

    # create_edge resolves logical ids to @rid values and
    # embeds them directly in the SQL text (no bind params).
    assert f"#1:{agent_item.item_id}" in edge[
        "command"
    ]

    assert f"#1:{tool_item.item_id}" in edge[
        "command"
    ]


@pytest.mark.asyncio
async def test_create_test_run_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
    test_run: TestRunModel,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    test_run_repository = TestRunRepo(
        fake_db
    )

    await test_run_repository.create(test_run)

    await repository.create_edge(
        edge_type="HAS_TEST_RUN",
        from_type="Item",
        from_id=tool_item.item_id,
        to_type="TestRun",
        to_id=test_run.run_id,
        to_field="run_id",
    )

    assert len(fake_db.edges) == 1

    edge = fake_db.edges[0]

    assert "CREATE EDGE HAS_TEST_RUN" in edge[
        "command"
    ]

    # create_edge resolves logical ids to @rid values and
    # embeds them directly in the SQL text (no bind params).
    assert f"#1:{tool_item.item_id}" in edge[
        "command"
    ]

    assert f"#2:{test_run.run_id}" in edge[
        "command"
    ]


@pytest.mark.asyncio
async def test_create_edge_rejects_unknown_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    with pytest.raises(ValueError):
        await repository.create_edge(
            edge_type="INVALID_EDGE",
            from_type="Item",
            from_id=tool_item.item_id,
            to_type="Item",
            to_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_create_edge_rejects_invalid_source_type(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    with pytest.raises(ValueError):
        await repository.create_edge(
            edge_type="USES_TOOL",
            from_type="InvalidType",
            from_id=tool_item.item_id,
            to_type="Item",
            to_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_create_edge_rejects_invalid_target_type(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    with pytest.raises(ValueError):
        await repository.create_edge(
            edge_type="USES_TOOL",
            from_type="Item",
            from_id=tool_item.item_id,
            to_type="InvalidType",
            to_id=uuid4(),
        )


# ============================================================
# TestRun Repository
# ============================================================


@pytest.mark.asyncio
async def test_create_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

    result = await repository.create(test_run)

    assert result.run_id == test_run.run_id
    assert result.item_id == test_run.item_id
    assert result.status == TestRunStatusModel.RUNNING

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
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

    await repository.create(test_run)

    result = await repository.get(
        test_run.run_id
    )

    assert result is not None
    assert result.run_id == test_run.run_id
    assert result.item_id == test_run.item_id
    assert result.status == TestRunStatusModel.RUNNING


@pytest.mark.asyncio
async def test_get_missing_test_run(
    fake_db: FakeArcadeDB,
) -> None:
    repository = TestRunRepo(fake_db)

    result = await repository.get(uuid4())

    assert result is None


@pytest.mark.asyncio
async def test_update_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

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
    assert result.status == TestRunStatusModel.SUCCESS
    assert result.duration_ms == 1250
    assert result.output == {
        "tracks": 10,
    }


@pytest.mark.asyncio
async def test_update_empty_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

    await repository.create(test_run)

    result = await repository.update(
        test_run.run_id,
        {},
    )

    assert result is not None
    assert result.run_id == test_run.run_id


@pytest.mark.asyncio
async def test_update_test_run_rejects_invalid_field(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

    await repository.create(test_run)

    with pytest.raises(ValueError):
        await repository.update(
            test_run.run_id,
            {
                "invalid_field": "value",
            },
        )


@pytest.mark.asyncio
async def test_delete_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

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
    repository = TestRunRepo(fake_db)

    deleted = await repository.delete(uuid4())

    assert deleted is False