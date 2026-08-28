from typing import Any
from uuid import UUID

from app.db.client import ArcadeDBClient
from app.models import Item, TestRun


class ItemRepository:
    """
    Repository for unified Tool/Agent Item records.
    """

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, item: Item) -> Item:
        await self._db.command(
            "sql",
            """
            CREATE DOCUMENT Item CONTENT {
                "item_id": :item_id,
                "type": :type,
                "name": :name,
                "description": :description,

                "source_type": :source_type,
                "source_id": :source_id,
                "source_url": :source_url,

                "version": :version,
                "status": :status,

                "reliability_score": :reliability_score,
                "reliability_confidence": :reliability_confidence,
                "scoring_version": :scoring_version,
                "last_evaluated": :last_evaluated,

                "first_seen": :first_seen,
                "last_seen": :last_seen,
                "last_synced": :last_synced,

                "tool": :tool,
                "agent": :agent,
                "artifacts": :artifacts,

                "embedding": :embedding
            }
            """,
            {
                "item_id": str(item.item_id),
                "type": item.type.value,
                "name": item.name,
                "description": item.description,

                "source_type": item.source.type.value,
                "source_id": item.source.id,
                "source_url": item.source.url,

                "version": item.version,
                "status": item.status.value,

                "reliability_score": item.reliability.score,
                "reliability_confidence": item.reliability.confidence,
                "scoring_version": item.reliability.scoring_version,
                "last_evaluated": item.reliability.last_evaluated,

                "first_seen": item.discovery.first_seen,
                "last_seen": item.discovery.last_seen,
                "last_synced": item.discovery.last_synced,

                "tool": (
                    item.tool.model_dump()
                    if item.tool
                    else None
                ),
                "agent": (
                    item.agent.model_dump(mode="json")
                    if item.agent
                    else None
                ),
                "artifacts": item.artifacts.model_dump(
                    mode="json"
                ),

                "embedding": item.embedding,
            },
        )

        return item

    async def get(self, item_id: UUID) -> Item | None:
        result = await self._db.command(
            "sql",
            """
            SELECT FROM Item
            WHERE item_id = :item_id
            LIMIT 1
            """,
            {
                "item_id": str(item_id),
            },
        )

        records = result.get("result", [])

        if not records:
            return None

        return self._record_to_item(records[0])

    async def update(
        self,
        item_id: UUID,
        fields: dict[str, Any],
    ) -> Item | None:
        if not fields:
            return await self.get(item_id)

        assignments = ", ".join(
            f"{field} = :{field}"
            for field in fields
        )

        await self._db.command(
            "sql",
            f"""
            UPDATE Item
            SET {assignments}
            WHERE item_id = :item_id
            """,
            {
                **fields,
                "item_id": str(item_id),
            },
        )

        return await self.get(item_id)

    async def delete(self, item_id: UUID) -> bool:
        result = await self._db.command(
            "sql",
            """
            DELETE FROM Item
            WHERE item_id = :item_id
            """,
            {
                "item_id": str(item_id),
            },
        )

        return result.get("count", 0) > 0

    async def create_edge(
        self,
        edge_type: str,
        from_type: str,
        from_id: UUID | str,
        to_type: str,
        to_id: UUID | str,
        from_field: str = "item_id",
        to_field: str = "item_id",
    ) -> None:
        """
        Create a graph edge between two records.

        Example:

            Item -> Skill
            Item -> DiscoverySource
            Item -> TestRun

        The edge type and record types are allowlisted so callers
        cannot inject arbitrary SQL identifiers.
        """

        allowed_edges = {
            "USES_TOOL",
            "HAS_SKILL",
            "DISCOVERED_FROM",
            "HAS_TEST_RUN",
        }

        allowed_types = {
            "Item",
            "Tool",
            "Agent",
            "Skill",
            "DiscoverySource",
            "Artifact",
            "TestRun",
        }

        if edge_type not in allowed_edges:
            raise ValueError(
                f"Unsupported edge type: {edge_type}"
            )

        if from_type not in allowed_types:
            raise ValueError(
                f"Unsupported source type: {from_type}"
            )

        if to_type not in allowed_types:
            raise ValueError(
                f"Unsupported target type: {to_type}"
            )

        await self._db.command(
            "sql",
            f"""
            CREATE EDGE {edge_type}
            FROM (
                SELECT FROM {from_type}
                WHERE {from_field} = :from_id
                LIMIT 1
            )
            TO (
                SELECT FROM {to_type}
                WHERE {to_field} = :to_id
                LIMIT 1
            )
            """,
            {
                "from_id": str(from_id),
                "to_id": str(to_id),
            },
        )

    @staticmethod
    def _record_to_item(
        record: dict[str, Any],
    ) -> Item:
        return Item(
            item_id=UUID(str(record["item_id"])),

            type=record["type"],
            name=record["name"],
            description=record["description"],

            source={
                "type": record["source_type"],
                "id": record["source_id"],
                "url": record.get("source_url"),
            },

            version=record.get("version"),
            status=record.get("status", "active"),

            reliability={
                "score": record.get(
                    "reliability_score",
                    0.0,
                ),
                "confidence": record.get(
                    "reliability_confidence",
                    0.0,
                ),
                "scoring_version": record.get(
                    "scoring_version",
                    "v1",
                ),
                "last_evaluated": record.get(
                    "last_evaluated",
                ),
            },

            discovery={
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
                "last_synced": record["last_synced"],
            },

            tool=record.get("tool"),
            agent=record.get("agent"),
            artifacts=record.get(
                "artifacts",
                {},
            ),

            embedding=record.get("embedding"),
        )


class TestRunRepository:
    """
    Repository for persisted tool/agent test runs.
    """

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(
        self,
        test_run: TestRun,
    ) -> TestRun:
        await self._db.command(
            "sql",
            """
            CREATE DOCUMENT TestRun CONTENT {
                "run_id": :run_id,
                "item_id": :item_id,
                "type": :type,

                "started_at": :started_at,
                "completed_at": :completed_at,

                "status": :status,

                "input": :input,
                "output": :output,

                "duration_ms": :duration_ms,

                "errors": :errors,
                "logs": :logs,

                "dependencies": :dependencies
            }
            """,
            {
                "run_id": str(test_run.run_id),
                "item_id": str(test_run.item_id),
                "type": test_run.type,

                "started_at": test_run.started_at,
                "completed_at": test_run.completed_at,

                "status": test_run.status.value,

                "input": test_run.input,
                "output": test_run.output,

                "duration_ms": test_run.duration_ms,

                "errors": test_run.errors,
                "logs": test_run.logs,

                "dependencies": (
                    test_run.dependencies.model_dump()
                ),
            },
        )

        return test_run

    async def get(
        self,
        run_id: UUID,
    ) -> TestRun | None:
        result = await self._db.command(
            "sql",
            """
            SELECT FROM TestRun
            WHERE run_id = :run_id
            LIMIT 1
            """,
            {
                "run_id": str(run_id),
            },
        )

        records = result.get("result", [])

        if not records:
            return None

        return self._record_to_test_run(records[0])

    async def update(
        self,
        run_id: UUID,
        fields: dict[str, Any],
    ) -> TestRun | None:
        if not fields:
            return await self.get(run_id)

        assignments = ", ".join(
            f"{field} = :{field}"
            for field in fields
        )

        await self._db.command(
            "sql",
            f"""
            UPDATE TestRun
            SET {assignments}
            WHERE run_id = :run_id
            """,
            {
                **fields,
                "run_id": str(run_id),
            },
        )

        return await self.get(run_id)

    async def delete(
        self,
        run_id: UUID,
    ) -> bool:
        result = await self._db.command(
            "sql",
            """
            DELETE FROM TestRun
            WHERE run_id = :run_id
            """,
            {
                "run_id": str(run_id),
            },
        )

        return result.get("count", 0) > 0

    @staticmethod
    def _record_to_test_run(
        record: dict[str, Any],
    ) -> TestRun:
        return TestRun.model_validate(
            {
                "run_id": record["run_id"],
                "item_id": record["item_id"],
                "type": record["type"],
                "started_at": record["started_at"],
                "completed_at": record.get(
                    "completed_at"
                ),
                "status": record["status"],
                "input": record.get(
                    "input",
                    {},
                ),
                "output": record.get(
                    "output",
                    {},
                ),
                "duration_ms": record.get(
                    "duration_ms"
                ),
                "errors": record.get(
                    "errors",
                    [],
                ),
                "logs": record.get(
                    "logs",
                    [],
                ),
                "dependencies": record.get(
                    "dependencies",
                    {},
                ),
            }
        )