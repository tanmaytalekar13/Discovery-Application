from __future__ import annotations

from typing import Any
from uuid import UUID

from app.db.client import ArcadeDBClient
from app.models import Item, TestRun


class ItemRepository:
    ALLOWED_EDGE_TYPES = {
        "USES_TOOL",
        "HAS_SKILL",
        "DISCOVERED_FROM",
        "HAS_TEST_RUN",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, item: Item) -> Item:
        payload = {
            "item_id": str(item.item_id),
            "type": item.type.value,
            "name": item.name,
            "description": item.description,
            "source_type": item.source.type.value,
            "source_id": item.source.id,
            "source_url": (
                str(item.source.url)
                if item.source.url
                else None
            ),
            "version": item.version,
            "status": item.status.value,
            "reliability_score": item.reliability.score,
            "reliability_confidence": (
                item.reliability.confidence
            ),
            "scoring_version": (
                item.reliability.scoring_version
            ),
            "last_evaluated": (
                item.reliability.last_evaluated
            ),
            "first_seen": item.discovery.first_seen,
            "last_seen": item.discovery.last_seen,
            "last_synced": item.discovery.last_synced,
            "tool": (
                item.tool.model_dump(mode="json")
                if item.tool
                else None
            ),
            "agent": (
                item.agent.model_dump(mode="json")
                if item.agent
                else None
            ),
            "artifacts": (
                item.artifacts.model_dump(mode="json")
            ),
            "embedding": item.embedding,
        }

        await self._db.command(
            "sql",
            """
            CREATE DOCUMENT Item CONTENT {
                item_id: :item_id,
                type: :type,
                name: :name,
                description: :description,
                source_type: :source_type,
                source_id: :source_id,
                source_url: :source_url,
                version: :version,
                status: :status,
                reliability_score: :reliability_score,
                reliability_confidence: :reliability_confidence,
                scoring_version: :scoring_version,
                last_evaluated: :last_evaluated,
                first_seen: :first_seen,
                last_seen: :last_seen,
                last_synced: :last_synced,
                tool: :tool,
                agent: :agent,
                artifacts: :artifacts,
                embedding: :embedding
            }
            """,
            payload,
        )

        return item

    async def get(self, item_id: UUID) -> Item | None:
        response = await self._db.command(
            "sql",
            """
            SELECT FROM Item
            WHERE item_id = :item_id
            """,
            {
                "item_id": str(item_id),
            },
        )

        rows = response.get("result", [])

        if not rows:
            return None

        return self._to_item(rows[0])

    async def update(
        self,
        item_id: UUID,
        updates: dict[str, Any],
    ) -> Item | None:
        if not updates:
            return await self.get(item_id)

        allowed_fields = {
            "type",
            "name",
            "description",
            "source_type",
            "source_id",
            "source_url",
            "version",
            "status",
            "reliability_score",
            "reliability_confidence",
            "scoring_version",
            "last_evaluated",
            "first_seen",
            "last_seen",
            "last_synced",
            "tool",
            "agent",
            "artifacts",
            "embedding",
        }

        invalid_fields = set(updates) - allowed_fields

        if invalid_fields:
            raise ValueError(
                "Unsupported Item update fields: "
                + ", ".join(sorted(invalid_fields))
            )

        set_clauses = []

        for field in updates:
            set_clauses.append(
                f"{field} = :{field}"
            )

        params = {
            "item_id": str(item_id),
            **{
                key: (
                    value.value
                    if hasattr(value, "value")
                    else value
                )
                for key, value in updates.items()
            },
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
        response = await self._db.command(
            "sql",
            """
            DELETE FROM Item
            WHERE item_id = :item_id
            """,
            {
                "item_id": str(item_id),
            },
        )

        return response.get("count", 0) > 0

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
        if edge_type not in self.ALLOWED_EDGE_TYPES:
            raise ValueError(
                f"Unsupported edge type: {edge_type}"
            )

        allowed_vertex_types = {
            "Item",
            "Skill",
            "DiscoverySource",
            "TestRun",
        }

        if from_type not in allowed_vertex_types:
            raise ValueError(
                f"Unsupported source type: {from_type}"
            )

        if to_type not in allowed_vertex_types:
            raise ValueError(
                f"Unsupported target type: {to_type}"
            )

        response = await self._db.command(
            "sql",
            f"""
            CREATE EDGE {edge_type}
            FROM (
                SELECT FROM {from_type}
                WHERE {from_field} = :from_id
            )
            TO (
                SELECT FROM {to_type}
                WHERE {to_field} = :to_id
            )
            """,
            {
                "from_id": str(from_id),
                "to_id": str(to_id),
            },
        )

        if "error" in response:
            raise RuntimeError(
                "Failed to create graph edge: "
                f"{response}"
            )

    @staticmethod
    def _to_item(row: dict[str, Any]) -> Item:
        return Item.model_validate(
            {
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
                    "score": row.get(
                        "reliability_score",
                        0.0,
                    ),
                    "confidence": row.get(
                        "reliability_confidence",
                        0.0,
                    ),
                    "scoring_version": row.get(
                        "scoring_version",
                        "v1",
                    ),
                    "last_evaluated": row.get(
                        "last_evaluated"
                    ),
                },
                "discovery": {
                    "first_seen": row["first_seen"],
                    "last_seen": row["last_seen"],
                    "last_synced": row["last_synced"],
                },
                "tool": row.get("tool"),
                "agent": row.get("agent"),
                "artifacts": row.get(
                    "artifacts",
                    {},
                ),
                "embedding": row.get("embedding"),
            }
        )


class TestRunRepository:
    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, test_run: TestRun) -> TestRun:
        payload = {
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
        }

        await self._db.command(
            "sql",
            """
            CREATE DOCUMENT TestRun CONTENT {
                run_id: :run_id,
                item_id: :item_id,
                type: :type,
                started_at: :started_at,
                completed_at: :completed_at,
                status: :status,
                input: :input,
                output: :output,
                duration_ms: :duration_ms,
                errors: :errors,
                logs: :logs,
                dependencies: :dependencies
            }
            """,
            payload,
        )

        return test_run

    async def get(
        self,
        run_id: UUID,
    ) -> TestRun | None:
        response = await self._db.command(
            "sql",
            """
            SELECT FROM TestRun
            WHERE run_id = :run_id
            """,
            {
                "run_id": str(run_id),
            },
        )

        rows = response.get("result", [])

        if not rows:
            return None

        return self._to_test_run(rows[0])

    async def update(
        self,
        run_id: UUID,
        updates: dict[str, Any],
    ) -> TestRun | None:
        if not updates:
            return await self.get(run_id)

        allowed_fields = {
            "item_id",
            "type",
            "started_at",
            "completed_at",
            "status",
            "input",
            "output",
            "duration_ms",
            "errors",
            "logs",
            "dependencies",
        }

        invalid_fields = set(updates) - allowed_fields

        if invalid_fields:
            raise ValueError(
                "Unsupported TestRun update fields: "
                + ", ".join(sorted(invalid_fields))
            )

        set_clauses = []

        for field in updates:
            set_clauses.append(
                f"{field} = :{field}"
            )

        params = {
            "run_id": str(run_id),
            **{
                key: (
                    value.value
                    if hasattr(value, "value")
                    else (
                        value.model_dump()
                        if hasattr(value, "model_dump")
                        else value
                    )
                )
                for key, value in updates.items()
            },
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
        response = await self._db.command(
            "sql",
            """
            DELETE FROM TestRun
            WHERE run_id = :run_id
            """,
            {
                "run_id": str(run_id),
            },
        )

        return response.get("count", 0) > 0

    @staticmethod
    def _to_test_run(
        row: dict[str, Any],
    ) -> TestRun:
        return TestRun.model_validate(
            {
                "run_id": row["run_id"],
                "item_id": row["item_id"],
                "type": row["type"],
                "started_at": row["started_at"],
                "completed_at": row.get(
                    "completed_at"
                ),
                "status": row.get(
                    "status",
                    "running",
                ),
                "input": row.get(
                    "input",
                    {},
                ),
                "output": row.get(
                    "output",
                    {},
                ),
                "duration_ms": row.get(
                    "duration_ms"
                ),
                "errors": row.get(
                    "errors",
                    [],
                ),
                "logs": row.get(
                    "logs",
                    [],
                ),
                "dependencies": row.get(
                    "dependencies",
                    {},
                ),
            }
        )