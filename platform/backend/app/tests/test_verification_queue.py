"""Worker-loop eligibility tests (verification queue).

Covers the retry-schedule rules that decide WHICH record the worker
verifies next:

- `pending` is always picked immediately (user rechecks must not wait);
- `retry_pending` waits out its backoff window (now / +1h / +6h);
- expired-TTL `verified` records are re-verified (Section 11);
- `review` records with attempts left get their next pass (the scorer's
  high-score-low-confidence bucket) - previously they were stranded
  forever because the worker only ever read pending/retry_pending;
- attempts-exhausted `review` records are left to the human queue
  (Section 10) instead of being re-verified in a tight loop.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.verification.models import McpServerRecord, ServerStatus, Transport
from app.verification.queue import process_next_server, requeue_expired_ttl


def _record(
    status: ServerStatus,
    *,
    transport: Transport = Transport.REMOTE,
    attempts_completed: int = 1,
    attempts_planned: int = 3,
    last_attempt_at: datetime | None = None,
) -> McpServerRecord:
    return McpServerRecord(
        name=f"srv-{status.value}",
        source_url=f"https://example.com/{uuid4()}",
        transport=transport,
        endpoint_url="https://mcp.example.com/mcp",
        status=status,
        attempts_completed=attempts_completed,
        attempts_planned=attempts_planned,
        last_attempt_at=last_attempt_at,
    )


class _FakeServerRepo:
    def __init__(self, by_status: dict[str, list], expired=()):
        self._by_status = by_status
        self._expired = list(expired)
        self.updates: list[tuple[str, dict]] = []

    async def list_status(self, *statuses, limit=50):
        out = []
        for status in statuses:
            out.extend(self._by_status.get(status, []))
        return out[:limit]

    async def list_expired_ttl(self, now_iso=None, limit=50):
        return self._expired[:limit]

    async def update(self, server_id, updates):
        self.updates.append((str(server_id), updates))
        return None


class _FakePipeline:
    def __init__(self):
        self.verified: list = []

    async def verify(self, record):
        self.verified.append(record)
        return record


def _handles(by_status: dict[str, list], expired=()):
    servers = _FakeServerRepo(by_status, expired=expired)
    pipeline = _FakePipeline()
    return SimpleNamespace(servers=servers, pipeline=pipeline), pipeline


NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_pending_is_picked_immediately_despite_fresh_attempt():
    """A user recheck flips status to pending - it must not wait behind
    the retry backoff window."""
    fresh = _record(
        ServerStatus.PENDING, last_attempt_at=NOW - timedelta(seconds=10)
    )
    handles, pipeline = _handles({ServerStatus.PENDING.value: [fresh]})

    assert await process_next_server(handles) is True
    assert pipeline.verified == [fresh]


@pytest.mark.asyncio
async def test_retry_pending_waits_out_backoff_window():
    """retry_pending whose last attempt was seconds ago is not due."""
    fresh = _record(
        ServerStatus.RETRY_PENDING, last_attempt_at=NOW - timedelta(seconds=30)
    )
    handles, pipeline = _handles({ServerStatus.RETRY_PENDING.value: [fresh]})

    assert await process_next_server(handles) is False
    assert pipeline.verified == []


@pytest.mark.asyncio
async def test_retry_pending_runs_once_backoff_elapsed():
    due = _record(
        ServerStatus.RETRY_PENDING, last_attempt_at=NOW - timedelta(hours=2)
    )
    handles, pipeline = _handles({ServerStatus.RETRY_PENDING.value: [due]})

    assert await process_next_server(handles) is True
    assert pipeline.verified == [due]


@pytest.mark.asyncio
async def test_expired_verified_record_is_reverified():
    """Section 11: an expired verified record is re-verified while it
    keeps serving its stale-but-available status."""
    stale = _record(
        ServerStatus.VERIFIED,
        attempts_completed=1,
        attempts_planned=1,
        last_attempt_at=NOW - timedelta(days=40),
    )
    handles, pipeline = _handles({}, expired=[stale])

    assert await process_next_server(handles) is True
    assert pipeline.verified == [stale]


@pytest.mark.asyncio
async def test_review_record_with_attempts_left_gets_next_pass():
    """The scorer's high-score-low-confidence bucket (score >= 80, 1 of 3
    remote attempts) must be re-attempted - previously these records were
    stranded in `review` forever and never earned the verified badge."""
    stranded = _record(
        ServerStatus.REVIEW,
        attempts_completed=1,
        attempts_planned=3,
        last_attempt_at=NOW - timedelta(hours=2),
    )
    handles, pipeline = _handles({ServerStatus.REVIEW.value: [stranded]})

    assert await process_next_server(handles) is True
    assert pipeline.verified == [stranded]


@pytest.mark.asyncio
async def test_review_record_waits_out_backoff_window():
    stranded = _record(
        ServerStatus.REVIEW,
        attempts_completed=1,
        attempts_planned=3,
        last_attempt_at=NOW - timedelta(seconds=30),
    )
    handles, pipeline = _handles({ServerStatus.REVIEW.value: [stranded]})

    assert await process_next_server(handles) is False
    assert pipeline.verified == []


@pytest.mark.asyncio
async def test_attempts_exhausted_review_record_is_left_to_human_queue():
    """Section 10: attempts-exhausted review records belong to the manual
    review queue - auto-retrying them would loop forever."""
    exhausted = _record(
        ServerStatus.REVIEW,
        attempts_completed=3,
        attempts_planned=3,
        last_attempt_at=NOW - timedelta(days=2),
    )
    handles, pipeline = _handles({ServerStatus.REVIEW.value: [exhausted]})

    assert await process_next_server(handles) is False
    assert pipeline.verified == []


@pytest.mark.asyncio
async def test_pending_wins_over_due_review_record():
    due_pending = _record(ServerStatus.PENDING)
    due_review = _record(
        ServerStatus.REVIEW,
        attempts_completed=1,
        attempts_planned=3,
        last_attempt_at=NOW - timedelta(hours=2),
    )
    handles, pipeline = _handles({
        ServerStatus.PENDING.value: [due_pending],
        ServerStatus.REVIEW.value: [due_review],
    })

    assert await process_next_server(handles) is True
    assert pipeline.verified == [due_pending]


@pytest.mark.asyncio
async def test_local_retry_pending_respects_backoff_too():
    """A local retry_pending record has attempts_planned=1 (exhausted) -
    the backoff guard keeps the worker from hammering it every poll."""
    flaky = _record(
        ServerStatus.RETRY_PENDING,
        transport=Transport.LOCAL,
        attempts_completed=1,
        attempts_planned=1,
        last_attempt_at=NOW - timedelta(seconds=30),
    )
    handles, pipeline = _handles({ServerStatus.RETRY_PENDING.value: [flaky]})

    assert await process_next_server(handles) is False
    assert pipeline.verified == []


@pytest.mark.asyncio
async def test_requeue_expired_ttl_resets_attempts():
    stale = _record(
        ServerStatus.VERIFIED,
        attempts_completed=1,
        attempts_planned=1,
    )
    handles, _ = _handles({}, expired=[stale])

    count = await requeue_expired_ttl(handles)

    assert count == 1
    assert handles.servers.updates == [
        (str(stale.server_id), {"attempts_completed": 0})
    ]
