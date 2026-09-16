"""ArcadeDB repositories for the verification subsystem (spec v2 Section 2).

Deliberately separate from the generic `Item` repositories: these cover
the `McpServer` scored registry (the only thing the search API reads),
the per-provider credential store, cold-miss query bookkeeping, and the
observability decision log.

All writes go through allow-listed field sets (matching the existing
repository convention in `app/db/repositories.py`) so a malformed
pipeline run cannot inject arbitrary properties.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.db.client import ArcadeDBClient
from app.verification.models import (
    ColdMissQueryRecord,
    Confidence,
    McpServerRecord,
    ProviderCredentialRecord,
    ServerStatus,
    Transport,
    VerificationDecisionRecord,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _query_key_for(term: str) -> str:
    """Stable dedupe key for a cold-miss search term."""
    return hashlib.sha256(term.strip().lower().encode()).hexdigest()[:32]


class VerificationRepository:
    """Read/write access to McpServer registry records."""

    ALLOWED_UPDATE_FIELDS = {
        "name",
        "source_url",
        "transport",
        "install_cmd",
        "endpoint_url",
        "declared_tools",
        "description",
        "registry_name",
        "repository_url",
        "provider_keys",
        "auth_required",
        "auth_type",
        "oauth_flow",
        "oauth_provider",
        "status",
        "quality_score",
        "confidence",
        "attempts_completed",
        "attempts_planned",
        "invocation_verified",
        "latency_p50_ms",
        "latency_category",
        "last_verified_at",
        "ttl_expires_at",
        "verification_details",
        "rejection_stage",
        "rejection_check",
        "first_seen_at",
        "last_attempt_at",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    # ---- create / read / update -----------------------------------------

    async def create(self, record: McpServerRecord) -> McpServerRecord:
        existing = await self.get_by_source_url(record.source_url)
        if existing is not None:
            return existing
        payload = self._to_payload(record)
        await self._db.command(
            "sql", "CREATE VERTEX McpServer CONTENT :payload", {"payload": payload}
        )
        return record

    async def get(self, server_id: UUID) -> McpServerRecord | None:
        rows = await self._fetch("server_id = :value", {"value": str(server_id)})
        return self._to_record(rows[0]) if rows else None

    async def get_by_source_url(self, source_url: str) -> McpServerRecord | None:
        rows = await self._fetch("source_url = :value", {"value": source_url})
        return self._to_record(rows[0]) if rows else None

    async def get_by_registry_name(self, registry_name: str) -> McpServerRecord | None:
        rows = await self._fetch(
            "registry_name = :value", {"value": registry_name}
        )
        return self._to_record(rows[0]) if rows else None

    async def update(
        self, server_id: UUID, updates: dict[str, Any]
    ) -> McpServerRecord | None:
        if not updates:
            return await self.get(server_id)
        invalid = set(updates) - self.ALLOWED_UPDATE_FIELDS
        if invalid:
            raise ValueError(
                f"Unsupported McpServer update fields: {', '.join(sorted(invalid))}"
            )
        set_clauses = [f"{field} = :{field}" for field in updates]
        params: dict[str, Any] = {
            "server_id": str(server_id),
            **{key: self._prepare(value) for key, value in updates.items()},
        }
        await self._db.command(
            "sql",
            f"UPDATE McpServer SET {', '.join(set_clauses)} WHERE server_id = :server_id",
            params,
        )
        return await self.get(server_id)

    # ---- search (the ONLY query shape the API uses) -----------------------

    async def search(
        self,
        *,
        keywords: list[str] | tuple[str, ...] = (),
        statuses: list[str] | tuple[str, ...] = (ServerStatus.VERIFIED.value,),
        min_score: int = 0,
        transport: str | None = None,
        limit: int = 20,
    ) -> list[McpServerRecord]:
        """Pure DB read for the search API (spec v2 Section 9 golden rule).

        No verification, no network, no sandbox - a SELECT with filters.
        Every search keyword must appear in at least one indexed identity
        field (name/description/source_url), mirroring the Item search
        behavior; protocol boilerplate words are ignored.
        """
        generic_terms = {"mcp", "model", "context", "protocol", "server",
                         "servers", "tool", "tools", "registry"}
        cleaned = [
            k.strip().lower()
            for k in keywords
            if k.strip() and k.strip().lower() not in generic_terms
        ]

        where_clauses: list[str] = []
        params: dict[str, Any] = {"min_score": int(min_score), "limit": int(limit)}

        if statuses:
            status_list = list(statuses)
            where_clauses.append(
                "status IN [" + ", ".join(f":status_{i}" for i in range(len(status_list))) + "]"
            )
            for i, status in enumerate(status_list):
                params[f"status_{i}"] = status

        if min_score > 0:
            where_clauses.append("quality_score >= :min_score")

        if transport:
            where_clauses.append("transport = :transport")
            params["transport"] = transport

        for i, keyword in enumerate(cleaned[:10]):
            key = f"keyword_{i}"
            params[key] = f"%{keyword}%"
            where_clauses.append(
                "("
                + " OR ".join(
                    [
                        f"name.toLowerCase() LIKE :{key}",
                        f"description.toLowerCase() LIKE :{key}",
                        f"source_url.toLowerCase() LIKE :{key}",
                    ]
                )
                + ")"
            )

        where_sql = f" WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        response = await self._db.command(
            "sql",
            f"""
            SELECT FROM McpServer{where_sql}
            ORDER BY quality_score DESC
            LIMIT :limit
            """,
            params,
        )
        return [self._to_record(row) for row in response.get("result", [])]

    # ---- queue helpers -----------------------------------------------------

    async def list_status(self, *statuses: str, limit: int = 50) -> list[McpServerRecord]:
        if not statuses:
            return []
        status_list = list(statuses)
        where = "status IN [" + ", ".join(
            f":status_{i}" for i in range(len(status_list))
        ) + "]"
        params: dict[str, Any] = {
            f"status_{i}": status for i, status in enumerate(status_list)
        }
        rows = await self._fetch(where, params, limit=limit)
        return [self._to_record(row) for row in rows]

    async def count_by_status(self, status: str) -> int:
        response = await self._db.command(
            "sql",
            "SELECT count(*) AS total FROM McpServer WHERE status = :status",
            {"status": status},
        )
        rows = response.get("result", [])
        if not rows:
            return 0
        total = rows[0].get("total", 0)
        try:
            return int(total)
        except (TypeError, ValueError):
            return 0

    async def list_expired_ttl(self, now_iso: str | None = None, limit: int = 50) -> list[McpServerRecord]:
        """Verified records whose TTL has elapsed (Section 11)."""
        response = await self._db.command(
            "sql",
            """
            SELECT FROM McpServer
            WHERE status = :status AND ttl_expires_at IS NOT NULL AND ttl_expires_at < :now
            LIMIT :limit
            """,
            {
                "now": now_iso or _now_iso(),
                "limit": limit,
                "status": ServerStatus.VERIFIED.value,
            },
        )
        return [self._to_record(row) for row in response.get("result", [])]

    async def list_by_provider(self, provider: str) -> list[McpServerRecord]:
        rows = await self._fetch(
            "oauth_provider = :value", {"value": provider}, limit=200
        )
        return [self._to_record(row) for row in rows]

    # ---- helpers ------------------------------------------------------------

    async def _fetch(
        self, where: str, params: dict[str, Any], limit: int = 1
    ) -> list[dict[str, Any]]:
        response = await self._db.command(
            "sql",
            f"SELECT FROM McpServer WHERE {where} LIMIT :limit",
            {**params, "limit": limit},
        )
        return list(response.get("result", []))

    def _to_record(self, row: dict[str, Any]) -> McpServerRecord:
        details = row.get("verification_details")
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                details = {}
        return McpServerRecord(
            server_id=UUID(str(row["server_id"])),
            name=str(row.get("name") or ""),
            source_url=str(row.get("source_url") or ""),
            transport=Transport(str(row.get("transport") or Transport.REMOTE.value)),
            install_cmd=row.get("install_cmd"),
            endpoint_url=row.get("endpoint_url"),
            declared_tools=(
                row.get("declared_tools") if isinstance(row.get("declared_tools"), list) else []
            ),
            description=str(row.get("description") or ""),
            registry_name=row.get("registry_name"),
            repository_url=row.get("repository_url"),
            provider_keys=(
                row.get("provider_keys") if isinstance(row.get("provider_keys"), list) else []
            ),
            auth_required=bool(row.get("auth_required", False)),
            auth_type=row.get("auth_type") or "none",
            oauth_flow=row.get("oauth_flow"),
            oauth_provider=row.get("oauth_provider"),
            status=ServerStatus(str(row.get("status") or ServerStatus.PENDING.value)),
            quality_score=int(row.get("quality_score") or 0),
            confidence=Confidence(str(row.get("confidence") or Confidence.LOW.value)),
            attempts_completed=int(row.get("attempts_completed") or 0),
            attempts_planned=int(row.get("attempts_planned") or 1),
            invocation_verified=bool(row.get("invocation_verified", False)),
            latency_p50_ms=row.get("latency_p50_ms"),
            latency_category=row.get("latency_category"),
            last_verified_at=row.get("last_verified_at"),
            ttl_expires_at=row.get("ttl_expires_at"),
            verification_details=details if isinstance(details, dict) else {},
            rejection_stage=row.get("rejection_stage"),
            rejection_check=row.get("rejection_check"),
            first_seen_at=row.get("first_seen_at"),
            last_attempt_at=row.get("last_attempt_at"),
        )

    def _to_payload(self, record: McpServerRecord) -> dict[str, Any]:
        return {
            "server_id": str(record.server_id),
            "name": record.name,
            "source_url": record.source_url,
            "transport": record.transport.value,
            "install_cmd": record.install_cmd,
            "endpoint_url": record.endpoint_url,
            "declared_tools": self._prepare(record.declared_tools),
            "description": record.description,
            "registry_name": record.registry_name,
            "repository_url": record.repository_url,
            "provider_keys": record.provider_keys,
            "auth_required": record.auth_required,
            "auth_type": record.auth_type.value
            if hasattr(record.auth_type, "value")
            else record.auth_type,
            "oauth_flow": record.oauth_flow.value if record.oauth_flow else None,
            "oauth_provider": record.oauth_provider,
            "status": record.status.value,
            "quality_score": record.quality_score,
            "confidence": record.confidence.value,
            "attempts_completed": record.attempts_completed,
            "attempts_planned": record.attempts_planned,
            "invocation_verified": record.invocation_verified,
            "latency_p50_ms": record.latency_p50_ms,
            "latency_category": record.latency_category.value if record.latency_category else None,
            "last_verified_at": self._prepare(record.last_verified_at),
            "ttl_expires_at": self._prepare(record.ttl_expires_at),
            "verification_details": self._prepare(record.verification_details),
            "rejection_stage": record.rejection_stage,
            "rejection_check": record.rejection_check,
            "first_seen_at": self._prepare(record.first_seen_at or datetime.now(timezone.utc)),
            "last_attempt_at": self._prepare(record.last_attempt_at),
        }

    @staticmethod
    def _prepare(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return value


class ProviderCredentialRepository:
    """Per-provider OAuth credential store (spec v2 Section 2.2 / rule 7)."""

    ALLOWED_UPDATE_FIELDS = {
        "client_id",
        "redirect_uri",
        "refresh_token",
        "access_token",
        "granted_scopes",
        "requested_scopes",
        "token_expires_at",
        "status",
        "updated_at",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def upsert(self, record: ProviderCredentialRecord) -> ProviderCredentialRecord:
        record.updated_at = datetime.now(timezone.utc)
        existing = await self.get(record.provider)
        if existing is None:
            await self._db.command(
                "sql",
                "CREATE VERTEX ProviderCredential CONTENT :payload",
                {"payload": self._to_payload(record)},
            )
            return record
        await self.update(record.provider, self._to_payload(record))
        return record

    async def get(self, provider: str) -> ProviderCredentialRecord | None:
        response = await self._db.command(
            "sql",
            "SELECT FROM ProviderCredential WHERE provider = :provider LIMIT 1",
            {"provider": provider},
        )
        rows = response.get("result", [])
        return self._to_record(rows[0]) if rows else None

    async def update(self, provider: str, updates: dict[str, Any]) -> None:
        invalid = set(updates) - self.ALLOWED_UPDATE_FIELDS
        if invalid:
            raise ValueError(
                f"Unsupported ProviderCredential update fields: {', '.join(sorted(invalid))}"
            )
        set_clauses = [f"{field} = :{field}" for field in updates]
        params = {
            "provider": provider,
            **{key: VerificationRepository._prepare(value) for key, value in updates.items()},
        }
        await self._db.command(
            "sql",
            f"UPDATE ProviderCredential SET {', '.join(set_clauses)} WHERE provider = :provider",
            params,
        )

    async def list_all(self) -> list[ProviderCredentialRecord]:
        response = await self._db.command(
            "sql", "SELECT FROM ProviderCredential LIMIT 100", {}
        )
        return [self._to_record(row) for row in response.get("result", [])]

    def _to_record(self, row: dict[str, Any]) -> ProviderCredentialRecord:
        scopes = row.get("granted_scopes")
        requested = row.get("requested_scopes")
        return ProviderCredentialRecord(
            provider=str(row.get("provider") or ""),
            client_id=str(row.get("client_id") or ""),
            redirect_uri=str(row.get("redirect_uri") or ""),
            refresh_token=row.get("refresh_token"),
            access_token=row.get("access_token"),
            granted_scopes=scopes if isinstance(scopes, list) else [],
            requested_scopes=requested if isinstance(requested, list) else [],
            token_expires_at=row.get("token_expires_at"),
            status=str(row.get("status") or "active"),
            updated_at=row.get("updated_at"),
        )

    def _to_payload(self, record: ProviderCredentialRecord) -> dict[str, Any]:
        return {
            "provider": record.provider,
            "client_id": record.client_id,
            "redirect_uri": record.redirect_uri,
            "refresh_token": record.refresh_token,
            "access_token": record.access_token,
            "granted_scopes": record.granted_scopes,
            "requested_scopes": record.requested_scopes,
            "token_expires_at": VerificationRepository._prepare(record.token_expires_at),
            "status": record.status,
            "updated_at": VerificationRepository._prepare(record.updated_at),
        }


class ColdMissQueryRepository:
    """Persisted cold-miss search terms (spec v2 Section 9.1)."""

    ALLOWED_UPDATE_FIELDS = {
        "status",
        "registry_done",
        "github_done",
        "servers_found",
        "updated_at",
    }

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def enqueue(self, term: str) -> tuple[ColdMissQueryRecord, bool]:
        """Record the term; returns the record and whether it is newly queued."""
        query_key = _query_key_for(term)
        existing = await self.get(term)
        if existing is not None and existing.status != "done":
            return existing, False
        record = ColdMissQueryRecord(
            query_key=query_key,
            term=term,
            status="queued",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        if existing is None:
            await self._db.command(
                "sql",
                "CREATE VERTEX ColdMissQuery CONTENT :payload",
                {"payload": {
                    "query_key": record.query_key,
                    "term": record.term,
                    "status": record.status,
                    "registry_done": False,
                    "github_done": False,
                    "servers_found": 0,
                    "created_at": VerificationRepository._prepare(record.created_at),
                    "updated_at": VerificationRepository._prepare(record.updated_at),
                }},
            )
        else:
            # Re-run the cascade for a term that previously found nothing.
            await self.update(term, {
                "status": "queued",
                "registry_done": False,
                "github_done": False,
                "updated_at": datetime.now(timezone.utc),
            })
        return record, True

    async def get(self, term: str) -> ColdMissQueryRecord | None:
        query_key = _query_key_for(term)
        response = await self._db.command(
            "sql",
            "SELECT FROM ColdMissQuery WHERE query_key = :query_key LIMIT 1",
            {"query_key": query_key},
        )
        rows = response.get("result", [])
        return self._to_record(rows[0]) if rows else None

    async def update(self, term: str, updates: dict[str, Any]) -> None:
        invalid = set(updates) - self.ALLOWED_UPDATE_FIELDS
        if invalid:
            raise ValueError(
                f"Unsupported ColdMissQuery update fields: {', '.join(sorted(invalid))}"
            )
        query_key = _query_key_for(term)
        set_clauses = [f"{field} = :{field}" for field in updates]
        params = {
            "query_key": query_key,
            **{key: VerificationRepository._prepare(value) for key, value in updates.items()},
        }
        await self._db.command(
            "sql",
            f"UPDATE ColdMissQuery SET {', '.join(set_clauses)} WHERE query_key = :query_key",
            params,
        )

    async def list_pending(self, limit: int = 20) -> list[ColdMissQueryRecord]:
        response = await self._db.command(
            "sql",
            "SELECT FROM ColdMissQuery WHERE status IN [:s1, :s2] LIMIT :limit",
            {"s1": "queued", "s2": "checking_registry", "limit": limit},
        )
        return [self._to_record(row) for row in response.get("result", [])]

    def _to_record(self, row: dict[str, Any]) -> ColdMissQueryRecord:
        return ColdMissQueryRecord(
            query_key=str(row.get("query_key") or ""),
            term=str(row.get("term") or ""),
            status=str(row.get("status") or "queued"),
            registry_done=bool(row.get("registry_done", False)),
            github_done=bool(row.get("github_done", False)),
            servers_found=int(row.get("servers_found") or 0),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


class VerificationDecisionRepository:
    """Append-only decision log for observability (spec v2 Section 12)."""

    def __init__(self, db: ArcadeDBClient) -> None:
        self._db = db

    async def record(self, decision: VerificationDecisionRecord) -> None:
        decision.decided_at = datetime.now(timezone.utc)
        await self._db.command(
            "sql",
            "CREATE VERTEX VerificationDecision CONTENT :payload",
            {"payload": {
                "decision_id": str(decision.decision_id),
                "server_id": str(decision.server_id),
                "transport": decision.transport,
                "stage": decision.stage,
                "check": decision.check,
                "reason": decision.reason,
                "quality_score": decision.quality_score,
                "confidence": decision.confidence,
                "resulting_status": decision.resulting_status,
                "decided_at": VerificationRepository._prepare(decision.decided_at),
            }},
        )

    async def rejection_breakdown(self) -> list[dict[str, Any]]:
        """Per (stage, check) rejection counts - the miscalibration signal."""
        response = await self._db.command(
            "sql",
            """
            SELECT stage, check, count(*) AS total
            FROM VerificationDecision
            WHERE resulting_status IN [:s1, :s2]
            GROUP BY stage, check
            LIMIT 100
            """,
            {"s1": "rejected", "s2": "malformed"},
        )
        return list(response.get("result", []))
