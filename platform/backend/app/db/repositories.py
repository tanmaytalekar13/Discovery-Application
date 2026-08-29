from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from app.db.client import ArcadeDBClient
from app.models import Item, TestRun


def _prepare_value(value: Any) -> Any:
    """
    Convert Python values into JSON-serializable values for ArcadeDB.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


class ItemRepository:
    """Repository for managing Item vertices and graph edges."""

    ALLOWED_EDGE_TYPES = {
        "USES_TOOL",
        "HAS_TEST_RUN",
    }

    ALLOWED_VERTEX_TYPES = {
        "Item",
        "TestRun",
    }

    ALLOWED_UPDATE_FIELDS = {
        "type", "name", "description", "source_type", "source_id",
        "source_url", "version", "status", "reliability_score",
        "reliability_confidence", "scoring_version", "last_evaluated",
        "first_seen", "last_seen", "last_synced", "tool", "agent",
        "artifacts", "embedding",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, item: Item) -> Item:
        """Create a new Item vertex."""
        payload = {
            "item_id": str(item.item_id),
            "type": item.type.value,
            "name": item.name,
            "description": item.description,
            
            "source_type": item.source.type.value,
            "source_id": item.source.id,
            "source_url": str(item.source.url) if item.source.url else None,
            
            "version": item.version,
            "status": item.status.value,
            
            "reliability_score": item.reliability.score,
            "reliability_confidence": item.reliability.confidence,
            "scoring_version": item.reliability.scoring_version,
            "last_evaluated": _prepare_value(item.reliability.last_evaluated),
            
            "first_seen": _prepare_value(item.discovery.first_seen),
            "last_seen": _prepare_value(item.discovery.last_seen),
            "last_synced": _prepare_value(item.discovery.last_synced),
            
            # IMPORTANT:
            # These are embedded maps, not ArcadeDB schema types.
            # Do NOT add @type here.
            "tool": item.tool.model_dump(mode="json") if item.tool else None,
            "agent": item.agent.model_dump(mode="json") if item.agent else None,
            "artifacts": item.artifacts.model_dump(mode="json"),
            "embedding": item.embedding,
        }

        await self._db.command(
            "sql",
            "CREATE VERTEX Item CONTENT :payload",
            {"payload": payload},
        )

        return item

    async def get(self, item_id: UUID) -> Item | None:
        """Retrieve an Item vertex by item_id."""
        response = await self._db.command(
            "sql",
            """
            SELECT FROM Item
            WHERE item_id = :item_id
            LIMIT 1
            """,
            {"item_id": str(item_id)},
        )

        rows = response.get("result", [])
        return self._to_item(rows[0]) if rows else None

    async def update(self, item_id: UUID, updates: dict[str, Any]) -> Item | None:
        """Update specific fields of an existing Item vertex."""
        if not updates:
            return await self.get(item_id)

        invalid_fields = set(updates) - self.ALLOWED_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(
                f"Unsupported Item update fields: {', '.join(sorted(invalid_fields))}"
            )

        set_clauses = [f"{field} = :{field}" for field in updates]
        params = {
            "item_id": str(item_id),
            **{key: _prepare_value(value) for key, value in updates.items()}
        }

        await self._db.command(
            "sql",
            f"""
            UPDATE Item
            SET {", ".join(set_clauses)}
            WHERE item_id = :item_id
            """,
            params,
        )

        return await self.get(item_id)

    async def delete(self, item_id: UUID) -> bool:
        """Delete an Item vertex by item_id."""
        response = await self._db.command(
            "sql",
            """
            DELETE FROM Item
            WHERE item_id = :item_id
            """,
            {"item_id": str(item_id)},
        )

        return response.get("count", 0) > 0

    async def _get_record_rid(self, record_type: str, field: str, value: UUID | str) -> str:
        """Resolve a logical identifier to an ArcadeDB @rid."""
        if record_type not in self.ALLOWED_VERTEX_TYPES:
            raise ValueError(f"Unsupported vertex type: {record_type}")

        response = await self._db.command(
            "sql",
            f"""
            SELECT @rid
            FROM {record_type}
            WHERE {field} = :value
            LIMIT 1
            """,
            {"value": str(value)},
        )

        rows = response.get("result", [])
        if not rows:
            raise ValueError(f"{record_type} with {field}={value} was not found")

        rid = rows[0].get("@rid")
        if not rid:
            raise ValueError(f"ArcadeDB did not return @rid for {record_type}.{field}={value}")

        return str(rid)

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
        Create an edge between two existing ArcadeDB vertices.

        Source and target are first resolved to their internal
        ArcadeDB @rid values. CREATE EDGE then uses those RIDs.
        """
        if edge_type not in self.ALLOWED_EDGE_TYPES:
            raise ValueError(f"Unsupported edge type: {edge_type}")

        from_rid = await self._get_record_rid(
            record_type=from_type, field=from_field, value=from_id
        )
        to_rid = await self._get_record_rid(
            record_type=to_type, field=to_field, value=to_id
        )

        await self._db.command(
            "sql",
            f"""
            CREATE EDGE {edge_type}
            FROM {from_rid}
            TO {to_rid}
            IF NOT EXISTS
            """,
        )

    @staticmethod
    def _to_item(row: dict[str, Any]) -> Item:
        """Map an ArcadeDB Item record to the Pydantic model."""
        return Item.model_validate({
            "item_id": row["item_id"],
            "type": row["type"],
            "name": row["name"],
            "description": row["description"],
            "source": {
                "type": row["source_type"],
                "id": row["source_id"],
                "url": row.get("source_url"),
            },
            "version": row.get("version"),
            "status": row.get("status", "active"),
            "reliability": {
                "score": row.get("reliability_score", 0.0),
                "confidence": row.get("reliability_confidence", 0.0),
                "scoring_version": row.get("scoring_version", "v1"),
                "last_evaluated": row.get("last_evaluated"),
            },
            "discovery": {
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "last_synced": row["last_synced"],
            },
            "tool": row.get("tool"),
            "agent": row.get("agent"),
            "artifacts": row.get("artifacts", {}),
            "embedding": row.get("embedding"),
        })


class TestRunRepository:
    """Repository for managing TestRun vertices."""

    ALLOWED_UPDATE_FIELDS = {
        "item_id", "type", "started_at", "completed_at", "status",
        "input", "output", "duration_ms", "errors", "logs", "dependencies",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, test_run: TestRun) -> TestRun:
        """Create a new TestRun vertex."""
        payload = {
            "run_id": str(test_run.run_id),
            "item_id": str(test_run.item_id),
            "type": test_run.type,
            
            "started_at": _prepare_value(test_run.started_at),
            "completed_at": _prepare_value(test_run.completed_at),
            
            "status": (
                test_run.status.value
                if hasattr(test_run.status, "value")
                else test_run.status
            ),
            
            "input": test_run.input,
            "output": test_run.output,
            "duration_ms": test_run.duration_ms,
            "errors": test_run.errors,
            "logs": test_run.logs,
            "dependencies": (
                test_run.dependencies.model_dump(mode="json")
                if test_run.dependencies else {}
            ),
        }

        await self._db.command(
            "sql",
            "CREATE VERTEX TestRun CONTENT :payload",
            {"payload": payload},
        )

        return test_run

    async def get(self, run_id: UUID) -> TestRun | None:
        """Retrieve a TestRun vertex by run_id."""
        response = await self._db.command(
            "sql",
            """
            SELECT FROM TestRun
            WHERE run_id = :run_id
            LIMIT 1
            """,
            {"run_id": str(run_id)},
        )

        rows = response.get("result", [])
        return self._to_test_run(rows[0]) if rows else None

    async def update(self, run_id: UUID, updates: dict[str, Any]) -> TestRun | None:
        """Update specific fields of a TestRun vertex."""
        if not updates:
            return await self.get(run_id)

        invalid_fields = set(updates) - self.ALLOWED_UPDATE_FIELDS
        if invalid_fields:
            raise ValueError(
                f"Unsupported TestRun update fields: {', '.join(sorted(invalid_fields))}"
            )

        set_clauses = [f"{field} = :{field}" for field in updates]
        params = {
            "run_id": str(run_id),
            **{key: _prepare_value(value) for key, value in updates.items()}
        }

        await self._db.command(
            "sql",
            f"""
            UPDATE TestRun
            SET {", ".join(set_clauses)}
            WHERE run_id = :run_id
            """,
            params,
        )

        return await self.get(run_id)

    async def delete(self, run_id: UUID) -> bool:
        """Delete a TestRun vertex by run_id."""
        response = await self._db.command(
            "sql",
            """
            DELETE FROM TestRun
            WHERE run_id = :run_id
            """,
            {"run_id": str(run_id)},
        )

        return response.get("count", 0) > 0

    @staticmethod
    def _to_test_run(row: dict[str, Any]) -> TestRun:
        """Map an ArcadeDB TestRun record to the Pydantic model."""
        return TestRun.model_validate({
            "run_id": row["run_id"],
            "item_id": row["item_id"],
            "type": row["type"],
            "started_at": row["started_at"],
            "completed_at": row.get("completed_at"),
            "status": row.get("status", "running"),
            "input": row.get("input", {}),
            "output": row.get("output", {}),
            "duration_ms": row.get("duration_ms"),
            "errors": row.get("errors", []),
            "logs": row.get("logs", []),
            "dependencies": row.get("dependencies", {}),
        })