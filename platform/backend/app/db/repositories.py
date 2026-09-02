from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from app.db.client import ArcadeDBClient
from app.models import DiscoveryEvidence, DiscoverySource, Item, TestRun


def _make_serializable(obj: Any, max_depth: int = 10, current_depth: int = 0) -> Any:
    """
    Make any Python object JSON serializable by recursively converting all nested objects.
    """
    if current_depth > max_depth:
        return str(obj)
    
    # Handle None and primitive types
    if obj is None:
        return None
    
    if isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Handle datetime and UUID
    if isinstance(obj, (datetime, UUID)):
        return str(obj)
    
    # Handle dicts
    if isinstance(obj, dict):
        return {
            str(k): _make_serializable(v, max_depth, current_depth + 1)
            for k, v in obj.items()
        }
    
    # Handle lists and tuples
    if isinstance(obj, (list, tuple)):
        return [_make_serializable(item, max_depth, current_depth + 1) for item in obj]
    
    # Handle Pydantic models
    if hasattr(obj, "model_dump"):
        try:
            return _make_serializable(obj.model_dump(mode="json"), max_depth, current_depth + 1)
        except Exception:
            pass
    
    # Handle enums
    if hasattr(obj, "value"):
        return _make_serializable(obj.value, max_depth, current_depth + 1)
    
    # Handle objects with __dict__
    if hasattr(obj, "__dict__"):
        try:
            return _make_serializable(vars(obj), max_depth, current_depth + 1)
        except Exception:
            pass
    
    # Fallback: convert to string
    return str(obj)



def _prepare_value(value: Any) -> Any:
    """
    Convert Python values into JSON-serializable values for ArcadeDB.
    """
    return _make_serializable(value)


class ItemRepository:
    """Repository for managing Item vertices and graph edges."""

    ALLOWED_EDGE_TYPES = {
        "USES_TOOL",
        "HAS_TEST_RUN",
        "HAS_DISCOVERY_SOURCE",
        "HAS_DISCOVERY_EVIDENCE",
        "HAS_RELIABILITY_EVALUATION",
    }

    ALLOWED_VERTEX_TYPES = {
        "Item",
        "TestRun",
        "DiscoverySource",
        "DiscoveryEvidence",
        "ReliabilityEvaluation",
        "DiscoveryRejection",
    }

    ALLOWED_UPDATE_FIELDS = {
        "canonical_id",
        "type",
        "name",
        "description",
        "source_type",
        "source_id",
        "source_url",
        "source_provider",
        "provenance",
        "evidence_summary",
        "version",
        "status",
        "reliability_score",
        "reliability_confidence",
        "security_validation",
        "reliability_signals",
        "reliability_reasons",
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

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def create(self, item: Item) -> Item:
        """Create a new Item vertex."""
        payload = {
            "item_id": str(item.item_id),
            "canonical_id": item.canonical_id,
            "type": item.type.value,
            "name": item.name,
            "description": item.description,
            "source_type": item.source.type.value,
            "source_id": item.source.id,
            "source_url": str(item.source.url) if item.source.url else None,
            "source_provider": item.source.provider,
            "provenance": [
                source.model_dump(mode="json") for source in item.provenance
            ],
            "evidence_summary": [
                evidence.model_dump(mode="json") for evidence in item.evidence
            ],
            "version": item.version,
            "status": item.status.value,
            "reliability_score": item.reliability.score,
            "reliability_confidence": item.reliability.confidence,
            "security_validation": item.reliability.security_validation,
            "reliability_signals": item.reliability.signals,
            "reliability_reasons": item.reliability.reasons,
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
            **{key: _prepare_value(value) for key, value in updates.items()},
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

    async def search_rankable(
        self,
        *,
        item_type: str = "all",
        limit: int = 100,
        keywords: list[str] | tuple[str, ...] = (),
    ) -> list[Item]:
        """Fetch active catalog items for Phase 11 semantic/structured ranking."""
        if item_type not in {"all", "tool", "agent"}:
            raise ValueError("item_type must be 'all', 'tool', or 'agent'")

        where_clauses = ["status = :status"]
        params: dict[str, Any] = {
            "status": "active",
            "limit": limit,
        }

        if item_type != "all":
            where_clauses.append("type = :type")
            params["type"] = item_type

        cleaned_keywords = [
            keyword.strip().lower() for keyword in keywords if keyword.strip()
        ]
        if cleaned_keywords:
            keyword_clauses: list[str] = []
            for index, keyword in enumerate(cleaned_keywords[:10]):
                key = f"keyword_{index}"
                params[key] = f"%{keyword}%"
                keyword_clauses.extend(
                    [
                        f"name.toLowerCase() LIKE :{key}",
                        f"description.toLowerCase() LIKE :{key}",
                        f"source_id.toLowerCase() LIKE :{key}",
                    ]
                )
            where_clauses.append(f"({' OR '.join(keyword_clauses)})")

        response = await self._db.command(
            "sql",
            f"""
            SELECT FROM Item
            WHERE {' AND '.join(where_clauses)}
            LIMIT :limit
            """,
            params,
        )

        return [self._to_item(row) for row in response.get("result", [])]

    async def update_embedding(
        self, item_id: UUID, embedding: list[float]
    ) -> Item | None:
        """Persist a locally generated embedding for an Item."""
        return await self.update(item_id, {"embedding": embedding})

    async def upsert_catalog_item(self, item: Item, evaluation: Any) -> Item:
        """Persist an approved canonical Item and all Phase 10 provenance/evidence."""
        existing = await self.get(item.item_id)
        payload = {
            "item_id": str(item.item_id),
            "canonical_id": item.canonical_id,
            "type": item.type.value,
            "name": item.name,
            "description": item.description,
            "source_type": item.source.type.value,
            "source_id": item.source.id,
            "source_url": str(item.source.url) if item.source.url else None,
            "source_provider": item.source.provider,
            "provenance": [s.model_dump(mode="json") for s in item.provenance],
            "evidence_summary": [e.model_dump(mode="json") for e in item.evidence],
            "version": item.version,
            "status": item.status.value,
            "reliability_score": item.reliability.score,
            "reliability_confidence": item.reliability.confidence,
            "scoring_version": item.reliability.scoring_version,
            "last_evaluated": _prepare_value(item.reliability.last_evaluated),
            "security_validation": item.reliability.security_validation,
            "reliability_signals": item.reliability.signals,
            "reliability_reasons": item.reliability.reasons,
            "first_seen": _prepare_value(item.discovery.first_seen),
            "last_seen": _prepare_value(item.discovery.last_seen),
            "last_synced": _prepare_value(item.discovery.last_synced),
            "tool": item.tool.model_dump(mode="json") if item.tool else None,
            "agent": item.agent.model_dump(mode="json") if item.agent else None,
            "artifacts": item.artifacts.model_dump(mode="json"),
            "embedding": item.embedding,
        }
        if existing is None:
            await self._db.command(
                "sql", "CREATE VERTEX Item CONTENT :payload", {"payload": payload}
            )
        else:
            updates = {
                key: value
                for key, value in payload.items()
                if key in self.ALLOWED_UPDATE_FIELDS
            }
            await self.update(item.item_id, updates)
        item_rid = await self._get_record_rid("Item", "item_id", item.item_id)
        for source in item.provenance:
            source_key = (
                f"{source.type.value}|{source.id}|{source.url}|{source.provider}"
            )
            await self._upsert_source(
                item_rid, source_key, source, item.discovery.last_seen
            )
        for evidence in item.evidence:
            await self._upsert_evidence(item_rid, item.item_id, evidence)
        await self._upsert_reliability_evaluation(item_rid, item.item_id, evaluation)
        return item

    async def _upsert_source(
        self,
        item_rid: str,
        source_key: str,
        source: DiscoverySource,
        observed_at: datetime,
    ) -> None:
        found = await self._db.command(
            "sql",
            "SELECT FROM DiscoverySource WHERE source_key = :source_key LIMIT 1",
            {"source_key": source_key},
        )
        rows = found.get("result", [])
        if rows:
            source_rid = str(rows[0]["@rid"])
            await self._db.command(
                "sql",
                "UPDATE DiscoverySource SET last_seen = :last_seen WHERE source_key = :source_key",
                {
                    "source_key": source_key,
                    "last_seen": _prepare_value(observed_at),
                },
            )
        else:
            payload = {
                "source_key": source_key,
                "source_type": source.type.value,
                "source_id": source.id,
                "source_url": str(source.url) if source.url else None,
                "provider": source.provider,
                "first_seen": _prepare_value(observed_at),
                "last_seen": _prepare_value(observed_at),
            }
            await self._db.command(
                "sql",
                "CREATE VERTEX DiscoverySource CONTENT :payload",
                {"payload": payload},
            )
            source_rid = await self._get_record_rid(
                "DiscoverySource", "source_key", source_key
            )
        await self._db.command(
            "sql",
            f"CREATE EDGE HAS_DISCOVERY_SOURCE FROM {item_rid} TO {source_rid} IF NOT EXISTS",
        )

    async def _upsert_evidence(
        self, item_rid: str, item_id: UUID, evidence: DiscoveryEvidence
    ) -> None:
        evidence_id = str(evidence.evidence_id)
        found = await self._db.command(
            "sql",
            "SELECT FROM DiscoveryEvidence WHERE evidence_id = :evidence_id LIMIT 1",
            {"evidence_id": evidence_id},
        )
        rows = found.get("result", [])
        if rows:
            evidence_rid = str(rows[0]["@rid"])
        else:
            payload = {
                "evidence_id": evidence_id,
                "item_id": str(item_id),
                "kind": evidence.kind,
                "statement": evidence.statement,
                "source_type": evidence.source.type.value,
                "source_id": evidence.source.id,
                "source_url": str(evidence.source.url) if evidence.source.url else None,
                "provider": evidence.source.provider,
                "observed_at": _prepare_value(evidence.observed_at),
                "details": evidence.details,
            }
            await self._db.command(
                "sql",
                "CREATE VERTEX DiscoveryEvidence CONTENT :payload",
                {"payload": payload},
            )
            evidence_rid = await self._get_record_rid(
                "DiscoveryEvidence", "evidence_id", evidence_id
            )
        await self._db.command(
            "sql",
            f"CREATE EDGE HAS_DISCOVERY_EVIDENCE FROM {item_rid} TO {evidence_rid} IF NOT EXISTS",
        )

    async def _upsert_reliability_evaluation(
        self, item_rid: str, item_id: UUID, evaluation: Any
    ) -> None:
        evaluation_id = str(uuid4())
        payload = {
            "evaluation_id": evaluation_id,
            "item_id": str(item_id),
            "score": evaluation.score,
            "confidence": evaluation.confidence,
            "scoring_version": "v1",
            "approved": evaluation.approved,
            "signals": evaluation.signals,
            "reasons": evaluation.reasons,
            "security_validation": evaluation.security_validation,
            "evaluated_at": datetime.now().isoformat(),
        }
        await self._db.command(
            "sql",
            "CREATE VERTEX ReliabilityEvaluation CONTENT :payload",
            {"payload": payload},
        )
        evaluation_rid = await self._get_record_rid(
            "ReliabilityEvaluation", "evaluation_id", evaluation_id
        )
        await self._db.command(
            "sql",
            f"CREATE EDGE HAS_RELIABILITY_EVALUATION FROM {item_rid} TO {evaluation_rid} IF NOT EXISTS",
        )

    async def persist_rejection(self, rejection: Any) -> None:
        """Persist a DiscoveryRejection vertex for an item/candidate that failed discovery."""
        payload = {
            "rejection_id": str(uuid4()),
            "candidate_id": str(rejection.candidate_id),
            "item_id": str(rejection.item_id) if rejection.item_id else None,
            "protocol": rejection.protocol,
            "source_type": rejection.source.type.value,
            "source_id": rejection.source.id,
            "source_url": str(rejection.source.url) if rejection.source.url else None,
            "provider": rejection.source.provider,
            "reason": rejection.reason,
            "evidence": rejection.evidence if isinstance(rejection.evidence, list) else [],
            "details": _make_serializable(rejection.details) if rejection.details else {},
            "observed_at": _prepare_value(rejection.observed_at),
        }
        # Ensure the entire payload is JSON serializable
        payload = _make_serializable(payload)

        await self._db.command(
            "sql",
            "CREATE VERTEX DiscoveryRejection CONTENT :payload",
            {"payload": payload},
        )

    async def _get_record_rid(
        self, record_type: str, field: str, value: UUID | str
    ) -> str:
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
            raise ValueError(
                f"ArcadeDB did not return @rid for {record_type}.{field}={value}"
            )

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
        observed_at = _coalesce_datetime(
            row.get("last_seen"),
            row.get("first_seen"),
            row.get("last_synced"),
            row.get("last_evaluated"),
        )

        # Fix corrupted artifacts.source_url if it's a dict
        artifacts = row.get("artifacts", {})
        if isinstance(artifacts, dict) and "source_url" in artifacts:
            source_url = artifacts["source_url"]
            if isinstance(source_url, dict) and "_url" in source_url:
                # Corrupted HttpUrl object - extract the actual URL string
                artifacts["source_url"] = source_url["_url"]

        return Item.model_validate(
            {
                "item_id": row["item_id"],
                "canonical_id": row.get("canonical_id"),
                "type": row["type"],
                "name": row["name"],
                "description": row["description"],
                "source": {
                    "type": row["source_type"],
                    "id": row["source_id"],
                    "url": row.get("source_url"),
                    "provider": row.get("source_provider"),
                },
                "version": row.get("version"),
                "status": row.get("status", "active"),
                "reliability": {
                    "score": row.get("reliability_score") or 0.0,
                    "confidence": row.get("reliability_confidence") or 0.0,
                    "scoring_version": row.get("scoring_version") or "v1",
                    "last_evaluated": row.get("last_evaluated"),
                    "security_validation": (
                        row.get("security_validation")
                        if isinstance(row.get("security_validation"), (int, float))
                        else 0.0
                    ),
                    "signals": (
                        row.get("reliability_signals")
                        if isinstance(row.get("reliability_signals"), dict)
                        else {}
                    ),
                    "reasons": row.get("reliability_reasons") or [],
                },
                "discovery": {
                    "first_seen": row.get("first_seen") or observed_at,
                    "last_seen": row.get("last_seen") or observed_at,
                    "last_synced": row.get("last_synced") or observed_at,
                },
                "tool": row.get("tool"),
                "agent": row.get("agent"),
                "artifacts": artifacts,
                "provenance": row.get("provenance", []),
                "evidence": row.get("evidence_summary", []),
                "embedding": row.get("embedding"),
            }
        )


def _coalesce_datetime(*values: Any) -> Any:
    for value in values:
        if value:
            return value
    return datetime.now(timezone.utc)


class TestRunRepository:
    """Repository for managing TestRun vertices."""

    ALLOWED_UPDATE_FIELDS = {
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
                if test_run.dependencies
                else {}
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
            **{key: _prepare_value(value) for key, value in updates.items()},
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
        return TestRun.model_validate(
            {
                "run_id": row["run_id"],
                "item_id": row["item_id"],
                "canonical_id": row.get("canonical_id"),
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
            }
        )
