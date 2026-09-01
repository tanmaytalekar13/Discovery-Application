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
        self.discovery_source_records: dict[str, dict] = {}
        self.discovery_evidence_records: dict[str, dict] = {}
        self.reliability_evaluation_records: dict[str, dict] = {}
        self.discovery_rejection_records: dict[str, dict] = {}
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

        if normalized.startswith("SELECT @RID FROM DISCOVERYSOURCE"):
            assert params is not None

            source_key = str(params["value"])
            if source_key in self.discovery_source_records:
                return {
                    "result": [{"@rid": f"#3:{source_key}"}],
                }

            return {
                "result": [],
            }

        if normalized.startswith("SELECT @RID FROM DISCOVERYEVIDENCE"):
            assert params is not None

            evidence_id = str(params["value"])
            if evidence_id in self.discovery_evidence_records:
                return {
                    "result": [{"@rid": f"#4:{evidence_id}"}],
                }

            return {
                "result": [],
            }

        if normalized.startswith("SELECT @RID FROM RELIABILITYEVALUATION"):
            assert params is not None

            evaluation_id = str(params["value"])
            if evaluation_id in self.reliability_evaluation_records:
                return {
                    "result": [{"@rid": f"#5:{evaluation_id}"}],
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
                "reliability_score": payload["reliability_score"],
                "reliability_confidence": payload["reliability_confidence"],
                "scoring_version": payload["scoring_version"],
                "last_evaluated": payload["last_evaluated"],
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
            if "item_id" not in params:
                records = list(self.item_records.values())
                if "type" in params:
                    records = [
                        record for record in records if record["type"] == params["type"]
                    ]
                records = [
                    record
                    for record in records
                    if record.get("status", "active") == params["status"]
                ]
                return {
                    "result": records[: params["limit"]],
                }

            record = self.item_records.get(str(params["item_id"]))

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
        # Phase 10 provenance/evidence vertices
        # ----------------------------------------------------

        if "CREATE VERTEX DISCOVERYSOURCE" in normalized:
            assert params is not None

            payload = params["payload"]
            record = dict(payload)
            self.discovery_source_records[str(payload["source_key"])] = record

            return {
                "result": [record],
            }

        if "SELECT FROM DISCOVERYSOURCE" in normalized:
            assert params is not None

            record = self.discovery_source_records.get(str(params["source_key"]))
            if record is None:
                return {
                    "result": [],
                }

            return {
                "result": [
                    {
                        **record,
                        "@rid": f"#3:{record['source_key']}",
                    }
                ],
            }

        if "UPDATE DISCOVERYSOURCE" in normalized:
            assert params is not None

            record = self.discovery_source_records.get(str(params["source_key"]))
            if record is not None:
                record["last_seen"] = params["last_seen"]

            return {
                "result": [record] if record else [],
            }

        if "CREATE VERTEX DISCOVERYEVIDENCE" in normalized:
            assert params is not None

            payload = params["payload"]
            record = dict(payload)
            self.discovery_evidence_records[str(payload["evidence_id"])] = record

            return {
                "result": [record],
            }

        if "SELECT FROM DISCOVERYEVIDENCE" in normalized:
            assert params is not None

            record = self.discovery_evidence_records.get(str(params["evidence_id"]))
            if record is None:
                return {
                    "result": [],
                }

            return {
                "result": [
                    {
                        **record,
                        "@rid": f"#4:{record['evidence_id']}",
                    }
                ],
            }

        if "CREATE VERTEX RELIABILITYEVALUATION" in normalized:
            assert params is not None

            payload = params["payload"]
            record = dict(payload)
            self.reliability_evaluation_records[str(payload["evaluation_id"])] = record

            return {
                "result": [record],
            }

        if "CREATE VERTEX DISCOVERYREJECTION" in normalized:
            assert params is not None

            payload = params["payload"]
            record = dict(payload)
            self.discovery_rejection_records[str(payload["rejection_id"])] = record

            return {
                "result": [record],
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

            dependencies_data = dict(payload["dependencies"])
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

            record = self.test_run_records.get(str(params["run_id"]))

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

        raise AssertionError(f"Unexpected ArcadeDB command:\n{command}")


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
            source_url=("https://example.com/spotify-mcp/source"),
            source_code=(
                "def search_tracks(query: str):\n" "    return search_spotify(query)\n"
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
        description=("Researches financial information " "and produces summaries."),
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
                "description": ("Researches financial information."),
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

    stored = fake_db.item_records[str(tool_item.item_id)]

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

    result = await repository.get(tool_item.item_id)

    assert result is not None
    assert result.item_id == tool_item.item_id
    assert result.name == tool_item.name
    assert result.description == tool_item.description
    assert result.type == ItemType.TOOL

    assert result.tool is not None
    assert result.tool.tool_name == "search_tracks"

    assert result.artifacts.source_code is not None


def test_to_item_backfills_missing_discovery_timestamps(tool_item: Item) -> None:
    row = {
        "item_id": str(tool_item.item_id),
        "canonical_id": tool_item.canonical_id,
        "type": tool_item.type.value,
        "name": tool_item.name,
        "description": tool_item.description,
        "source_type": tool_item.source.type.value,
        "source_id": tool_item.source.id,
        "source_url": str(tool_item.source.url),
        "source_provider": tool_item.source.provider,
        "version": tool_item.version,
        "status": tool_item.status.value,
        "reliability_score": tool_item.reliability.score,
        "reliability_confidence": tool_item.reliability.confidence,
        "scoring_version": tool_item.reliability.scoring_version,
        "last_evaluated": tool_item.reliability.last_evaluated.isoformat(),
        "security_validation": tool_item.reliability.security_validation,
        "reliability_signals": tool_item.reliability.signals,
        "reliability_reasons": tool_item.reliability.reasons,
        "first_seen": None,
        "last_seen": None,
        "last_synced": None,
        "tool": tool_item.tool.model_dump(mode="json"),
        "agent": None,
        "artifacts": tool_item.artifacts.model_dump(mode="json"),
        "provenance": [],
        "evidence_summary": [],
        "embedding": None,
    }

    result = ItemRepository._to_item(row)

    assert result.discovery.first_seen == tool_item.reliability.last_evaluated
    assert result.discovery.last_seen == tool_item.reliability.last_evaluated
    assert result.discovery.last_synced == tool_item.reliability.last_evaluated


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

    deleted = await repository.delete(tool_item.item_id)

    assert deleted is True

    result = await repository.get(tool_item.item_id)

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

    stored = fake_db.item_records[str(agent_item.item_id)]

    assert stored["agent"] is not None

    # Pydantic HttpUrl normalizes the URL with a trailing slash.
    assert stored["agent"]["endpoint"] == "http://financial-agent:9000/"

    assert stored["agent"]["skills"] == [
        "financial-research",
        "market-summary",
    ]


@pytest.mark.asyncio
async def test_upsert_catalog_item_persists_new_phase10_record(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)
    tool_item.provenance = [tool_item.source]

    class Evaluation:
        score = 0.91
        confidence = 0.88
        approved = True
        signals = {"protocol_validation": 1.0}
        reasons = ["protocol validation succeeded"]
        security_validation = 1.0

    await repository.upsert_catalog_item(tool_item, Evaluation())

    stored = fake_db.item_records[str(tool_item.item_id)]
    assert stored["item_id"] == str(tool_item.item_id)
    assert stored["name"] == "search_tracks"
    assert fake_db.discovery_source_records
    assert fake_db.reliability_evaluation_records

    edge_commands = [edge["command"] for edge in fake_db.edges]
    assert any(
        "CREATE EDGE HAS_DISCOVERY_SOURCE" in command for command in edge_commands
    )
    assert any(
        "CREATE EDGE HAS_RELIABILITY_EVALUATION" in command for command in edge_commands
    )


@pytest.mark.asyncio
async def test_upsert_catalog_item_refreshes_existing_phase10_record(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    class Evaluation:
        score = 0.93
        confidence = 0.89
        approved = True
        signals = {"protocol_validation": 1.0}
        reasons = ["protocol validation refreshed"]
        security_validation = 1.0

    await repository.create(tool_item)
    tool_item.description = "Updated Spotify search tool"

    await repository.upsert_catalog_item(tool_item, Evaluation())

    stored = fake_db.item_records[str(tool_item.item_id)]
    assert stored["description"] == "Updated Spotify search tool"
    assert not any(
        "SET item_id" in " ".join(command["command"].split())
        for command in fake_db.commands
        if "UPDATE Item" in command["command"]
    )


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

    assert "CREATE EDGE USES_TOOL" in edge["command"]

    # create_edge resolves logical ids to @rid values and
    # embeds them directly in the SQL text (no bind params).
    assert f"#1:{agent_item.item_id}" in edge["command"]

    assert f"#1:{tool_item.item_id}" in edge["command"]


@pytest.mark.asyncio
async def test_create_test_run_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
    test_run: TestRunModel,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    test_run_repository = TestRunRepo(fake_db)

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

    assert "CREATE EDGE HAS_TEST_RUN" in edge["command"]

    # create_edge resolves logical ids to @rid values and
    # embeds them directly in the SQL text (no bind params).
    assert f"#1:{tool_item.item_id}" in edge["command"]

    assert f"#2:{test_run.run_id}" in edge["command"]


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
async def test_create_phase10_discovery_edge(
    fake_db: FakeArcadeDB,
    tool_item: Item,
) -> None:
    repository = ItemRepository(fake_db)

    await repository.create(tool_item)

    source_key = "mcp_registry|spotify-mcp-server|https://example.com/spotify-mcp|None"
    fake_db.discovery_source_records[source_key] = {
        "source_key": source_key,
        "source_type": "mcp_registry",
        "source_id": "spotify-mcp-server",
        "source_url": "https://example.com/spotify-mcp",
        "provider": None,
        "first_seen": "2026-08-31T00:00:00+00:00",
        "last_seen": "2026-08-31T00:00:00+00:00",
    }

    await repository.create_edge(
        edge_type="HAS_DISCOVERY_SOURCE",
        from_type="Item",
        from_id=tool_item.item_id,
        to_type="DiscoverySource",
        to_id=source_key,
        to_field="source_key",
    )

    assert len(fake_db.edges) == 1
    assert "CREATE EDGE HAS_DISCOVERY_SOURCE" in fake_db.edges[0]["command"]


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

    stored = fake_db.test_run_records[str(test_run.run_id)]

    assert stored["item_id"] == str(test_run.item_id)

    assert stored["status"] == "running"


@pytest.mark.asyncio
async def test_get_test_run(
    fake_db: FakeArcadeDB,
    test_run: TestRunModel,
) -> None:
    repository = TestRunRepo(fake_db)

    await repository.create(test_run)

    result = await repository.get(test_run.run_id)

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

    deleted = await repository.delete(test_run.run_id)

    assert deleted is True

    result = await repository.get(test_run.run_id)

    assert result is None


@pytest.mark.asyncio
async def test_delete_missing_test_run(
    fake_db: FakeArcadeDB,
) -> None:
    repository = TestRunRepo(fake_db)

    deleted = await repository.delete(uuid4())

    assert deleted is False
