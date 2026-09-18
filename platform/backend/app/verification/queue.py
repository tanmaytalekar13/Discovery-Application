"""Background worker pool for the verification pipeline (spec v2 Sections 5-7, 9.1, 11).

An in-process asyncio worker pool replaces Celery/BullMQ: verification
volume is deliberately tiny (3-5 working servers needed), so a bounded
async loop with DB-backed queue state gives the same decoupling without
a second runtime. The golden rule is unchanged - search never calls any
function here; workers only read `pending`/`retry_pending` records and
write results back through the repository.

Three loops:
- verification workers: drain `pending` / `retry_pending` servers, then
  re-attempt `review` records that still have attempts left, and
  re-verify expired-TTL `verified` records (Section 11);
- TTL re-verification: reset the attempt counter on expired `verified`
  records (Section 11) so the worker loop re-verifies them while they
  keep serving their last known (stale-but-available) status;
- cold-miss cascade: registry -> GitHub discovery for search misses (9.1).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.discovery.github.client import GitHubDiscoveryAdapter
from app.discovery.mcp_registry.client import MCPRegistryClient
from app.verification.ingestion import VerificationIngestionService
from app.verification.models import (
    ColdMissQueryRecord,
    McpServerRecord,
    ServerStatus,
)
from app.verification.prefilter import prefilter_server, prefilter_server_with_size
from app.verification.repository import (
    ColdMissQueryRepository,
    VerificationDecisionRepository,
    VerificationRepository,
)
from app.verification.verifiers import VerificationPipeline, next_retry_delay

logger = logging.getLogger(__name__)


@dataclass
class WorkerHandles:
    """Everything the loops need; built once from settings."""

    servers: VerificationRepository
    decisions: VerificationDecisionRepository
    cold_miss: ColdMissQueryRepository
    ingestion: VerificationIngestionService
    pipeline: VerificationPipeline
    registry_client: MCPRegistryClient | None
    github_adapter: GitHubDiscoveryAdapter | None
    poll_interval_s: float = 30.0
    ttl_check_interval_s: float = 6 * 3600.0
    cold_miss_poll_s: float = 60.0


def build_worker_handles(
    settings: Settings,
    db_client,
    *,
    provider_repository=None,
) -> WorkerHandles:
    from app.verification.models import ProviderCredentialRecord  # noqa: F401

    servers = VerificationRepository(db_client)
    decisions = VerificationDecisionRepository(db_client)
    cold_miss = ColdMissQueryRepository(db_client)
    ingestion = VerificationIngestionService(servers)
    pipeline = VerificationPipeline(servers, decisions, settings)
    if provider_repository is not None:
        pipeline.set_provider_repository(provider_repository)

    registry_client = (
        MCPRegistryClient() if settings.enable_mcp_registry_discovery else None
    )
    github_adapter = (
        GitHubDiscoveryAdapter(token=settings.github_token)
        if settings.enable_github_discovery else None
    )

    try:
        poll = float(os.getenv("VERIFICATION_POLL_INTERVAL_SECONDS", "30"))
    except ValueError:
        poll = 30.0

    return WorkerHandles(
        servers=servers,
        decisions=decisions,
        cold_miss=cold_miss,
        ingestion=ingestion,
        pipeline=pipeline,
        registry_client=registry_client,
        github_adapter=github_adapter,
        poll_interval_s=max(poll, 5.0),
    )


# ---------------------------------------------------------------------------
# Ingestion: scheduled source sweep (Section 3, primary trigger)
# ---------------------------------------------------------------------------


async def run_scheduled_ingestion(handles: WorkerHandles, max_results: int = 20) -> int:
    """Poll official registry (and optionally GitHub) for new servers."""
    ingested = 0

    if handles.registry_client is not None:
        try:
            candidates = await handles.registry_client.search(None, max_results=max_results)
        except Exception as exc:  # noqa: BLE001 - one source failing must not stop the sweep
            logger.warning("Scheduled registry sweep failed: %s", exc)
            candidates = []
        for candidate in candidates:
            ingested += await _ingest_candidate(handles, candidate, from_registry=True)

    if handles.github_adapter is not None:
        try:
            candidates = await handles.github_adapter.discover_mcp("mcp-server", max_results)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scheduled GitHub sweep failed: %s", exc)
            candidates = []
        for candidate in candidates:
            ingested += await _ingest_candidate(handles, candidate, from_registry=False)

    return ingested


async def _ingest_candidate(
    handles: WorkerHandles, candidate, *, from_registry: bool
) -> int:
    from app.verification.ingestion import from_github_candidate, from_mcp_registry_candidate

    record = (
        from_mcp_registry_candidate(candidate) if from_registry
        else from_github_candidate(candidate)
    )
    result = await handles.ingestion.ingest(record)
    if not result.created:
        return 0

    # Prefilter synchronously at ingestion (Section 4): malformed /
    # known-incompatible / oversized entries never enter the verification
    # queue. The size check consults npm/PyPI registry metadata for local
    # packages (the exit-137 prefilter) and fails open when unknown.
    prefilter = await prefilter_server_with_size(result.record)
    if not prefilter.passed:
        status = (
            ServerStatus.REJECTED if prefilter.status == "rejected"
            else ServerStatus.MALFORMED
        )
        await handles.servers.update(result.record.server_id, {
            "status": status.value,
            "rejection_stage": "prefilter",
            "rejection_check": prefilter.failures[0] if prefilter.failures else None,
            "verification_details": {"prefilter_failures": prefilter.failures},
        })
        return 0

    await handles.servers.update(result.record.server_id, {
        "status": ServerStatus.PENDING.value,
    })
    return 1


# ---------------------------------------------------------------------------
# Verification worker loop (Section 5)
# ---------------------------------------------------------------------------


def _due_for_attempt(record: McpServerRecord, now: datetime) -> bool:
    """Retry-schedule eligibility (Section 5.2/6: now / +1h / +6h).

    A record is due when its backoff window since the last attempt has
    elapsed. Records never attempted are immediately due. An unreadable
    timestamp fails open so a malformed stamp cannot strand a record
    forever.
    """
    last = record.last_attempt_at
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    required = next_retry_delay(record.attempts_completed)
    return (now - last_dt).total_seconds() >= required


async def process_next_server(handles: WorkerHandles) -> bool:
    """Verify one eligible server; True when work was done.

    Eligibility, in priority order:
      1. `pending` (fresh work, incl. user-requested rechecks);
      2. `retry_pending` whose backoff window has elapsed;
      3. expired-TTL `verified` records (Section 11 re-verification:
         they keep serving their stale status while being re-checked);
      4. `review` records that still have attempts left (the scorer's
         "score >= 80 but low confidence -> needs another retry pass"
         bucket). Without this, a remote server verified on quality in
         its first attempt but planned for 3 (auth ambiguity) would sit
         in `review` forever and never earn the verified badge.

    Attempts-exhausted `review` records are NOT auto-retried - they
    belong to the human review queue (Section 10). Without that guard a
    permanently mid-score server would be re-verified in a tight loop
    forever.
    """
    now = datetime.now(timezone.utc)

    record = None
    queued = await handles.servers.list_status(
        ServerStatus.PENDING.value, ServerStatus.RETRY_PENDING.value, limit=5
    )
    for candidate in queued:
        if candidate.status.value == ServerStatus.PENDING.value:
            record = candidate
            break
        if _due_for_attempt(candidate, now):
            record = candidate
            break

    if record is None:
        expired = await handles.servers.list_expired_ttl(
            now_iso=now.isoformat(), limit=1
        )
        if expired:
            record = expired[0]

    if record is None:
        reviewing = await handles.servers.list_status(
            ServerStatus.REVIEW.value, limit=20
        )
        for candidate in reviewing:
            if (
                candidate.attempts_completed < candidate.attempts_planned
                and _due_for_attempt(candidate, now)
            ):
                record = candidate
                break

    if record is None:
        return False

    # Prefilter re-check (cheap, catches records enqueued before a
    # prefilter rule change).
    prefilter = prefilter_server(record)
    if not prefilter.passed:
        status = (
            ServerStatus.REJECTED if prefilter.status == "rejected"
            else ServerStatus.MALFORMED
        )
        await handles.servers.update(record.server_id, {
            "status": status.value,
            "rejection_stage": "prefilter",
            "rejection_check": prefilter.failures[0] if prefilter.failures else None,
        })
        return True

    try:
        await handles.pipeline.verify(record)
    except Exception as exc:  # noqa: BLE001 - a crashed verification must not kill the worker
        logger.exception("Verification of %s failed", record.source_url)
        await handles.servers.update(record.server_id, {
            "status": ServerStatus.RETRY_PENDING.value,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "verification_details": {
                **(record.verification_details or {}),
                "worker_error": str(exc)[:500],
            },
        })
    return True


async def verification_loop(handles: WorkerHandles) -> None:
    logger.info("Verification worker loop started (interval=%ss)", handles.poll_interval_s)
    while True:
        try:
            did_work = await process_next_server(handles)
            if not did_work:
                await asyncio.sleep(handles.poll_interval_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Verification loop iteration failed")
            await asyncio.sleep(handles.poll_interval_s)


# ---------------------------------------------------------------------------
# TTL re-verification cron (Section 11)
# ---------------------------------------------------------------------------


async def requeue_expired_ttl(handles: WorkerHandles) -> int:
    """Reset attempts on expired `verified` records (Section 11).

    The record keeps serving its last known status (stale-but-available)
    while the verification worker loop re-verifies it: these records are
    now also part of `process_next_server` eligibility, so the attempt
    reset is what actually re-enqueues them.
    """
    expired = await handles.servers.list_expired_ttl()
    count = 0
    for record in expired:
        await handles.servers.update(record.server_id, {
            "attempts_completed": 0,
        })
        count += 1
    return count


async def ttl_loop(handles: WorkerHandles) -> None:
    while True:
        try:
            await requeue_expired_ttl(handles)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("TTL re-verification sweep failed")
        await asyncio.sleep(handles.ttl_check_interval_s)


# ---------------------------------------------------------------------------
# Cold-miss cascade (Section 9.1) - always async, never inline with search
# ---------------------------------------------------------------------------


async def enqueue_cold_miss(handles: WorkerHandles, term: str) -> tuple[ColdMissQueryRecord, bool]:
    """Called by the search API on a local-registry miss. Persists the
    term; the cascade loop picks it up. Returns (record, newly_queued)."""
    return await handles.cold_miss.enqueue(term)


async def run_cold_miss_cascade(handles: WorkerHandles, term: str) -> int:
    """Source cascade for one search term: official registry, then GitHub.

    Whatever is found goes through the FULL normal pipeline (ingest ->
    prefilter -> queue -> scoring) - a cold-miss hit is never
    fast-tracked to `verified`.
    """
    record = await handles.cold_miss.get(term)
    if record is None:
        record, _ = await handles.cold_miss.enqueue(term)

    found = 0

    if not record.registry_done:
        await handles.cold_miss.update(term, {
            "status": "checking_registry",
            "updated_at": datetime.now(timezone.utc),
        })
        if handles.registry_client is not None:
            try:
                candidates = await handles.registry_client.search(term, max_results=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cold-miss registry lookup failed for %r: %s", term, exc)
                candidates = []
            for candidate in candidates:
                found += await _ingest_candidate(handles, candidate, from_registry=True)
        await handles.cold_miss.update(term, {
            "registry_done": True,
            "servers_found": found,
            "updated_at": datetime.now(timezone.utc),
        })
        # Step 2 before step 3, not in parallel (Section 9.1): only fall
        # through to GitHub when the official registry found nothing.
        if found:
            await handles.cold_miss.update(term, {
                "status": "done",
                "updated_at": datetime.now(timezone.utc),
            })
            return found

    if not record.github_done:
        await handles.cold_miss.update(term, {
            "status": "checking_github",
            "updated_at": datetime.now(timezone.utc),
        })
        if handles.github_adapter is not None:
            try:
                candidates = await handles.github_adapter.discover_mcp(term, max_results=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Cold-miss GitHub lookup failed for %r: %s", term, exc)
                candidates = []
            for candidate in candidates:
                found += await _ingest_candidate(handles, candidate, from_registry=False)
        await handles.cold_miss.update(term, {
            "github_done": True,
            "servers_found": found,
            "status": "done",
            "updated_at": datetime.now(timezone.utc),
        })

    return found


async def cold_miss_loop(handles: WorkerHandles) -> None:
    while True:
        try:
            pending = await handles.cold_miss.list_pending(limit=5)
            for record in pending:
                with contextlib.suppress(Exception):
                    logger.info("Cold-miss cascade running for %r", record.term)
                    await run_cold_miss_cascade(handles, record.term)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Cold-miss cascade loop failed")
        await asyncio.sleep(handles.cold_miss_poll_s)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------


def start_background_loops(handles: WorkerHandles) -> list[asyncio.Task]:
    return [
        asyncio.create_task(verification_loop(handles), name="verification-workers"),
        asyncio.create_task(ttl_loop(handles), name="ttl-reverification"),
        asyncio.create_task(cold_miss_loop(handles), name="cold-miss-cascade"),
    ]


async def stop_background_loops(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
