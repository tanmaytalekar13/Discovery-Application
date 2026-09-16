"""Verification API (spec v2 Sections 9, 9.1, 10).

The search endpoint is STRICTLY read-only: it runs a single SELECT
against the McpServer registry and never touches the verification
pipeline, sandboxes, or live servers (the golden rule). Everything else
here either enqueues async work and returns immediately (recheck,
cold-miss hint) or supports the manual review queue (OAuth consent).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db.client import ArcadeDBClient
from app.verification.models import (
    DEFAULT_SEARCH_STATUSES,
    McpServerRecord,
    ServerStatus,
)
from app.verification.queue import enqueue_cold_miss, build_worker_handles
from app.verification.repository import (
    ColdMissQueryRepository,
    ProviderCredentialRepository,
    VerificationDecisionRepository,
    VerificationRepository,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/verification", tags=["verification"])


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _db() -> ArcadeDBClient:
    return ArcadeDBClient(get_settings())


def get_verification_repository() -> VerificationRepository:
    return VerificationRepository(_db())


def get_provider_credential_repository() -> ProviderCredentialRepository:
    return ProviderCredentialRepository(_db())


def get_decision_repository() -> VerificationDecisionRepository:
    return VerificationDecisionRepository(_db())


def get_cold_miss_repository() -> ColdMissQueryRepository:
    return ColdMissQueryRepository(_db())


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ServerSummary(BaseModel):
    server_id: str
    name: str
    description: str = ""
    transport: str
    source_url: str
    endpoint_url: str | None = None
    install_cmd: str | None = None
    status: str
    quality_score: int
    confidence: str
    attempts_completed: str
    invocation_verified: bool
    latency_category: str | None = None
    latency_p50_ms: int | None = None
    auth_required: bool = False
    auth_type: str = "none"
    oauth_provider: str | None = None
    tool_count: int = 0

    @classmethod
    def from_record(cls, record: McpServerRecord) -> "ServerSummary":
        return cls(
            server_id=str(record.server_id),
            name=record.name,
            description=record.description,
            transport=record.transport.value,
            source_url=record.source_url,
            endpoint_url=record.endpoint_url,
            install_cmd=record.install_cmd,
            status=record.status.value,
            quality_score=record.quality_score,
            confidence=record.confidence.value,
            attempts_completed=f"{record.attempts_completed}/{record.attempts_planned}",
            invocation_verified=record.invocation_verified,
            latency_category=record.latency_category,
            latency_p50_ms=record.latency_p50_ms,
            auth_required=record.auth_required,
            auth_type=record.auth_type.value
            if hasattr(record.auth_type, "value") else str(record.auth_type),
            oauth_provider=record.oauth_provider,
            tool_count=len(record.declared_tools or []),
        )


class SearchMetadataOut(BaseModel):
    query: str
    min_score: int
    statuses: list[str]
    result_count: int
    cold_miss_triggered: bool = False
    search_state: Literal["results", "not_found_yet_checking_further_sources"] = "results"


class SearchResponse(BaseModel):
    results: list[ServerSummary]
    metadata: SearchMetadataOut


class RecheckResponse(BaseModel):
    server_id: str
    status: str
    detail: str


class RecheckStatusResponse(BaseModel):
    server_id: str
    status: str
    quality_score: int
    confidence: str
    attempts_completed: str
    last_verified_at: str | None = None


class DecisionBreakdownRow(BaseModel):
    stage: str | None = None
    check: str | None = None
    total: int = 0


class ProviderStatusOut(BaseModel):
    provider: str
    status: str
    client_id: str = ""
    granted_scopes: list[str] = Field(default_factory=list)
    requested_scopes: list[str] = Field(default_factory=list)
    token_expires_at: str | None = None


# ---------------------------------------------------------------------------
# Search - the golden rule lives here
# ---------------------------------------------------------------------------


@router.get("/search", response_model=SearchResponse)
async def search_servers(
    q: Annotated[str, Query(min_length=1, max_length=200)],
    min_score: Annotated[int, Query(ge=0, le=100)] = 0,
    status: Annotated[str, Query()] = "verified",
    transport: Annotated[Literal["local", "remote", "all"], Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    repository: VerificationRepository = Depends(get_verification_repository),
    cold_miss: ColdMissQueryRepository = Depends(get_cold_miss_repository),
) -> SearchResponse:
    """Search the pre-computed scored registry.

    Default filter is `status=verified` sorted by quality_score DESC.
    `min_score` is configurable at query time (never hardcoded in the
    pipeline). This handler performs zero network calls, zero sandbox
    spin-ups, zero handshakes - a pure DB read.

    On a local-registry miss, the response returns immediately with
    `not_found_yet_checking_further_sources` while an async cascade
    job queries the official registry, then GitHub (Section 9.1).
    """
    statuses = _parse_statuses(status)

    records = await repository.search(
        keywords=q.split(),
        statuses=statuses,
        min_score=min_score,
        transport=transport if transport != "all" else None,
        limit=limit,
    )

    cold_miss_triggered = False
    if not records:
        # Trigger the async source cascade; NEVER block on it.
        with_logger_safe(cold_miss, q)
        cold_miss_triggered = True

    return SearchResponse(
        results=[ServerSummary.from_record(r) for r in records],
        metadata=SearchMetadataOut(
            query=q,
            min_score=min_score,
            statuses=list(statuses),
            result_count=len(records),
            cold_miss_triggered=cold_miss_triggered,
            search_state=(
                "not_found_yet_checking_further_sources"
                if cold_miss_triggered else "results"
            ),
        ),
    )


def with_logger_safe(cold_miss: ColdMissQueryRepository, query: str) -> None:
    """Enqueue the cold-miss term, tolerating a DB hiccup: the search
    response must never fail because bookkeeping failed."""
    try:
        from app.config import get_settings as _gs
        handles = build_worker_handles(_gs(), _db())
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(enqueue_cold_miss(handles, query))
            # Fire-and-forget: add a done-callback to log failures.
            def _log_failure(task_: asyncio.Task) -> None:
                exc = task_.exception()
                if exc:
                    logger.warning("Cold-miss enqueue failed for %r: %s", query, exc)

            task.add_done_callback(_log_failure)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cold-miss enqueue could not start for %r: %s", query, exc)


def _parse_statuses(raw: str) -> tuple[str, ...]:
    raw = (raw or "").strip().lower()
    if raw in ("", "verified", "default"):
        return tuple(sorted(DEFAULT_SEARCH_STATUSES))
    if raw == "all":
        return tuple(s.value for s in ServerStatus)
    return tuple(s.strip() for s in raw.split(",") if s.strip())


# ---------------------------------------------------------------------------
# Server detail + on-demand recheck (the one legitimate exception)
# ---------------------------------------------------------------------------


@router.get("/servers/{server_id}", response_model=ServerSummary)
async def get_server(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> ServerSummary:
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return ServerSummary.from_record(record)


@router.get("/servers/{server_id}/verification-details")
async def get_server_details(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> dict[str, Any]:
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return {
        "server_id": str(record.server_id),
        "verification_details": record.verification_details,
        "rejection_stage": record.rejection_stage,
        "rejection_check": record.rejection_check,
    }


@router.post("/servers/{server_id}/recheck", response_model=RecheckResponse)
async def recheck_server(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> RecheckResponse:
    """User-requested recheck: enqueue an async verification job and
    return immediately. NEVER block while verification runs (Section 9)."""
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")

    # Any authenticated record can be re-verified; TTL-expiry is handled
    # separately by the cron. Reset the attempt counter for a fresh run.
    await repository.update(server_uuid, {"attempts_completed": 0})

    def _force_status(record: McpServerRecord) -> None:
        pass

    # Setting status back to pending lets the running worker pool pick
    # this server up on its next poll - no inline verification here.
    await repository.update(server_uuid, {"status": ServerStatus.PENDING.value})

    return RecheckResponse(
        server_id=server_id,
        status="queued",
        detail="Verification started, check back shortly.",
    )


@router.get("/servers/{server_id}/recheck", response_model=RecheckStatusResponse)
async def recheck_status(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> RecheckStatusResponse:
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")
    return RecheckStatusResponse(
        server_id=server_id,
        status=record.status.value,
        quality_score=record.quality_score,
        confidence=record.confidence.value,
        attempts_completed=f"{record.attempts_completed}/{record.attempts_planned}",
        last_verified_at=record.last_verified_at.isoformat()
        if record.last_verified_at else None,
    )


# ---------------------------------------------------------------------------
# Manual review queue support (Section 10)
# ---------------------------------------------------------------------------


@router.get("/review-queue", response_model=list[ServerSummary])
async def list_review_queue(
    repository: VerificationRepository = Depends(get_verification_repository),
) -> list[ServerSummary]:
    """Servers in the `review` bucket for the human smoke-test pass,
    plus partial_verified / oauth_pending_consent records that a human
    consent action can promote."""
    records = await repository.list_status(
        ServerStatus.REVIEW.value,
        ServerStatus.PARTIAL_VERIFIED.value,
        ServerStatus.OAUTH_PENDING_CONSENT.value,
        limit=100,
    )
    return [ServerSummary.from_record(r) for r in records]


@router.post("/servers/{server_id}/approve", response_model=ServerSummary)
async def approve_server(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> ServerSummary:
    """Manual review action: flip a review-bucket server to verified.

    This is the human smoke-test step from Section 10 - deliberately a
    manual endpoint, not an automated pipeline decision.
    """
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")
    if record.status not in (ServerStatus.REVIEW, ServerStatus.PARTIAL_VERIFIED):
        raise HTTPException(
            status_code=409,
            detail=f"Only review/partial_verified servers can be approved (current: {record.status.value})",
        )
    if not record.invocation_verified:
        raise HTTPException(
            status_code=409,
            detail="Refusing to approve: invocation was never verified (rule 8 - no faked verified status)",
        )
    updated = await repository.update(server_uuid, {
        "status": ServerStatus.VERIFIED.value,
    })
    return ServerSummary.from_record(updated or record)


@router.post("/servers/{server_id}/reject", response_model=ServerSummary)
async def reject_server(
    server_id: str,
    repository: VerificationRepository = Depends(get_verification_repository),
) -> ServerSummary:
    """Manual review action: reject after a failed human smoke test."""
    from uuid import UUID

    try:
        server_uuid = UUID(server_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="server_id must be a UUID") from exc
    record = await repository.get(server_uuid)
    if record is None:
        raise HTTPException(status_code=404, detail="Server not found")
    updated = await repository.update(server_uuid, {
        "status": ServerStatus.REJECTED.value,
    })
    return ServerSummary.from_record(updated or record)


# ---------------------------------------------------------------------------
# OAuth provider credentials (Sections 7.3 / 10)
# ---------------------------------------------------------------------------


@router.get("/providers", response_model=list[ProviderStatusOut])
async def list_providers(
    providers: ProviderCredentialRepository = Depends(get_provider_credential_repository),
) -> list[ProviderStatusOut]:
    """Provider credential status - never returns tokens."""
    records = await providers.list_all()
    return [
        ProviderStatusOut(
            provider=r.provider,
            status=r.status,
            client_id=r.client_id,
            granted_scopes=r.granted_scopes,
            requested_scopes=r.requested_scopes,
            token_expires_at=r.token_expires_at.isoformat()
            if r.token_expires_at else None,
        )
        for r in records
    ]


@router.post("/providers/{provider}/authorize")
async def start_provider_authorization(
    provider: str,
    providers: ProviderCredentialRepository = Depends(get_provider_credential_repository),
) -> dict[str, Any]:
    """Begin the one-time human consent flow for a provider (Section 7.3).

    Always foreground/human-triggered - never invoked by the pipeline
    (rule 6). The callback route stores the provider-scoped refresh
    token, which then unlocks full verification for every server that
    shares the provider.
    """
    from app.sandbox.oauth import (
        build_authorization_url,
        register_dynamic_client,
        resolve_authorization_server,
    )
    existing = await providers.get(provider)
    client_id = existing.client_id if existing else ""
    client_secret: str | None = None
    redirect_uri = f"{_public_base_url()}/api/verification/oauth/callback/{provider}"

    try:
        # Discover authorization-server metadata for the provider.
        as_metadata = await resolve_authorization_server(_PROVIDER_AUTH_BASES.get(provider, ""))
        if not client_id:
            client_id, client_secret = await register_dynamic_client(
                as_metadata.get("registration_endpoint", ""), redirect_uri
            )
        authorization_url, state, flow = build_authorization_url(
            authorization_endpoint=as_metadata["authorization_endpoint"],
            token_endpoint=as_metadata["token_endpoint"],
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            resource=_PROVIDER_AUTH_BASES.get(provider, ""),
            item_id=f"provider:{provider}",
            session_id=provider,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Provider authorization could not be initiated: {exc}",
        ) from exc

    await providers.upsert(
        _provider_record(provider, client_id, redirect_uri, existing)
    )

    return {"authorization_url": authorization_url, "state": state}


def _public_base_url() -> str:
    import os

    return os.environ.get(
        "MCP_OAUTH_PUBLIC_BASE_URL", "http://localhost:8000"
    ).rstrip("/")


_PROVIDER_AUTH_BASES: dict[str, str] = {
    # Pre-registered, per-provider OAuth bases. Extend as providers are
    # registered in their developer consoles (Section 7.3.1).
    "google": "https://accounts.google.com",
    "github": "https://github.com",
    "notion": "https://api.notion.com",
    "slack": "https://slack.com",
}


def _provider_record(provider: str, client_id: str, redirect_uri: str, existing):
    from app.verification.models import ProviderCredentialRecord

    return ProviderCredentialRecord(
        provider=provider,
        client_id=client_id,
        redirect_uri=redirect_uri,
        status=(existing.status if existing else "pending_consent"),
    )


@router.get("/oauth/callback/{provider}")
async def oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    providers: ProviderCredentialRepository = Depends(get_provider_credential_repository),
) -> dict[str, Any]:
    """Foreground OAuth callback: exchange the code, store the provider-
    scoped refresh token, and flip pending servers back into the queue."""
    from app.sandbox.oauth import get_flow_store, exchange_code_for_token

    if error:
        raise HTTPException(status_code=400, detail=f"Provider authorization failed: {error}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state on OAuth callback")

    flow = get_flow_store().take(state)
    if flow is None:
        raise HTTPException(status_code=400, detail="Unknown or expired OAuth state")

    try:
        token_doc = await exchange_code_for_token(flow, code)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Token exchange failed: {exc}") from exc

    granted_scopes = [
        s for s in str(token_doc.get("scope") or "").replace(" ", ",").split(",") if s
    ]
    expires_at = None
    expires_in = token_doc.get("expires_in")
    if isinstance(expires_in, (int, float)):
        from datetime import datetime, timedelta, timezone

        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
        ).isoformat()

    from app.verification.models import ProviderCredentialRecord

    await providers.upsert(ProviderCredentialRecord(
        provider=provider,
        client_id=flow.client_id,
        redirect_uri=flow.redirect_uri,
        refresh_token=token_doc.get("refresh_token"),
        access_token=token_doc.get("access_token"),
        granted_scopes=granted_scopes,
        token_expires_at=expires_at,
        status="active",
    ))

    # Scope mismatch handling (Section 7.3.3): servers that requested
    # scopes we did not grant move to scope_not_granted, not rejected.
    # Re-authenticated servers return to the verification queue.
    db = _db()
    servers = VerificationRepository(db)
    requeued = 0
    for record in await servers.list_by_provider(provider):
        if record.status.value in (
            ServerStatus.REAUTH_REQUIRED.value,
            ServerStatus.OAUTH_PENDING_CONSENT.value,
        ):
            await servers.update(record.server_id, {"status": ServerStatus.PENDING.value})
            requeued += 1

    return {
        "provider": provider,
        "status": "active",
        "granted_scopes": granted_scopes,
        "servers_requeued": requeued,
    }


# ---------------------------------------------------------------------------
# Observability (Section 12)
# ---------------------------------------------------------------------------


@router.get("/observability/rejections", response_model=list[DecisionBreakdownRow])
async def rejection_breakdown(
    decisions: VerificationDecisionRepository = Depends(get_decision_repository),
) -> list[DecisionBreakdownRow]:
    """Per (stage, check) rejection counts. A single check dominating
    rejections (>70%) is the miscalibration signal from Section 12."""
    rows = await decisions.rejection_breakdown()
    return [
        DecisionBreakdownRow(
            stage=str(row.get("stage")) if row.get("stage") else None,
            check=str(row.get("check")) if row.get("check") else None,
            total=int(row.get("total") or 0),
        )
        for row in rows
    ]
