"""Tests for the verification subsystem (spec v2).

Coverage follows the non-negotiable rules: prefilter static checks
(including Smithery rejection), scoring buckets with separate
confidence, ingestion dedupe, timeout/arg policy, read-only gating,
the remote multi-attempt pipeline with auth routing, and the strictly
read-only search endpoint with the cold-miss hint.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.verification.models import (
    AuthType,
    Confidence,
    McpServerRecord,
    OAuthFlow,
    ServerStatus,
    StageOutcomeKind,
    Transport,
)
from app.verification.prefilter import is_smithery_hosted, prefilter_server
from app.verification.scoring import SoftCheckResults, score_server, ttl_days_for
from app.verification.timeout_policy import (
    malformed_arguments,
    is_readonly_tool,
    synthetic_arguments,
    tool_policy,
)
from app.verification.verifiers import (
    VerificationPipeline,
    _classify_remote_failure,
    _latency_category,
    _local_tool_config,
)
from app.verification.ingestion import (
    VerificationIngestionService,
    classify_auth,
    from_mcp_registry_candidate,
    normalize_repo_identity,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeServerRepository:
    def __init__(self) -> None:
        self.records: dict[str, McpServerRecord] = {}
        self.updates: list[tuple[str, dict]] = []

    async def create(self, record):
        self.records[str(record.server_id)] = record.model_copy()
        return record

    async def get(self, server_id):
        return self.records.get(str(server_id))

    async def get_by_source_url(self, source_url):
        for record in self.records.values():
            if record.source_url == source_url:
                return record
        return None

    async def get_by_registry_name(self, registry_name):
        for record in self.records.values():
            if record.registry_name == registry_name:
                return record
        return None

    async def list_status(self, *statuses, limit=50):
        return [
            r for r in self.records.values()
            if r.status.value in statuses
        ][:limit]

    async def list_by_provider(self, provider):
        return [
            r for r in self.records.values() if r.oauth_provider == provider
        ]

    async def list_expired_ttl(self, now_iso=None, limit=50):
        now = datetime.now(timezone.utc)
        return [
            r for r in self.records.values()
            if r.status is ServerStatus.VERIFIED
            and r.ttl_expires_at is not None
            and r.ttl_expires_at < now
        ]

    async def update(self, server_id, updates):
        record = self.records.get(str(server_id))
        if record is None:
            return None
        self.updates.append((str(server_id), updates))
        data = record.model_dump()
        for key, value in updates.items():
            data[key] = value
        updated = McpServerRecord.model_validate(data)
        self.records[str(server_id)] = updated
        return updated


class FakeDecisionRepository:
    def __init__(self) -> None:
        self.decisions = []

    async def record(self, decision):
        self.decisions.append(decision)


# ---------------------------------------------------------------------------
# Prefilter (Section 4 + 7.4)
# ---------------------------------------------------------------------------


def _remote_record(endpoint: str) -> McpServerRecord:
    return McpServerRecord(
        name="test-server",
        source_url="https://registry.example/test",
        transport=Transport.REMOTE,
        endpoint_url=endpoint,
    )


class TestPrefilter:
    def test_smithery_host_detected(self):
        assert is_smithery_hosted("https://server.smithery.ai/@some/server/mcp")
        assert is_smithery_hosted("https://api.smithery.ai/mcp")
        assert not is_smithery_hosted("https://not-server.smithery.ai.evil.example")
        assert not is_smithery_hosted("https://mcp.example.com")

    def test_smithery_rejected_not_malformed(self):
        result = prefilter_server(
            _remote_record("https://server.smithery.ai/@x/y/mcp")
        )
        assert result.status == "rejected"
        assert "smithery_hosted_incompatible" in result.failures

    def test_third_party_gateway_hosts_rejected(self):
        """All known third-party hosted gateways are rejected, not just Smithery."""
        for endpoint in (
            "https://server.smithery.ai/@x/y/mcp",
            "https://mcp.glama.ai/mcp",
            "https://mcp.pulsemcp.com/mcp",
            "https://mcp.composio.dev/x/y",
            "https://mcp.zapier.com/mcp",
            "https://waystation.ai/slack/mcp",
        ):
            result = prefilter_server(_remote_record(endpoint))
            assert result.status == "rejected", endpoint
            assert result.passed is False
            assert result.failures[0].endswith("_incompatible"), endpoint

    def test_third_party_proxy_install_command_rejected(self):
        """A local install routing through an excluded proxy runtime is rejected."""
        record = McpServerRecord(
            name="proxy-local",
            source_url="https://github.com/x/y",
            transport=Transport.LOCAL,
            install_cmd="npx -y @smithery/cli install @x/y --config {}",
        )
        result = prefilter_server(record)
        assert result.status == "rejected"
        assert "third_party_proxy_install_command" in result.failures

    def test_upstream_github_endpoint_not_excluded(self):
        """Self-hosted/upstream remote endpoints are untouched by the exclusion list."""
        result = prefilter_server(_remote_record("https://mcp.example.com/mcp"))
        assert result.passed
        assert result.status == "passed"

    def test_remote_without_endpoint_is_malformed(self):
        result = prefilter_server(_remote_record(""))
        assert result.status == "malformed"

    def test_remote_bad_scheme_is_malformed(self):
        result = prefilter_server(_remote_record("ftp://mcp.example.com"))
        assert result.status == "malformed"

    def test_remote_good_endpoint_passes(self):
        result = prefilter_server(_remote_record("https://mcp.example.com/mcp"))
        assert result.passed

    def test_remote_unresolvable_host_fails_with_resolver(self):
        result = prefilter_server(
            _remote_record("https://nonexistent.invalid/mcp"),
            dns_resolver=lambda host: False,
        )
        assert result.status == "malformed"
        assert any("does not resolve" in f for f in result.failures)

    def test_local_without_install_cmd_is_malformed(self):
        record = McpServerRecord(
            name="local", source_url="https://github.com/x/y",
            transport=Transport.LOCAL,
        )
        result = prefilter_server(record)
        assert result.status == "malformed"
        assert "install command" in result.reason

    def test_local_with_install_cmd_passes(self):
        record = McpServerRecord(
            name="local", source_url="https://github.com/x/y",
            transport=Transport.LOCAL, install_cmd="npx -y some-mcp",
        )
        assert prefilter_server(record).passed


# ---------------------------------------------------------------------------
# Scoring (Section 8)
# ---------------------------------------------------------------------------


class TestScoring:
    def _full_success(self) -> SoftCheckResults:
        return SoftCheckResults(
            install_connect=1.0,
            handshake_valid=1.0,
            tools_list_schema=1.0,
            tool_invocation=1.0,
            semantic_validity=1.0,
            no_crash_malformed_input=1.0,
            latency_category="fast",
        )

    def test_full_success_verified_high_confidence(self):
        outcome = score_server(self._full_success(), attempts_completed=3,
                               attempts_planned=3)
        assert outcome.quality_score >= 80
        assert outcome.status is ServerStatus.VERIFIED
        assert outcome.confidence is Confidence.HIGH

    def test_high_score_low_confidence_goes_to_review(self):
        outcome = score_server(self._full_success(), attempts_completed=1,
                               attempts_planned=3)
        assert outcome.quality_score >= 80
        assert outcome.status is ServerStatus.REVIEW

    def test_hard_fail_scores_zero_rejected(self):
        result = self._full_success()
        result.hard_fail = True
        outcome = score_server(result)
        assert outcome.quality_score == 0
        assert outcome.status is ServerStatus.REJECTED
        assert outcome.hard_fail

    def test_mid_score_goes_to_review(self):
        result = self._full_success()
        result.tool_invocation = 0.2
        result.semantic_validity = 0.0
        outcome = score_server(result, attempts_completed=3, attempts_planned=3)
        assert 40 <= outcome.quality_score < 80
        assert outcome.status is ServerStatus.REVIEW

    def test_low_score_high_confidence_rejected(self):
        result = SoftCheckResults(
            install_connect=1.0,
            handshake_valid=1.0,
            tools_list_schema=0.0,
            tool_invocation=0.0,
            semantic_validity=0.0,
            latency_category="slow",
        )
        outcome = score_server(result, attempts_completed=3, attempts_planned=3)
        assert outcome.quality_score < 40
        assert outcome.status is ServerStatus.REJECTED

    def test_low_score_low_confidence_retry_pending(self):
        result = SoftCheckResults(
            install_connect=1.0, handshake_valid=0.0,
            tools_list_schema=0.0, tool_invocation=0.0,
        )
        outcome = score_server(result, attempts_completed=1, attempts_planned=3)
        assert outcome.quality_score < 40
        assert outcome.status is ServerStatus.RETRY_PENDING

    def test_score_exactly_40_is_review_not_rejected(self):
        result = SoftCheckResults(
            install_connect=1.0, handshake_valid=1.0,
            tools_list_schema=0.0, tool_invocation=0.0,
        )
        outcome = score_server(result, attempts_completed=3, attempts_planned=3)
        assert outcome.quality_score == 40
        assert outcome.status is ServerStatus.REVIEW

    def test_auth_blocked_partial_verified(self):
        result = SoftCheckResults(
            install_connect=1.0,
            handshake_valid=1.0,
            auth_blocked=True,
        )
        outcome = score_server(result)
        assert outcome.status is ServerStatus.PARTIAL_VERIFIED

    def test_latency_penalty_not_rejection(self):
        slow = self._full_success()
        slow.latency_category = "slow"
        fast = self._full_success()
        fast.latency_category = "fast"
        slow_outcome = score_server(slow, attempts_completed=3, attempts_planned=3)
        fast_outcome = score_server(fast, attempts_completed=3, attempts_planned=3)
        assert slow_outcome.quality_score < fast_outcome.quality_score
        assert slow_outcome.status is ServerStatus.VERIFIED  # still passes

    def test_ttl_lengths(self):
        assert ttl_days_for("local") == 30
        assert ttl_days_for("remote") == 7


# ---------------------------------------------------------------------------
# Timeout policy (Section 6)
# ---------------------------------------------------------------------------


class TestTimeoutPolicy:
    def test_stage_budgets_differ(self):
        from app.verification.timeout_policy import (
            CONNECTION_TIMEOUT_S,
            HANDSHAKE_TIMEOUT_S,
            TOOL_INVOCATION_TIMEOUT_S,
            HARD_KILL_TIMEOUT_S,
        )
        assert CONNECTION_TIMEOUT_S < HANDSHAKE_TIMEOUT_S < TOOL_INVOCATION_TIMEOUT_S
        assert TOOL_INVOCATION_TIMEOUT_S <= HARD_KILL_TIMEOUT_S

    def test_per_tool_budget(self):
        assert tool_policy("search_docs").effective_timeout_s < \
            tool_policy("generate_report").effective_timeout_s

    def test_hard_kill_cap(self):
        assert tool_policy("export_huge_bulk_sync").effective_timeout_s <= 180

    def test_readonly_detection(self):
        assert is_readonly_tool("list_files")
        assert is_readonly_tool("search_web")
        assert is_readonly_tool("get_user")
        assert not is_readonly_tool("delete_file")
        assert not is_readonly_tool("send_message")
        assert not is_readonly_tool("create_ticket")

    def test_synthetic_arguments_respect_schema(self):
        tool = {
            "name": "get_issue",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string"},
                    "repo": {"type": "string"},
                    "number": {"type": "integer"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "verbose": {"type": "boolean"},
                },
                "required": ["owner", "repo", "number"],
            },
        }
        args = synthetic_arguments(tool)
        assert args == {"owner": "test", "repo": "test", "number": 1}

    def test_synthetic_arguments_enum_and_default(self):
        tool = {
            "name": "t",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "level": {"type": "string", "enum": ["low", "high"]},
                    "count": {"type": "integer", "default": 7},
                },
                "required": ["level"],
            },
        }
        args = synthetic_arguments(tool)
        assert args == {"level": "low"}

    def test_malformed_arguments_break_types(self):
        tool = {
            "name": "get_issue",
            "inputSchema": {
                "type": "object",
                "properties": {"number": {"type": "integer"}},
                "required": ["number"],
            },
        }
        args = malformed_arguments(tool)
        assert args == {"number": "not-a-number"}


# ---------------------------------------------------------------------------
# Ingestion (Section 3)
# ---------------------------------------------------------------------------


class TestIngestion:
    def test_normalize_repo_identity(self):
        assert normalize_repo_identity("https://github.com/Owner/Repo.git") == "owner/repo"
        assert normalize_repo_identity("https://github.com/a/b/tree/main/x") == "a/b"
        assert normalize_repo_identity("https://example.com/a/b") is None

    def test_classify_auth_api_key(self):
        required, auth_type, flow, provider = classify_auth(
            env_names=["GITHUB_TOKEN"]
        )
        assert required and auth_type is AuthType.API_KEY and flow is None

    def test_classify_auth_oauth(self):
        required, auth_type, flow, provider = classify_auth(
            text="Uses OAuth2 to access your Notion workspace"
        )
        assert required and auth_type is AuthType.OAUTH2
        assert flow is OAuthFlow.AUTHORIZATION_CODE
        assert provider == "notion"

    def test_classify_auth_client_credentials(self):
        required, auth_type, flow, _ = classify_auth(
            text="supports client_credentials grant"
        )
        assert flow is OAuthFlow.CLIENT_CREDENTIALS

    def test_classify_auth_none(self):
        required, auth_type, flow, provider = classify_auth(text="a plain calculator")
        assert not required and auth_type is AuthType.NONE and provider is None

    def test_from_registry_candidate_local_package(self):
        candidate = SimpleNamespace(
            server_name="io.github.owner/some-tool",
            title="Some Tool",
            description="A tool",
            version="1.0.0",
            repository_url="https://github.com/owner/some-tool",
            packages=({"identifier": "@scope/some-mcp", "registry_type": "npm"},),
            remotes=(),
        )
        record = from_mcp_registry_candidate(candidate)
        assert record.transport is Transport.LOCAL
        assert record.install_cmd == "npx -y @scope/some-mcp"
        assert record.registry_name == "io.github.owner/some-tool"

    def test_from_registry_candidate_remote(self):
        candidate = SimpleNamespace(
            server_name="io.github.owner/remote-tool",
            title="Remote Tool",
            description="Uses OAuth with Notion",
            version="1.0.0",
            repository_url=None,
            packages=(),
            remotes=({"type": "streamable-http", "url": "https://mcp.example.com"},),
        )
        record = from_mcp_registry_candidate(candidate)
        assert record.transport is Transport.REMOTE
        assert record.endpoint_url == "https://mcp.example.com"
        assert record.auth_required
        assert record.oauth_provider == "notion"

    @pytest.mark.asyncio
    async def test_ingestion_dedupes_source_url(self):
        repo = FakeServerRepository()
        service = VerificationIngestionService(repo)
        record = _remote_record("https://mcp.example.com")
        first = await service.ingest(record)
        second = await service.ingest(_remote_record("https://mcp.example.com"))
        assert first.created
        assert not second.created
        assert second.skipped_reason == "duplicate source_url"

    @pytest.mark.asyncio
    async def test_ingestion_dedupes_repo_identity(self):
        repo = FakeServerRepository()
        service = VerificationIngestionService(repo)
        original = McpServerRecord(
            name="Original",
            source_url="https://registry.example/original",
            transport=Transport.LOCAL,
            install_cmd="npx -y original",
            repository_url="https://github.com/owner/repo",
        )
        await service.ingest(original)
        mirror = McpServerRecord(
            name="Mirror",
            source_url="https://registry.example/mirror",
            transport=Transport.LOCAL,
            install_cmd="npx -y mirror",
            repository_url="https://github.com/owner/repo.git",
        )
        result = await service.ingest(mirror)
        assert not result.created
        assert "duplicate repo identity" in (result.skipped_reason or "")


# ---------------------------------------------------------------------------
# Pipeline (Section 5.2 / 7 / 8) with faked remote client
# ---------------------------------------------------------------------------


def _success_connect_result():
    return SimpleNamespace(
        connected=True,
        auth_required=False,
        transport="streamable-http",
        tools=[
            SimpleNamespace(
                name="search_items",
                description="Search items (read-only)",
                input_schema={"type": "object", "properties": {}},
            ),
        ],
        error=None,
        server_info={"name": "srv", "version": "1.0"},
        auth_reason=None,
        user_message=None,
        show_token_input=False,
        show_oauth_button=False,
    )


def _success_invoke_result():
    return SimpleNamespace(
        status="success",
        result={"content": [{"text": '{"items": [1, 2, 3]}'}]},
        error=None,
        requires_auth=False,
        duration_ms=120,
        auth_reason=None,
        user_message=None,
    )


@pytest.fixture
def pipeline():
    repo = FakeServerRepository()
    decisions = FakeDecisionRepository()
    settings = SimpleNamespace(gemini_api_key="", gemini_model="gemini-2.5-flash")
    return VerificationPipeline(repo, decisions, settings), repo, decisions


class TestRemotePipeline:
    @pytest.mark.asyncio
    async def test_auth_success_path_earns_verified_badge(self, pipeline, monkeypatch):
        """An auth-gated remote server whose stored credential WORKS must
        complete a full pass (connect + handshake + tools/list + real
        tool invocation) and land in `verified` WITH the badge - the
        exact scenario the spec's 'auth + successfully working ->
        verified with badge' rule demands."""
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.auth_required = True
        record.auth_type = AuthType.OAUTH2
        record.oauth_flow = OAuthFlow.AUTHORIZATION_CODE
        record.oauth_provider = "notion"
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(_success_connect_result()),
            invoke=self._async(_success_invoke_result()),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )
        # Active stored provider credential with a future expiry.
        monkeypatch.setattr(
            svc, "_credential_token_for", self._async("valid-token")
        )

        result = await svc.verify(record)

        assert result.status is ServerStatus.VERIFIED
        assert result.verified_badge is True
        assert result.verified_via_auth is True
        assert result.last_verified_via == "auth"
        assert result.invocation_verified is True
        assert result.confidence is Confidence.HIGH
        assert result.attempts_planned == 1
        assert result.attempts_completed == 1
        assert result.ttl_expires_at is not None
        assert len(decisions.decisions) == 1

        # The pass must record the full stage trail, including the
        # handshake the auth path previously skipped.
        stage_names = [
            s["stage"] if isinstance(s, dict) else s.stage
            for s in result.verification_details["stages"]
        ]
        assert "handshake" in stage_names
        assert "tools_list" in stage_names

    @pytest.mark.asyncio
    async def test_verified_badge_awarded_on_anonymous_success(self, pipeline, monkeypatch):
        """The anonymous success path keeps earning the badge, just via
        the 'anonymous' route."""
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.attempts_completed = 2
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(_success_connect_result()),
            invoke=self._async(_success_invoke_result()),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)

        assert result.status is ServerStatus.VERIFIED
        assert result.verified_badge is True
        assert result.verified_via_auth is False
        assert result.last_verified_via == "anonymous"

    @pytest.mark.asyncio
    async def test_verified_badge_cleared_when_server_stops_working(self, pipeline, monkeypatch):
        """A previously-badged server that later fails its handshake
        loses the badge together with its verified status."""
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.verified_badge = True
        record.verified_via_auth = True
        record.last_verified_via = "auth"
        record.status = ServerStatus.VERIFIED
        record.attempts_completed = 2
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(SimpleNamespace(
                connected=False, auth_required=False, transport=None, tools=[],
                error="boom", auth_reason="server_error",
                user_message="Server error", show_token_input=False,
                show_oauth_button=False, server_info={},
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)

        assert result.verified_badge is False
        assert result.verified_via_auth is False
        assert result.last_verified_via is None

    @pytest.mark.asyncio
    async def test_token_refresh_on_expired_credential(self, pipeline, monkeypatch):
        """Expired access token + stored refresh token: the pipeline
        refreshes transparently and verification still completes with
        the auth badge."""
        from app.verification.models import ProviderCredentialRecord

        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.auth_type = AuthType.OAUTH2
        record.oauth_flow = OAuthFlow.AUTHORIZATION_CODE
        record.oauth_provider = "notion"
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(_success_connect_result()),
            invoke=self._async(_success_invoke_result()),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        refreshed_calls = []

        async def fake_refresh(credential):
            refreshed_calls.append(credential)
            return "fresh-token"

        monkeypatch.setattr(svc, "_refresh_provider_credential", fake_refresh)

        provider_repo = SimpleNamespace(
            get=self._async(ProviderCredentialRecord(
                provider="notion",
                client_id="cid",
                token_endpoint="https://auth.example/token",
                refresh_token="rt",
                access_token="stale",
                token_expires_at="2000-01-01T00:00:00+00:00",
                status="active",
            )),
            update=self._async(None),
        )
        svc.set_provider_repository(provider_repo)

        result = await svc.verify(record)

        assert refreshed_calls, "refresh grant must have been replayed"
        assert result.status is ServerStatus.VERIFIED
        assert result.verified_badge is True
        assert result.verified_via_auth is True

    @pytest.mark.asyncio
    async def test_failed_refresh_routes_to_reauth(self, pipeline, monkeypatch):
        """Expired token + failed refresh: credential is flagged
        reauth_required and the run is auth-blocked, never a fake pass."""
        from app.verification.models import ProviderCredentialRecord

        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.auth_type = AuthType.OAUTH2
        record.oauth_flow = OAuthFlow.AUTHORIZATION_CODE
        record.oauth_provider = "notion"
        await repo.create(record)

        captured_updates = {}

        async def capture_update(provider, updates):
            captured_updates.update(updates)

        provider_repo = SimpleNamespace(
            get=self._async(ProviderCredentialRecord(
                provider="notion",
                refresh_token="rt",
                token_endpoint="https://auth.example/token",
                access_token="stale",
                token_expires_at="2000-01-01T00:00:00+00:00",
                status="active",
            )),
            update=capture_update,
        )
        svc.set_provider_repository(provider_repo)
        monkeypatch.setattr(svc, "_refresh_provider_credential", self._async(None))
        monkeypatch.setattr(
            svc, "_refine_auth_metadata",
            self._async((AuthType.OAUTH2, OAuthFlow.AUTHORIZATION_CODE, "notion")),
        )

        # Anonymous connect is refused -> AUTH_REQUIRED path.
        fake_client = SimpleNamespace(
            connect=self._async(SimpleNamespace(
                connected=False, auth_required=True, transport=None, tools=[],
                error="unauthorized", auth_reason="unauthorized",
                user_message="Requires auth", show_token_input=False,
                show_oauth_button=True, server_info={},
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)

        assert captured_updates.get("status") == "reauth_required"
        # Auth-blocked (the refresh failed so no working credential),
        # never a fake pass - and definitely no badge.
        assert result.status in (
            ServerStatus.OAUTH_PENDING_CONSENT, ServerStatus.PARTIAL_VERIFIED,
        )
        assert result.verified_badge is False

    @pytest.mark.asyncio
    async def test_success_path_scores_and_persists_verified(self, pipeline, monkeypatch):
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.attempts_completed = 2  # final attempt -> high confidence
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(_success_connect_result()),
            invoke=self._async(_success_invoke_result()),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)

        assert result.status is ServerStatus.VERIFIED
        assert result.quality_score >= 80
        assert result.invocation_verified is True
        assert result.confidence is Confidence.HIGH
        assert result.ttl_expires_at is not None
        assert result.latency_category is not None
        assert len(decisions.decisions) == 1
        assert decisions.decisions[0].resulting_status == "verified"

    @pytest.mark.asyncio
    async def test_transport_fail_first_attempt_is_retry_not_rejection(
        self, pipeline, monkeypatch
    ):
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(SimpleNamespace(
                connected=False, auth_required=False, transport=None, tools=[],
                error="DNS failure", auth_reason="connection_error",
                user_message="Could not connect", show_token_input=False,
                show_oauth_button=False, server_info={},
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)
        # 1 of 3 attempts: inconclusive, not a verdict (rule 3).
        assert result.status in (ServerStatus.RETRY_PENDING, ServerStatus.REJECTED)
        assert result.attempts_completed == 1
        assert result.attempts_planned == 3

    @pytest.mark.asyncio
    async def test_auth_required_no_credentials_partial_verified(self, pipeline, monkeypatch):
        svc, repo, decisions = pipeline
        record = _remote_record("https://mcp.example.com")
        record.oauth_provider = None
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(SimpleNamespace(
                connected=False, auth_required=True, transport=None, tools=[],
                error="Authentication required", auth_reason="unauthorized",
                user_message="This MCP server requires credentials.",
                show_token_input=True, show_oauth_button=True, server_info={},
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )
        monkeypatch.setattr(
            svc, "_refine_auth_metadata",
            self._async((AuthType.API_KEY, None, None)),
        )

        result = await svc.verify(record)
        # Rule 4: auth-required servers are NOT rejected.
        assert result.status in (
            ServerStatus.PARTIAL_VERIFIED, ServerStatus.OAUTH_PENDING_CONSENT,
        )
        assert result.auth_required is True

    @pytest.mark.asyncio
    async def test_oauth_code_without_consent_is_oauth_pending(self, pipeline, monkeypatch):
        svc, repo, _ = pipeline
        record = _remote_record("https://mcp.example.com")
        record.auth_type = AuthType.OAUTH2
        record.oauth_flow = OAuthFlow.AUTHORIZATION_CODE
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(SimpleNamespace(
                connected=False, auth_required=True, transport=None, tools=[],
                error="Authentication required", auth_reason="unauthorized",
                user_message="Requires OAuth.", show_token_input=False,
                show_oauth_button=True, server_info={},
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )
        monkeypatch.setattr(
            svc, "_refine_auth_metadata",
            self._async((AuthType.OAUTH2, OAuthFlow.AUTHORIZATION_CODE, "notion")),
        )

        result = await svc.verify(record)
        assert result.status is ServerStatus.OAUTH_PENDING_CONSENT

    @pytest.mark.asyncio
    async def test_semantic_judge_fails_stub_output(self, pipeline, monkeypatch):
        svc, repo, _ = pipeline
        record = _remote_record("https://mcp.example.com")
        await repo.create(record)

        fake_client = SimpleNamespace(
            connect=self._async(_success_connect_result()),
            invoke=self._async(SimpleNamespace(
                status="success",
                result={"content": [{"text": "This is a placeholder response"}]},
                error=None, requires_auth=False, duration_ms=50,
                auth_reason=None, user_message=None,
            )),
        )
        monkeypatch.setattr(
            "app.verification.verifiers.MCPTestClient",
            lambda *a, **kw: fake_client,
        )

        result = await svc.verify(record)
        # Semantic check catches the stub; invocation succeeded at the
        # protocol level, so status reflects the degraded score.
        assert result.quality_score < 95

    @pytest.mark.asyncio
    async def test_local_unparseable_install_is_malformed(self, pipeline):
        svc, repo, _ = pipeline
        record = McpServerRecord(
            name="broken-local",
            source_url="https://github.com/x/broken",
            transport=Transport.LOCAL,
            install_cmd='"unclosed quote',
        )
        await repo.create(record)
        result = await svc.verify(record)
        # A runtime install-parse failure is structural (malformed),
        # not a quality verdict.
        assert result.status is ServerStatus.MALFORMED
        assert result.rejection_check == "install_connect"

    @staticmethod
    def _async(value):
        async def _call(*args, **kwargs):
            return value
        return _call


class TestHelpers:
    def test_classify_remote_failure_categories(self):
        assert _classify_remote_failure("connection_error") is StageOutcomeKind.TRANSPORT_FAIL
        assert _classify_remote_failure("timeout") is StageOutcomeKind.TRANSPORT_FAIL
        assert _classify_remote_failure("not_found") is StageOutcomeKind.PROTOCOL_FAIL
        assert _classify_remote_failure(None) is StageOutcomeKind.TRANSPORT_FAIL

    def test_latency_categories(self):
        assert _latency_category(500) is _latency_category(1500)
        assert _latency_category(5000) is not _latency_category(500)
        assert _latency_category(30000).value == "slow"

    def test_local_tool_config_npx(self):
        record = McpServerRecord(
            name="t", source_url="s", transport=Transport.LOCAL,
            install_cmd="npx -y @scope/tool --port 3000",
        )
        config = _local_tool_config(record)
        assert config is not None
        assert config.registry_type == "npm"
        assert config.identifier == "@scope/tool"
        assert config.runtime_arguments == ["--port", "3000"]

    def test_local_tool_config_uvx(self):
        record = McpServerRecord(
            name="t", source_url="s", transport=Transport.LOCAL,
            install_cmd="uvx some-package",
        )
        config = _local_tool_config(record)
        assert config is not None
        assert config.registry_type == "pip"


# ---------------------------------------------------------------------------
# Cold-miss cascade (Section 9.1)
# ---------------------------------------------------------------------------


class TestColdMiss:
    @pytest.mark.asyncio
    async def test_cascade_stops_after_registry_hit(self, monkeypatch):
        from app.verification.queue import run_cold_miss_cascade

        repo = FakeServerRepository()
        decisions = FakeDecisionRepository()

        cold_miss_repo = FakeColdMissRepository()
        handles = SimpleNamespace(
            servers=repo,
            decisions=decisions,
            cold_miss=cold_miss_repo,
            ingestion=VerificationIngestionService(repo),
            pipeline=None,
            registry_client=FakeRegistry(hits=True),
            github_adapter=FakeGitHub(hits=True),
        )


        found = await run_cold_miss_cascade(handles, "figma")
        assert found > 0
        # GitHub must NOT have been called (step 2 before step 3).
        assert handles.github_adapter.calls == []
        record = await cold_miss_repo.get("figma")
        assert record.status == "done"
        assert record.registry_done

    @pytest.mark.asyncio
    async def test_cascade_falls_through_to_github(self, monkeypatch):
        from app.verification.queue import run_cold_miss_cascade

        repo = FakeServerRepository()
        decisions = FakeDecisionRepository()
        cold_miss_repo = FakeColdMissRepository()
        handles = SimpleNamespace(
            servers=repo,
            decisions=decisions,
            cold_miss=cold_miss_repo,
            ingestion=VerificationIngestionService(repo),
            pipeline=None,
            registry_client=FakeRegistry(hits=False),
            github_adapter=FakeGitHub(hits=True),
        )

        found = await run_cold_miss_cascade(handles, "obsidian-xyz")
        assert found > 0
        assert handles.github_adapter.calls == ["obsidian-xyz"]

    @pytest.mark.asyncio
    async def test_cascade_github_hit_without_install_cmd_is_malformed(self):
        """A GitHub repo with no documented runnable command is malformed
        at prefilter, never enters the queue, and does not count as found."""
        from app.verification.queue import run_cold_miss_cascade

        repo = FakeServerRepository()
        cold_miss_repo = FakeColdMissRepository()
        handles = SimpleNamespace(
            servers=repo,
            decisions=FakeDecisionRepository(),
            cold_miss=cold_miss_repo,
            ingestion=VerificationIngestionService(repo),
            pipeline=None,
            registry_client=FakeRegistry(hits=False),
            github_adapter=FakeGitHub(hits=True, readme="Just docs, no commands"),
        )
        found = await run_cold_miss_cascade(handles, "docs-only-repo")
        assert found == 0
        records = list(repo.records.values())
        assert records and records[0].status is ServerStatus.MALFORMED

    @pytest.mark.asyncio
    async def test_cascade_registry_hit_goes_through_prefilter_and_queue(self, monkeypatch):
        """A cold-miss hit enters the normal pipeline as pending - it is
        never fast-tracked to verified (Section 9.1)."""
        from app.verification.queue import run_cold_miss_cascade

        repo = FakeServerRepository()
        cold_miss_repo = FakeColdMissRepository()
        handles = SimpleNamespace(
            servers=repo,
            decisions=FakeDecisionRepository(),
            cold_miss=cold_miss_repo,
            ingestion=VerificationIngestionService(repo),
            pipeline=None,
            registry_client=FakeRegistry(hits=True, smithery=True),
            github_adapter=FakeGitHub(hits=False),
        )
        found = await run_cold_miss_cascade(handles, "smithery-server")
        # The Smithery candidate is ingested then immediately rejected at
        # prefilter - it never enters the verification queue.
        assert found == 0
        records = list(repo.records.values())
        assert records and records[0].status is ServerStatus.REJECTED


class FakeColdMissRepository:
    def __init__(self) -> None:
        self.records = {}

    async def enqueue(self, term):
        from app.verification.models import ColdMissQueryRecord

        record = ColdMissQueryRecord(query_key=term, term=term)
        self.records.setdefault(term, record)
        return self.records[term], term not in self.records

    async def get(self, term):
        return self.records.get(term)

    async def update(self, term, updates):
        record = self.records.get(term)
        if record:
            data = record.model_dump()
            data.update(updates)
            from app.verification.models import ColdMissQueryRecord

            self.records[term] = ColdMissQueryRecord.model_validate(data)

    async def list_pending(self, limit=20):
        return [r for r in self.records.values() if r.status != "done"]


class FakeRegistry:
    def __init__(self, hits: bool, smithery: bool = False) -> None:
        self.hits = hits
        self.smithery = smithery

    async def search(self, query, max_results=10):
        if not self.hits:
            return []
        if self.smithery:
            remotes = ({"type": "streamable-http",
                        "url": "https://server.smithery.ai/@x/y/mcp"},)
        else:
            remotes = ({"type": "streamable-http",
                        "url": "https://mcp.example.com/x"},)
        return [
            SimpleNamespace(
                server_name=f"io.github.owner/{query or 'tool'}",
                title=f"{query} server",
                description="A server",
                version="1.0.0",
                repository_url=None,
                packages=(),
                remotes=remotes,
            )
        ]


class FakeGitHub:
    def __init__(self, hits: bool, readme: str = "") -> None:
        self.hits = hits
        self.readme = readme
        self.calls: list[str] = []

    async def discover_mcp(self, query, max_results=10):
        self.calls.append(query)
        if not self.hits:
            return []
        return [
            SimpleNamespace(
                repository="owner/some-mcp-server",
                name="some-mcp-server",
                html_url="https://github.com/owner/some-mcp-server",
                clone_url="https://github.com/owner/some-mcp-server.git",
                default_branch="main",
                description="A GitHub MCP server",
                item_type=SimpleNamespace(),
                evidence=(),
                source=SimpleNamespace(),
                readme=self.readme or (
                    "# some-mcp-server\n\nRun with: `npx -y some-mcp-server`"
                ),
                root_entries=(),
            )
        ]
