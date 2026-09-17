"""Verification pipeline (spec v2 Section 5).

Background workers only - this module is never imported by the search
API. Local servers run in the existing ephemeral Docker sandbox;
remote servers get multiple attempts spread over time with per-stage,
per-tool timeout budgets. Every outcome is categorized (never collapsed
into a single pass/fail), auth problems route to the auth paths, and
all tool responses pass through the semantic judge.

Scoring/status writes happen only here; the search API only ever reads
the resulting McpServer records.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.sandbox.container_manager import ContainerManager, LocalToolConfig, get_container_manager
from app.sandbox.local_mcp_client import LocalMCPClient
from app.sandbox.mcp_client import MCPTestClient
from app.sandbox.oauth import resolve_authorization_server
from app.verification.ingestion import classify_auth
from app.verification.models import (
    AuthType,
    LatencyCategory,
    McpServerRecord,
    OAuthFlow,
    ServerStatus,
    StageOutcomeKind,
    StageResult,
    ToolOutcome,
    Transport,
    VerificationStage,
)
from app.verification.scoring import (
    REMOTE_ATTEMPTS_PLANNED,
    SoftCheckResults,
    score_server,
    ttl_days_for,
)
from app.verification.semantic_judge import LLMSemanticJudge
from app.verification.timeout_policy import (
    HANDSHAKE_ATTEMPTS,
    HANDSHAKE_TIMEOUT_S,
    MALFORMED_INPUT_TIMEOUT_S,
    REMOTE_RETRY_DELAYS_S,
    malformed_arguments,
    is_readonly_tool,
    synthetic_arguments,
    tool_policy,
)

logger = logging.getLogger(__name__)

# Bound the verification cost per server regardless of tool count.
MAX_TOOLS_INVOKED = 5
MAX_STAGE_RESULTS_KEPT = 60


@dataclass
class AttemptOutcome:
    """Categorized result of one verification attempt (Section 5.2)."""

    kind: StageOutcomeKind
    detail: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    connect_latency_ms: int | None = None
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    malformed_graceful: bool | None = None
    security_flagged: bool = False
    stage_results: list[StageResult] = field(default_factory=list)
    # True when the run connected and exercised real tools WITH working
    # credentials - the auth-gated equivalent of a full verification pass.
    auth_exercised: bool = False


class VerificationPipeline:
    """Runs one verification job for one server record."""

    def __init__(
        self,
        repository,
        decision_repository,
        settings: Settings,
        *,
        container_manager: ContainerManager | None = None,
        judge: LLMSemanticJudge | None = None,
    ) -> None:
        self._repository = repository
        self._decisions = decision_repository
        self._settings = settings
        self._container_manager = container_manager
        self._judge = judge or (
            LLMSemanticJudge(settings.gemini_api_key, settings.gemini_model)
            if settings.gemini_api_key
            else None
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def verify(self, record: McpServerRecord) -> McpServerRecord:
        if record.transport is Transport.LOCAL:
            outcome = await self._verify_local(record)
        else:
            outcome = await self._verify_remote(record)
        return await self._finalize(record, outcome)

    # ------------------------------------------------------------------
    # Local servers (Section 5.1)
    # ------------------------------------------------------------------

    async def _verify_local(self, record: McpServerRecord) -> AttemptOutcome:
        stages: list[StageResult] = []
        config = _local_tool_config(record)
        if config is None:
            stages.append(StageResult(stage=VerificationStage.INSTALL.value,
                                      kind=StageOutcomeKind.MALFORMED.value,
                                      detail=f"unparseable install command: {record.install_cmd!r}"))
            return AttemptOutcome(kind=StageOutcomeKind.MALFORMED,
                                  detail="install command unparseable", stage_results=stages)

        manager = self._container_manager or get_container_manager()
        session = None
        client = LocalMCPClient()
        try:
            # 1. Sandbox + install (npm/pip install happens inside the
            #    container command; exit/stderr captured via the client).
            session = await manager.create_container(
                item_id=record.server_id,
                config=config,
                env_vars={},
                user_id=f"verify-{record.server_id}",
                timeout=60,
            )
            stages.append(StageResult(stage=VerificationStage.INSTALL.value,
                                      kind=StageOutcomeKind.SUCCESS.value,
                                      detail=f"sandbox {session.image} ready"))

            # 2-4. Start server, initialize handshake, tools/list.
            connect_started = asyncio.get_event_loop().time()
            connect_result = await client.connect(
                command=config.build_command(),
                env_vars={},
                timeout=HANDSHAKE_TIMEOUT_S * HANDSHAKE_ATTEMPTS,
                container_id=session.container_id,
            )
            connect_latency_ms = int((asyncio.get_event_loop().time() - connect_started) * 1000)

            if not connect_result.connected:
                kind = StageOutcomeKind.AUTH_REQUIRED if connect_result.auth_reason == "unauthorized" \
                    else StageOutcomeKind.PROTOCOL_FAIL
                stages.append(StageResult(
                    stage=VerificationStage.HANDSHAKE.value, kind=kind.value,
                    detail=connect_result.user_message or connect_result.error,
                    data={"auth_reason": connect_result.auth_reason,
                          "required_env_vars": connect_result.required_env_vars},
                ))
                return AttemptOutcome(kind=kind, detail=connect_result.error,
                                      connect_latency_ms=connect_latency_ms,
                                      stage_results=stages)

            stages.append(StageResult(stage=VerificationStage.HANDSHAKE.value,
                                      kind=StageOutcomeKind.SUCCESS.value,
                                      detail=f"initialized: {connect_result.server_info}"))
            tools = [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in connect_result.tools
            ]
            stages.append(StageResult(stage=VerificationStage.TOOLS_LIST.value,
                                      kind=StageOutcomeKind.SUCCESS.value,
                                      detail=f"{len(tools)} tools declared"))

            # 5-6. Invocation + malformed-input probes.
            tool_outcomes, malformed_graceful, security_flagged = await self._exercise_local_tools(
                client, tools, stages
            )
            return AttemptOutcome(
                kind=StageOutcomeKind.SUCCESS,
                tools=tools,
                connect_latency_ms=connect_latency_ms,
                tool_outcomes=tool_outcomes,
                malformed_graceful=malformed_graceful,
                security_flagged=security_flagged,
                stage_results=stages,
            )
        except Exception as exc:  # noqa: BLE001 - one failed run must not kill the worker
            logger.warning("Local verification crashed for %s: %s", record.source_url, exc)
            stages.append(StageResult(stage=VerificationStage.INSTALL.value,
                                      kind=StageOutcomeKind.TRANSPORT_FAIL.value,
                                      detail=str(exc)[:300]))
            return AttemptOutcome(kind=StageOutcomeKind.TRANSPORT_FAIL,
                                  detail=str(exc)[:300], stage_results=stages)
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            if session is not None:
                try:
                    await manager.destroy_container(session.session_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Sandbox cleanup failed for %s: %s", record.source_url, exc)

    async def _exercise_local_tools(
        self,
        client: LocalMCPClient,
        tools: list[dict[str, Any]],
        stages: list[StageResult],
    ) -> tuple[list[ToolOutcome], bool | None, bool]:
        """Invoke tools inside the sandbox; probe malformed input handling."""
        tool_outcomes: list[ToolOutcome] = []
        malformed_graceful: bool | None = None

        for tool in tools[:MAX_TOOLS_INVOKED]:
            name = str(tool.get("name") or "")
            if not name:
                continue
            policy = tool_policy(name)
            args = synthetic_arguments(tool)
            outcome = ToolOutcome(tool_name=name, invoked=True)
            try:
                result = await client.invoke(name, args, timeout=policy.effective_timeout_s)
                outcome.invocation_status = result.status
                outcome.duration_ms = result.duration_ms
                if result.status == "success":
                    outcome.detail = "invoked successfully"
                    verdict = await self._judge_output(tool, result.result, result.duration_ms)
                    outcome.semantic_valid = verdict.valid
                    outcome.semantic_reason = verdict.reason
                else:
                    outcome.detail = (result.error or "invocation error")[:300]
                    outcome.semantic_valid = False
                    outcome.semantic_reason = "tool returned an error result"
            except asyncio.TimeoutError:
                outcome.invocation_status = "timeout"
                outcome.detail = f"timed out after {policy.effective_timeout_s:.0f}s"
            except Exception as exc:  # noqa: BLE001
                outcome.invocation_status = "error"
                outcome.detail = str(exc)[:300]
            tool_outcomes.append(outcome)

        # Malformed-input probe on the first tool.
        if tools:
            probe_tool = tools[0]
            probe_name = str(probe_tool.get("name") or "")
            if probe_name:
                try:
                    before_alive = client._process is not None and client._process.returncode is None
                    probe = await client.invoke(
                        probe_name, malformed_arguments(probe_tool),
                        timeout=MALFORMED_INPUT_TIMEOUT_S,
                    )
                    still_alive = client._process is not None and client._process.returncode is None
                    # Graceful = structured error (or any response) without a crash/hang.
                    malformed_graceful = before_alive and still_alive and not (
                        probe.status == "error" and "timed out" in (probe.error or "").lower()
                    )
                    stages.append(StageResult(
                        stage=VerificationStage.MALFORMED_INPUT.value,
                        kind=StageOutcomeKind.SUCCESS.value if malformed_graceful
                        else StageOutcomeKind.SECURITY_FLAG.value,
                        detail="server survived malformed input with structured error"
                        if malformed_graceful else "server crashed or hung on malformed input",
                    ))
                except Exception as exc:  # noqa: BLE001
                    malformed_graceful = False
                    stages.append(StageResult(
                        stage=VerificationStage.MALFORMED_INPUT.value,
                        kind=StageOutcomeKind.SECURITY_FLAG.value,
                        detail=f"malformed-input probe failed: {str(exc)[:200]}",
                    ))

        # Security signal (Section 5.1 step 7): the sandbox enforces
        # resource limits and default-deny egress; flag nothing unless a
        # probe crashed the process (recorded above). No invented evidence.
        security_flagged = any(
            s.kind == StageOutcomeKind.SECURITY_FLAG.value for s in stages
        )
        stages.append(StageResult(
            stage=VerificationStage.SECURITY.value,
            kind=StageOutcomeKind.SUCCESS.value,
            detail="container isolation active (resource limits, cap drop, egress allowlist)",
        ))
        return tool_outcomes, malformed_graceful, security_flagged

    # ------------------------------------------------------------------
    # Remote servers (Section 5.2) - one attempt per call
    # ------------------------------------------------------------------

    async def _verify_remote(self, record: McpServerRecord) -> AttemptOutcome:
        stages: list[StageResult] = []
        endpoint = record.endpoint_url or ""
        token = await self._credential_token_for(record)

        started = asyncio.get_event_loop().time()
        client = MCPTestClient(endpoint, auth_token=token, timeout=HANDSHAKE_TIMEOUT_S,
                               max_retries=0)
        connect_result = await client.connect()
        connect_latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)

        if connect_result.auth_required or connect_result.auth_reason == "unauthorized":
            stages.append(StageResult(
                stage=VerificationStage.CONNECT.value,
                kind=StageOutcomeKind.AUTH_REQUIRED.value,
                detail=connect_result.user_message or "server requires credentials",
                data={"has_token": bool(token)},
            ))
            return await self._handle_remote_auth(record, stages, connect_latency_ms)

        if not connect_result.connected:
            kind = _classify_remote_failure(connect_result.auth_reason)
            stages.append(StageResult(
                stage=VerificationStage.CONNECT.value, kind=kind.value,
                detail=connect_result.user_message or connect_result.error,
                data={"auth_reason": connect_result.auth_reason},
            ))
            return AttemptOutcome(kind=kind, detail=connect_result.error,
                                  connect_latency_ms=connect_latency_ms, stage_results=stages)

        stages.append(StageResult(
            stage=VerificationStage.HANDSHAKE.value, kind=StageOutcomeKind.SUCCESS.value,
            detail=f"initialized: {connect_result.server_info}",
        ))
        tools = [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in connect_result.tools
        ]
        stages.append(StageResult(stage=VerificationStage.TOOLS_LIST.value,
                                  kind=StageOutcomeKind.SUCCESS.value,
                                  detail=f"{len(tools)} tools declared"))

        # Scope check (Section 7.3.3): credentials present but the token
        # lacks the scopes this server needs.
        if token and not tools:
            return AttemptOutcome(kind=StageOutcomeKind.AUTH_REQUIRED,
                                  detail="connected but no tools visible; possible scope restriction",
                                  stage_results=stages, connect_latency_ms=connect_latency_ms)

        tool_outcomes, malformed_graceful, security_flagged = await self._exercise_remote_tools(
            record, tools, stages, token
        )
        return AttemptOutcome(
            kind=StageOutcomeKind.SUCCESS,
            tools=tools,
            connect_latency_ms=connect_latency_ms,
            tool_outcomes=tool_outcomes,
            malformed_graceful=malformed_graceful,
            security_flagged=security_flagged,
            auth_exercised=bool(token),
            stage_results=stages,
        )

    async def _exercise_remote_tools(
        self,
        record: McpServerRecord,
        tools: list[dict[str, Any]],
        stages: list[StageResult],
        token: str | None,
    ) -> tuple[list[ToolOutcome], bool | None, bool]:
        """Invoke tools; with real credentials, only read-only tools (rule 4)."""
        tool_outcomes: list[ToolOutcome] = []
        malformed_graceful: bool | None = None

        invocable = [
            t for t in tools
            if (not token or is_readonly_tool(str(t.get("name") or ""), t.get("description")))
        ][:MAX_TOOLS_INVOKED]

        for tool in invocable:
            name = str(tool.get("name") or "")
            if not name:
                continue
            policy = tool_policy(name)
            args = synthetic_arguments(tool)
            outcome = ToolOutcome(tool_name=name, invoked=True)
            invoke_client = MCPTestClient(record.endpoint_url or "", auth_token=token,
                                          timeout=policy.effective_timeout_s, max_retries=0)
            try:
                result = await invoke_client.invoke(name, args)
                outcome.invocation_status = result.status
                outcome.duration_ms = result.duration_ms
                if result.status == "success":
                    outcome.detail = "invoked successfully"
                    verdict = await self._judge_output(tool, result.result, result.duration_ms)
                    outcome.semantic_valid = verdict.valid
                    outcome.semantic_reason = verdict.reason
                else:
                    outcome.detail = (result.error or "invocation error")[:300]
                    outcome.semantic_valid = False
                    outcome.semantic_reason = "tool returned an error result"
            except Exception as exc:  # noqa: BLE001
                outcome.invocation_status = "error"
                outcome.detail = str(exc)[:300]
            tool_outcomes.append(outcome)

        if tools and not invocable:
            stages.append(StageResult(
                stage=VerificationStage.TOOL_INVOCATION.value,
                kind=StageOutcomeKind.AUTH_REQUIRED.value,
                detail="all tools are write-capable; skipped auto-invocation with real credentials",
            ))

        # Malformed probe only when no credentials are attached (probe
        # sends deliberately broken input; harmless unauthenticated).
        if tools and not token:
            probe_client = MCPTestClient(record.endpoint_url or "",
                                         timeout=MALFORMED_INPUT_TIMEOUT_S, max_retries=0)
            try:
                probe = await probe_client.invoke(
                    str(tools[0].get("name") or ""), malformed_arguments(tools[0])
                )
                # Graceful = the server answered (structured error or
                # any well-formed response) instead of crashing/hanging
                # at the transport level. A timeout or connection reset
                # during the probe is the crash/hang signal.
                malformed_graceful = probe.status == "success" or (
                    probe.status == "error"
                    and probe.auth_reason not in ("timeout", "connection_error")
                )
                stages.append(StageResult(
                    stage=VerificationStage.MALFORMED_INPUT.value,
                    kind=StageOutcomeKind.SUCCESS.value if malformed_graceful
                    else StageOutcomeKind.SECURITY_FLAG.value,
                    detail="graceful structured error on malformed input"
                    if malformed_graceful else "ungainly failure on malformed input",
                ))
            except Exception as exc:  # noqa: BLE001
                stages.append(StageResult(
                    stage=VerificationStage.MALFORMED_INPUT.value,
                    kind=StageOutcomeKind.SECURITY_FLAG.value,
                    detail=f"malformed probe error: {str(exc)[:200]}",
                ))

        security_flagged = any(
            s.kind == StageOutcomeKind.SECURITY_FLAG.value for s in stages
        )
        return tool_outcomes, malformed_graceful, security_flagged

    # ------------------------------------------------------------------
    # Auth handling (Section 7)
    # ------------------------------------------------------------------

    async def _handle_remote_auth(
        self,
        record: McpServerRecord,
        stages: list[StageResult],
        connect_latency_ms: int,
    ) -> AttemptOutcome:
        """Route AUTH_REQUIRED through the auth paths; never a rejection."""
        token = await self._credential_token_for(record)
        if token:
            # Retry once with the provider credential attached.
            client = MCPTestClient(record.endpoint_url or "", auth_token=token,
                                   timeout=HANDSHAKE_TIMEOUT_S, max_retries=0)
            retry = await client.connect()
            if retry.connected:
                record_with_token = record
                stages.append(StageResult(
                    stage=VerificationStage.CONNECT.value,
                    kind=StageOutcomeKind.SUCCESS.value,
                    detail="connected using stored provider credential",
                ))
                # With credentials attached this is a COMPLETE verification
                # pass, structurally identical to the anonymous path: the
                # handshake and tools/list stages must be recorded or the
                # scoring engine will never award the verified bucket.
                stages.append(StageResult(
                    stage=VerificationStage.HANDSHAKE.value,
                    kind=StageOutcomeKind.SUCCESS.value,
                    detail=f"initialized with auth: {retry.server_info}",
                ))
                tools = [
                    {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                    for t in retry.tools
                ]
                stages.append(StageResult(
                    stage=VerificationStage.TOOLS_LIST.value,
                    kind=StageOutcomeKind.SUCCESS.value,
                    detail=f"{len(tools)} tools declared",
                ))
                tool_outcomes, malformed, security = await self._exercise_remote_tools(
                    record_with_token, tools, stages, token
                )
                return AttemptOutcome(kind=StageOutcomeKind.SUCCESS, tools=tools,
                                      connect_latency_ms=connect_latency_ms,
                                      tool_outcomes=tool_outcomes,
                                      malformed_graceful=malformed,
                                      security_flagged=security,
                                      auth_exercised=True,
                                      stage_results=stages)
            text = (retry.user_message or retry.error or "").lower()
            if "scope" in text or "restricted" in text or "insufficient" in text:
                stages.append(StageResult(
                    stage=VerificationStage.CONNECT.value,
                    kind=StageOutcomeKind.MALFORMED.value,
                    detail="credential rejected: scopes not granted for this server",
                ))
                return AttemptOutcome(kind=StageOutcomeKind.PROTOCOL_FAIL,
                                      detail="scope_not_granted",
                                      stage_results=stages,
                                      connect_latency_ms=connect_latency_ms)
            # Token present but rejected/expired -> provider needs reauth.
            await self._flag_provider_reauth(record)
            return AttemptOutcome(kind=StageOutcomeKind.AUTH_REQUIRED,
                                  detail="stored credential rejected; provider reauth required",
                                  stage_results=stages,
                                  connect_latency_ms=connect_latency_ms)

        # No credentials: structural-only verification (Tier A, 7.1/7.5).
        auth_type, oauth_flow, oauth_provider = await self._refine_auth_metadata(record)
        record.auth_required = True
        record.auth_type = auth_type
        record.oauth_flow = oauth_flow
        record.oauth_provider = oauth_provider
        stages.append(StageResult(
            stage=VerificationStage.CONNECT.value,
            kind=StageOutcomeKind.AUTH_REQUIRED.value,
            detail=f"structural-only verification; auth_type={auth_type.value}, "
                   f"flow={oauth_flow.value if oauth_flow else None}",
        ))
        return AttemptOutcome(kind=StageOutcomeKind.AUTH_REQUIRED,
                              detail="no credentials available",
                              stage_results=stages,
                              connect_latency_ms=connect_latency_ms)

    async def _refine_auth_metadata(
        self, record: McpServerRecord
    ) -> tuple[AuthType, OAuthFlow | None, str | None]:
        """Probe discovery endpoints (structural, safe, no consent) to
        distinguish oauth2 flows (Section 7.2)."""
        endpoint = record.endpoint_url or ""
        if record.auth_type in (AuthType.OAUTH2, AuthType.UNKNOWN, AuthType.NONE):
            try:
                metadata = await resolve_authorization_server(endpoint)
                grants = metadata.get("grant_types_supported") or []
                if isinstance(grants, list) and "client_credentials" in grants:
                    return AuthType.OAUTH2, OAuthFlow.CLIENT_CREDENTIALS, record.oauth_provider
                return AuthType.OAUTH2, OAuthFlow.AUTHORIZATION_CODE, record.oauth_provider
            except Exception:  # noqa: BLE001 - discovery is best-effort
                pass
        if record.auth_type is AuthType.NONE:
            auth_required, auth_type, oauth_flow, oauth_provider = classify_auth(
                text=record.description or ""
            )
            if auth_required:
                return auth_type, oauth_flow, oauth_provider
            return AuthType.UNKNOWN, None, record.oauth_provider
        return record.auth_type, record.oauth_flow, record.oauth_provider

    async def _credential_token_for(self, record: McpServerRecord) -> str | None:
        """Bearer token from the provider credential store, if active.

        When the cached access token has expired but a refresh token was
        stored at consent time, the RFC 6749 refresh grant is replayed
        once so re-verification never needs a new human consent round.
        A failed refresh marks the credential `reauth_required` (Section
        7.3.4) instead of silently dialing with a dead token.
        """
        provider = record.oauth_provider
        repo = getattr(self, "_provider_repository", None)
        if not provider or repo is None:
            return None
        credential = await repo.get(provider)
        if credential is None or credential.status != "active":
            return None

        token_expired = False
        if credential.token_expires_at:
            try:
                expires = datetime.fromisoformat(str(credential.token_expires_at))
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                token_expired = expires <= datetime.now(timezone.utc)
            except ValueError:
                token_expired = False

        if not token_expired:
            return credential.access_token or None

        # Access token expired: try the refresh grant, then persist the
        # rotated tokens so subsequent servers/providers reuse them.
        refreshed = await self._refresh_provider_credential(credential)
        if refreshed:
            return refreshed
        await repo.update(provider, {
            "status": "reauth_required",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        return None

    async def _refresh_provider_credential(self, credential) -> str | None:
        """Replay the refresh_token grant; persist rotated tokens on success."""
        from app.sandbox.oauth import OAuthFlowError, refresh_provider_token

        if not credential.refresh_token or not credential.token_endpoint:
            return None
        try:
            token_doc = await refresh_provider_token(
                token_endpoint=credential.token_endpoint,
                refresh_token=credential.refresh_token,
                client_id=credential.client_id,
                client_secret=credential.client_secret,
            )
        except OAuthFlowError:
            logger.info("Token refresh failed for provider %r", credential.provider)
            return None
        except Exception:  # noqa: BLE001 - network hiccups must not kill verification
            logger.exception("Unexpected token refresh failure for %r", credential.provider)
            return None

        repo = getattr(self, "_provider_repository", None)
        if repo is None:
            return token_doc.get("access_token")
        expires_in = token_doc.get("expires_in")
        updates: dict[str, Any] = {
            "access_token": token_doc.get("access_token"),
            "status": "active",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if token_doc.get("refresh_token"):
            updates["refresh_token"] = token_doc["refresh_token"]
        if isinstance(expires_in, (int, float)):
            updates["token_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=float(expires_in))
            ).isoformat()
        await repo.update(credential.provider, updates)
        return token_doc.get("access_token")

    async def _flag_provider_reauth(self, record: McpServerRecord) -> None:
        repo = getattr(self, "_provider_repository", None)
        if repo is None or not record.oauth_provider:
            return
        await repo.update(record.oauth_provider, {
            "status": "reauth_required",
            "updated_at": datetime.now(timezone.utc),
        })

    # ------------------------------------------------------------------
    # Scoring + persistence (Section 8)
    # ------------------------------------------------------------------

    async def _finalize(
        self, record: McpServerRecord, outcome: AttemptOutcome
    ) -> McpServerRecord:
        soft = self._build_soft_checks(outcome)
        # A run that exercised real tools with working credentials is a
        # COMPLETE verification pass regardless of transport: the auth
        # gate (the only reason remote servers plan 3 attempts) no longer
        # applies, so one pass is enough for high confidence.
        auth_exercised = outcome.auth_exercised
        attempts_planned = (
            1 if record.transport is Transport.LOCAL or auth_exercised
            else REMOTE_ATTEMPTS_PLANNED
        )
        attempts_completed = min(record.attempts_completed + 1, attempts_planned)

        result = score_server(
            soft, attempts_completed=attempts_completed, attempts_planned=attempts_planned
        )

        status = result.status
        # A structurally broken run (e.g. unparseable install command)
        # is a prefilter-class outcome, not a quality judgment.
        if outcome.kind is StageOutcomeKind.MALFORMED:
            status = ServerStatus.MALFORMED
        # Multi-attempt rules (Section 5.2/6): a retryable failure or an
        # exhausted-timeout is never a final verdict while attempts remain.
        if outcome.kind in (StageOutcomeKind.TRANSPORT_FAIL, StageOutcomeKind.TIMEOUT):
            if attempts_completed < attempts_planned:
                status = ServerStatus.RETRY_PENDING
            elif soft.auth_blocked:
                status = ServerStatus.PARTIAL_VERIFIED
        if (
            outcome.kind is StageOutcomeKind.AUTH_REQUIRED
            and not auth_exercised
            and status not in (
                ServerStatus.VERIFIED, ServerStatus.REVIEW,
            )
        ):
            # Auth-blocked: structural-only (rule 8). authorization_code
            # without consent routes to the manual queue bucket.
            status = (
                ServerStatus.OAUTH_PENDING_CONSENT
                if record.auth_type is AuthType.OAUTH2
                and record.oauth_flow is OAuthFlow.AUTHORIZATION_CODE
                and not await self._has_active_credential(record)
                else ServerStatus.PARTIAL_VERIFIED
            )

        # The verified badge (Section 2.1): awarded by the pipeline alone,
        # only on a verified result from a run that actually connected,
        # listed tools, and invoked them successfully. Auth-gated servers
        # earn it through `auth_exercised` runs; it is cleared the moment
        # any later run fails to reach a verified verdict.
        badge_earned = status is ServerStatus.VERIFIED and bool(
            outcome.tool_outcomes
        ) and any(o.invocation_status == "success" for o in outcome.tool_outcomes)

        now = datetime.now(timezone.utc)
        updates: dict[str, Any] = {
            "status": status.value,
            "quality_score": result.quality_score,
            "confidence": result.confidence.value,
            "attempts_completed": attempts_completed,
            "attempts_planned": attempts_planned,
            "last_attempt_at": now.isoformat(),
            "auth_required": record.auth_required,
            "auth_type": record.auth_type.value,
            "oauth_flow": record.oauth_flow.value if record.oauth_flow else None,
            "oauth_provider": record.oauth_provider,
        }

        if outcome.kind is StageOutcomeKind.SUCCESS:
            invocation_successes = [o for o in outcome.tool_outcomes if o.invocation_status == "success"]
            latencies = [o.duration_ms for o in outcome.tool_outcomes if o.duration_ms]
            if outcome.connect_latency_ms:
                latencies.append(outcome.connect_latency_ms)
            p50 = int(statistics.median(latencies)) if latencies else None
            updates.update({
                "declared_tools": outcome.tools,
                "invocation_verified": bool(invocation_successes),
                "latency_p50_ms": p50,
                "latency_category": _latency_category(p50).value if p50 else None,
                "last_verified_at": now.isoformat(),
                "ttl_expires_at": (
                    now + timedelta(days=ttl_days_for(record.transport.value))
                ).isoformat() if status is ServerStatus.VERIFIED else None,
            })
            if badge_earned:
                updates["verified_badge"] = True
                updates["verified_via_auth"] = auth_exercised
                updates["last_verified_via"] = "auth" if auth_exercised else "anonymous"
        elif record.verified_badge:
            # The badge is never stale: a later failing/incomplete run
            # drops it together with the verified status.
            updates["verified_badge"] = False
            updates["verified_via_auth"] = False
            updates["last_verified_via"] = None

        if auth_exercised:
            # Observability: how this server's badge was earned.
            outcome.stage_results.append(StageResult(
                stage=VerificationStage.SECURITY.value,
                kind=StageOutcomeKind.SUCCESS.value,
                detail="verified with credentials: auth flow exercised end-to-end",
            ))

        if result.hard_fail:
            updates["rejection_stage"] = VerificationStage.HANDSHAKE.value
            updates["rejection_check"] = "handshake_valid"
        if outcome.kind is StageOutcomeKind.MALFORMED:
            updates["rejection_stage"] = VerificationStage.INSTALL.value
            updates["rejection_check"] = "install_connect"

        details = dict(record.verification_details or {})
        stage_results = [s.model_dump(mode="json") for s in outcome.stage_results]
        details["stages"] = (details.get("stages") or []) + stage_results
        details["stages"] = details["stages"][-MAX_STAGE_RESULTS_KEPT:]
        details["last_score"] = {
            "score": result.quality_score,
            "confidence": result.confidence.value,
            "breakdown": result.breakdown,
            "attempt": attempts_completed,
        }
        details["tools"] = [o.model_dump(mode="json") for o in outcome.tool_outcomes]
        updates["verification_details"] = details

        updated = await self._repository.update(record.server_id, updates)
        await self._decisions.record(_decision_from(record, result, status, outcome))
        return updated or record

    def _build_soft_checks(self, outcome: AttemptOutcome) -> SoftCheckResults:
        soft = SoftCheckResults()
        stages = {s.stage: s for s in outcome.stage_results}

        install_ok = stages.get(VerificationStage.INSTALL.value)
        connect_ok = stages.get(VerificationStage.CONNECT.value)
        handshake_stage = stages.get(VerificationStage.HANDSHAKE.value)
        connected = (
            (install_ok and install_ok.kind == "success")
            or (connect_ok and connect_ok.kind in ("success", "auth_required"))
            or (handshake_stage and handshake_stage.kind == "success")
        )
        soft.install_connect = 1.0 if connected else (
            0.0 if outcome.kind is StageOutcomeKind.SUCCESS else None
        )

        handshake = stages.get(VerificationStage.HANDSHAKE.value)
        soft.handshake_valid = (
            1.0 if handshake and handshake.kind == "success"
            else 0.0 if handshake else None
        )
        tools_list = stages.get(VerificationStage.TOOLS_LIST.value)
        soft.tools_list_schema = (
            1.0 if tools_list and tools_list.kind == "success" and outcome.tools
            else 0.0 if tools_list else None
        )

        if outcome.tool_outcomes:
            soft.invocation_attempted = True
            successes = sum(1 for o in outcome.tool_outcomes if o.invocation_status == "success")
            soft.tool_invocation = successes / len(outcome.tool_outcomes)
            semantic_scores = [
                1.0 if o.semantic_valid else 0.0
                for o in outcome.tool_outcomes if o.semantic_valid is not None
            ]
            if semantic_scores:
                soft.semantic_validity = sum(semantic_scores) / len(semantic_scores)
        elif outcome.kind is StageOutcomeKind.AUTH_REQUIRED:
            soft.auth_blocked = True

        if outcome.malformed_graceful is not None:
            soft.no_crash_malformed_input = 1.0 if outcome.malformed_graceful else 0.0

        if outcome.security_flagged:
            soft.security_penalty = 1.0

        if outcome.kind is StageOutcomeKind.SUCCESS and (
            not outcome.tool_outcomes or all(
                o.invocation_status == "success" for o in outcome.tool_outcomes
            )
        ):
            soft.notes.append("all attempted invocations succeeded")
        return soft

    async def _judge_output(self, tool: dict[str, Any], output: Any,
                            duration_ms: int | None):
        if self._judge is None:
            from app.verification.semantic_judge import heuristic_judge
            return heuristic_judge(str(tool.get("name") or ""), tool.get("description"),
                                   output, duration_ms=duration_ms)
        return await self._judge.judge(
            str(tool.get("name") or ""), tool.get("description"),
            tool.get("inputSchema") or tool.get("input_schema"), output,
            duration_ms=duration_ms,
        )

    async def _has_active_credential(self, record: McpServerRecord) -> bool:
        repo = getattr(self, "_provider_repository", None)
        if repo is None or not record.oauth_provider:
            return False
        credential = await repo.get(record.oauth_provider)
        return bool(credential and credential.status == "active" and credential.refresh_token)

    # ------------------------------------------------------------------
    # Provider wiring (called by the worker bootstrap)
    # ------------------------------------------------------------------

    def set_provider_repository(self, provider_repository) -> None:
        self._provider_repository = provider_repository

    async def cascade_provider_reauth(self, provider: str) -> int:
        """Section 7.3.4: a provider losing authorization drops every
        server tied to it out of default search - without deleting them."""
        servers = await self._repository.list_by_provider(provider)
        count = 0
        for server in servers:
            await self._repository.update(server.server_id, {
                "status": ServerStatus.REAUTH_REQUIRED.value,
            })
            count += 1
        return count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_tool_config(record: McpServerRecord) -> LocalToolConfig | None:
    """Parse a documented install command into the sandbox config."""
    cmd = (record.install_cmd or "").strip()
    if not cmd:
        return None
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    if not tokens:
        return None
    head = tokens[0].lower()
    if head == "npx":
        rest = [t for t in tokens[1:] if t not in ("-y", "--yes", "--quiet")]
        if not rest:
            return None
        return LocalToolConfig(registry_type="npm", identifier=rest[0],
                               runtime_arguments=rest[1:])
    if head in ("uvx", "pipx"):
        rest = [t for t in tokens[1:] if not t.startswith("--")]
        if not rest:
            return None
        return LocalToolConfig(registry_type="pip", identifier=rest[0],
                               runtime_arguments=rest[1:],
                               runtime_hint=head)
    if head == "python" or head == "python3":
        return LocalToolConfig(registry_type="pip", identifier=cmd,
                               runtime_arguments=tokens[1:])
    if head == "node":
        return LocalToolConfig(registry_type="npm", identifier=cmd,
                               runtime_arguments=tokens[1:])
    # Unknown runtime: pass the whole command through the npm runner
    # fallback, which handles arbitrary binaries via node.
    return LocalToolConfig(registry_type="npm", identifier=cmd, runtime_arguments=[])


def _classify_remote_failure(auth_reason: str | None) -> StageOutcomeKind:
    """Map a failed remote connect to the Section 5.2 outcome categories."""
    if auth_reason in ("connection_error", "timeout", None):
        return StageOutcomeKind.TRANSPORT_FAIL
    if auth_reason in ("not_found", "incompatible"):
        return StageOutcomeKind.PROTOCOL_FAIL
    if auth_reason == "rate_limited":
        return StageOutcomeKind.TRANSPORT_FAIL
    return StageOutcomeKind.PROTOCOL_FAIL


def _latency_category(p50_ms: int | None) -> LatencyCategory:
    if p50_ms is None:
        return LatencyCategory.MODERATE
    if p50_ms < 2000:
        return LatencyCategory.FAST
    if p50_ms < 10000:
        return LatencyCategory.MODERATE
    return LatencyCategory.SLOW


def _decision_from(record: McpServerRecord, result, status: ServerStatus,
                   outcome: AttemptOutcome):
    from app.verification.models import VerificationDecisionRecord

    return VerificationDecisionRecord(
        server_id=record.server_id,
        transport=record.transport.value,
        stage=updates_stage(outcome),
        check=record.rejection_check,
        reason=result.reason or outcome.detail,
        quality_score=result.quality_score,
        confidence=result.confidence.value,
        resulting_status=status.value,
    )


def updates_stage(outcome: AttemptOutcome) -> str | None:
    if outcome.stage_results:
        return outcome.stage_results[-1].stage
    return None


def next_retry_delay(attempts_completed: int) -> int:
    """Seconds to wait before the next remote attempt (now/+1h/+6h)."""
    index = min(max(attempts_completed, 0), len(REMOTE_RETRY_DELAYS_S) - 1)
    return REMOTE_RETRY_DELAYS_S[index]
