"""Domain models for the MCP Server Discovery & Verification system (spec v2).

The verification subsystem keeps its own records, deliberately separate
from the generic discovery `Item` catalog: search (Section 9) reads only
these records, and only the background verification pipeline (Section 5)
writes their status/score fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Transport(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


class AuthType(str, Enum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH2 = "oauth2"
    UNKNOWN = "unknown"


class OAuthFlow(str, Enum):
    AUTHORIZATION_CODE = "authorization_code"
    CLIENT_CREDENTIALS = "client_credentials"


class ServerStatus(str, Enum):
    """Every status from spec v2 Section 2.1 / Section 8."""

    PENDING = "pending"
    MALFORMED = "malformed"
    VERIFIED = "verified"
    REVIEW = "review"
    REJECTED = "rejected"
    UNVERIFIABLE = "unverifiable"
    RETRY_PENDING = "retry_pending"
    PARTIAL_VERIFIED = "partial_verified"
    OAUTH_PENDING_CONSENT = "oauth_pending_consent"
    REAUTH_REQUIRED = "reauth_required"
    SCOPE_NOT_GRANTED = "scope_not_granted"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class LatencyCategory(str, Enum):
    FAST = "fast"
    MODERATE = "moderate"
    SLOW = "slow"


# Statuses that the default search may return (Section 7.1 rule 5: a
# `partial_verified` server is deliberately NOT in this set).
DEFAULT_SEARCH_STATUSES: frozenset[str] = frozenset({ServerStatus.VERIFIED.value})


class VerificationStage(str, Enum):
    """Pipeline stages recorded per server in `verification_details`."""

    INGESTION = "ingestion"
    PREFILTER = "prefilter"
    INSTALL = "install"
    CONNECT = "connect"
    HANDSHAKE = "handshake"
    TOOLS_LIST = "tools_list"
    TOOL_INVOCATION = "tool_invocation"
    MALFORMED_INPUT = "malformed_input"
    SECURITY = "security"
    SEMANTIC = "semantic"


class VerificationCheck(str, Enum):
    """Soft-check names used by the scoring engine and observability."""

    INSTALL_CONNECT = "install_connect"
    HANDSHAKE_VALID = "handshake_valid"
    TOOLS_LIST_SCHEMA = "tools_list_schema"
    TOOL_INVOCATION = "tool_invocation"
    SEMANTIC_VALIDITY = "semantic_validity"
    NO_CRASH_MALFORMED_INPUT = "no_crash_malformed_input"
    SECURITY = "security"
    LATENCY = "latency"


class StageOutcomeKind(str, Enum):
    """Outcome classification for a single stage attempt (Section 5.2)."""

    SUCCESS = "success"
    TRANSPORT_FAIL = "transport_fail"      # retryable, not a verdict
    AUTH_REQUIRED = "auth_required"        # not a failure; auth path
    PROTOCOL_FAIL = "protocol_fail"        # real red flag
    TIMEOUT = "timeout"                    # inconclusive until attempts exhausted
    MALFORMED = "malformed"
    SECURITY_FLAG = "security_flag"




class ToolOutcome(BaseModel):
    """Result of verifying one declared tool."""

    tool_name: str
    invoked: bool = False
    invocation_status: str | None = None  # success | error | timeout
    semantic_valid: bool | None = None
    semantic_reason: str | None = None
    duration_ms: int | None = None
    graceful_error_on_malformed: bool | None = None
    detail: str | None = None


class StageResult(BaseModel):
    """One recorded pipeline stage outcome for a server."""

    stage: str
    kind: str
    attempt: int = 1
    detail: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = Field(default_factory=dict)


class McpServerRecord(BaseModel):
    """The registry-table server record (spec v2 Section 2.1)."""

    server_id: UUID = Field(default_factory=uuid4)
    name: str
    source_url: str
    transport: Transport
    install_cmd: str | None = None
    endpoint_url: str | None = None
    declared_tools: list[dict[str, Any]] = Field(default_factory=list)
    description: str = ""
    registry_name: str | None = None
    repository_url: str | None = None
    provider_keys: list[str] = Field(default_factory=list)

    auth_required: bool = False
    auth_type: AuthType = AuthType.NONE
    oauth_flow: OAuthFlow | None = None
    oauth_provider: str | None = None

    status: ServerStatus = ServerStatus.PENDING
    quality_score: int = 0
    confidence: Confidence = Confidence.LOW
    attempts_completed: int = 0
    attempts_planned: int = 1

    invocation_verified: bool = False
    latency_p50_ms: int | None = None
    latency_category: LatencyCategory | None = None

    last_verified_at: datetime | None = None
    ttl_expires_at: datetime | None = None
    verification_details: dict[str, Any] = Field(default_factory=dict)
    rejection_stage: str | None = None
    rejection_check: str | None = None
    first_seen_at: datetime | None = None
    last_attempt_at: datetime | None = None


class ProviderCredentialRecord(BaseModel):
    """Per-provider OAuth credential (spec v2 Section 2.2 / rule 7)."""

    provider: str
    client_id: str = ""
    redirect_uri: str = ""
    # Stored encrypted at rest by the caller; this model never logs them.
    refresh_token: str | None = None
    access_token: str | None = None
    granted_scopes: list[str] = Field(default_factory=list)
    requested_scopes: list[str] = Field(default_factory=list)
    token_expires_at: datetime | None = None
    status: str = "active"
    updated_at: datetime | None = None


class ColdMissQueryRecord(BaseModel):
    """One persisted cold-miss search term (Section 9.1)."""

    query_key: str
    term: str
    status: str = "queued"  # queued | checking_registry | checking_github | done
    registry_done: bool = False
    github_done: bool = False
    servers_found: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class VerificationDecisionRecord(BaseModel):
    """One scoring decision row for observability (Section 12)."""

    decision_id: UUID = Field(default_factory=uuid4)
    server_id: UUID
    transport: str
    stage: str | None = None
    check: str | None = None
    reason: str | None = None
    quality_score: int = 0
    confidence: str = "low"
    resulting_status: str
    decided_at: datetime | None = None
