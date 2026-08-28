from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID


from pydantic import BaseModel, Field


class TestRunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class DependencySnapshot(BaseModel):
    declared: list[str] = Field(default_factory=list)
    observed: list[str] = Field(default_factory=list)
    unexpected: list[str] = Field(default_factory=list)


class TestRun(BaseModel):
    run_id: UUID
    item_id: UUID

    type: str

    started_at: datetime
    completed_at: datetime | None = None

    status: TestRunStatus = TestRunStatus.RUNNING

    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)

    duration_ms: int | None = None

    errors: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)

    dependencies: DependencySnapshot = Field(
        default_factory=DependencySnapshot
    )