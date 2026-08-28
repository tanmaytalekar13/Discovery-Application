from typing import Any
from uuid import UUID

from app.db.client import ArcadeDBClient
from app.models import Item, TestRun


class ItemRepository:
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
            {"item_id": str(item_id)},
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
            {"item_id": str(item_id)},
        )

        return result.get("count", 0) > 0

    async def create_edge(
        self,
        edge_type: str,
        from_item_id: UUID,
        to_item_id: UUID,
    ) -> None:
        await self._db.command(
            "sql",
            f"""
            CREATE EDGE {edge_type}
            FROM (
                SELECT FROM Item
                WHERE item_id = :from_id
            )
            TO (
                SELECT FROM Item
                WHERE item_id = :to_id
            )
            """,
            {
                "from_id": str(from_item_id),
                "to_id": str(to_item_id),
            },
        )

    @staticmethod
    def _record_to_item(record: dict[str, Any]) -> Item:
        """
        Convert an ArcadeDB Item document into the Pydantic model.
        """

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
                "score": record.get("reliability_score", 0.0),
                "confidence": record.get(
                    "reliability_confidence",
                    0.0,
                ),
                "scoring_version": record.get(
                    "scoring_version",
                    "v1",
                ),
                "last_evaluated": record.get(
                    "last_evaluated"
                ),
            },
            discovery={
                "first_seen": record["first_seen"],
                "last_seen": record["last_seen"],
                "last_synced": record["last_synced"],
            },
            embedding=record.get("embedding"),
        )


class TestRunRepository:
    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, test_run: TestRun) -> TestRun:
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

    async def get(self, run_id: UUID) -> TestRun | None:
        result = await self._db.command(
            "sql",
            """
            SELECT FROM TestRun
            WHERE run_id = :run_id
            LIMIT 1
            """,
            {"run_id": str(run_id)},
        )

        records = result.get("result", [])

        if not records:
            return None

        return TestRun.model_validate(records[0])

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

    async def delete(self, run_id: UUID) -> bool:
        result = await self._db.command(
            "sql",
            """
            DELETE FROM TestRun
            WHERE run_id = :run_id
            """,
            {"run_id": str(run_id)},
        )

        return result.get("count", 0) > 0