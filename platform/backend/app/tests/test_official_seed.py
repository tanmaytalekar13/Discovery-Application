"""Tests for the official MCP connector directory seed.

Covers the improved JSON (schema integrity), the seeder's Item mapping
('Official:' prefix, official_connectors provider, remote classification),
the non-STATIC / platform-excluded handling, and the provider-aware OAuth
scope defaults used by the authorize/start flow.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.db.seed_official_servers import (
    JSON_PATH,
    PROVIDER,
    _build_item,
    _canonical_id,
    _server_id,
    seed,
)
from app.models import Item
from app.sandbox.classifier import classify_tool
from app.sandbox.oauth import (
    DEFAULT_OAUTH_SCOPES,
    default_scopes_for,
    preregistered_client_for,
    preregistered_env_hint,
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def directory() -> dict:
    return json.loads(JSON_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def connectors(directory) -> list[dict]:
    return directory["connectors"]


# ---------------------------------------------------------------------------
# JSON integrity
# ---------------------------------------------------------------------------


def test_json_has_expected_shape(directory, connectors):
    assert directory["version"] == 3
    assert len(connectors) == directory["total_entries"] == 246


def test_json_ids_and_urls_are_unique(connectors):
    ids = [c["id"] for c in connectors]
    urls = [c["mcp_url"] for c in connectors if c.get("mcp_url")]
    assert len(ids) == len(set(ids))
    assert len(urls) == len(set(urls))


def test_every_entry_has_normalized_auth_fields(connectors):
    allowed_kinds = {"none", "api_key", "oauth2", "oauth_or_api_key", "unknown"}
    allowed_url_types = {"STATIC", "TEMPLATE", "TENANT_SPECIFIC", "REGION_SPECIFIC", "STORE_SPECIFIC"}
    for entry in connectors:
        assert entry["auth_kind"] in allowed_kinds, entry["id"]
        assert entry["url_type"] in allowed_url_types, entry["id"]
        assert isinstance(entry["testable"], bool), entry["id"]
        assert isinstance(entry["platform_excluded"], bool), entry["id"]
        assert entry["transport"] in {"streamable-http", "sse"}, entry["id"]
        # testable must exactly follow the seed policy rule: the URL can be
        # dialed as-is (no {placeholder}) and the operator is not excluded.
        url = entry["mcp_url"] or ""
        expected = bool(url) and "{" not in url and not entry["platform_excluded"]
        assert entry["testable"] == expected, entry["id"]


# ---------------------------------------------------------------------------
# Item construction
# ---------------------------------------------------------------------------


def _find(connectors, url: str) -> dict:
    return next(c for c in connectors if c["mcp_url"] == url)


def test_slack_entry_builds_official_remote_item(connectors):
    entry = _find(connectors, "https://mcp.slack.com/mcp")
    item = _build_item(entry, NOW)

    assert item.name == "Official: Slack"
    assert item.canonical_id == _canonical_id(entry["id"])
    assert item.source.provider == PROVIDER
    assert item.type.value == "tool"

    # Classifier must see it as a dialable remote — that is what unlocks
    # the whole Test Tool -> OAuth -> tools list flow.
    result = classify_tool(item)
    assert result.testable is True
    assert result.mode == "remote"
    assert result.detail is not None
    assert result.detail[0].url == "https://mcp.slack.com/mcp"
    assert result.detail[0].type == "streamable-http"


def test_oauth_entry_description_mentions_sign_in(connectors):
    entry = _find(connectors, "https://calendarmcp.googleapis.com/mcp/v1")
    item = _build_item(entry, NOW)
    assert item.name == "Official: Google Calendar"
    assert "OAuth sign-in" in item.description


def test_none_auth_entry_description(connectors):
    entry = _find(connectors, "https://learn.microsoft.com/api/mcp")
    item = _build_item(entry, NOW)
    assert "No sign-in required" in item.description


def test_tenant_specific_entry_is_not_dialable(connectors):
    entry = next(
        c
        for c in connectors
        if c["url_type"] == "TENANT_SPECIFIC" and "{tenant_id}" in c["mcp_url"]
    )
    item = _build_item(entry, NOW)
    assert item.name.startswith("Official: ")

    # No remotes config -> classifier refuses a live test.
    result = classify_tool(item)
    assert result.testable is False

    artifacts = item.artifacts.model_dump()
    assert artifacts["config_files"] == []


def test_server_id_is_stable_and_clean(connectors):
    entry = _find(connectors, "https://mcp.slack.com/mcp")
    sid = _server_id(entry)
    assert sid == f"official-{entry['id']}-slack"
    assert " " not in sid and sid == sid.lower()


# ---------------------------------------------------------------------------
# Seed summary (dry run — no DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_summary_counts(connectors):
    summary = await seed(dry_run=True)
    excluded = sum(1 for c in connectors if c["platform_excluded"])
    assert summary["skipped_excluded"] == excluded
    # Every remaining entry has a URL in the improved file.
    assert summary["skipped_no_url"] == 0
    assert summary["seeded"] + summary["skipped_excluded"] == len(connectors)


@pytest.mark.asyncio
async def test_excluded_gateways_are_waystation_rube_zapier(connectors):
    excluded_urls = {
        c["mcp_url"] for c in connectors if c["platform_excluded"]
    }
    assert "https://waystation.ai/mcp" in excluded_urls
    assert "https://rube.app/mcp" in excluded_urls
    assert "https://mcp.zapier.com/api/mcp/mcp" in excluded_urls


# ---------------------------------------------------------------------------
# OAuth scope defaults
# ---------------------------------------------------------------------------


def test_google_scopes_cover_drive_and_calendar():
    scopes = default_scopes_for("https://calendarmcp.googleapis.com/mcp/v1")
    assert scopes is not None
    assert "auth/drive" in scopes
    assert "auth/calendar" in scopes
    assert scopes.startswith("openid")


def test_entra_scopes_use_graph_default():
    scopes = default_scopes_for("https://mcp.ai.azure.com")
    assert scopes is not None
    assert "graph.microsoft.com/.default" in scopes


def test_server_advertised_scopes_win_over_defaults():
    scopes = default_scopes_for(
        "https://mcp.notion.com/mcp",
        {"scopes_supported": ["notion:read", "notion:write"]},
    )
    assert scopes == "notion:read notion:write"


def test_unknown_provider_returns_none():
    assert default_scopes_for("https://mcp.example-unlisted.dev/mcp") is None


def test_default_scopes_table_has_major_providers():
    # Keys are matched as host suffixes, so one entry per provider family.
    assert "googleapis.com" in DEFAULT_OAUTH_SCOPES
    assert "google.com" in DEFAULT_OAUTH_SCOPES
    assert "microsoftonline.com" in DEFAULT_OAUTH_SCOPES
    assert "azure.com" in DEFAULT_OAUTH_SCOPES
    assert "github.com" in DEFAULT_OAUTH_SCOPES
    assert "gitlab.com" in DEFAULT_OAUTH_SCOPES


# ---------------------------------------------------------------------------
# Pre-registered OAuth clients (providers without dynamic registration)
# ---------------------------------------------------------------------------


def test_preregistered_client_resolves_from_env(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_CLIENT_ID_SLACK", "1234.5678")
    result = preregistered_client_for("https://mcp.slack.com/mcp")
    assert result == ("1234.5678", None)  # PKCE public client, no secret


def test_preregistered_client_with_secret(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_CLIENT_ID_GOOGLE", "apps.googleusercontent.com")
    monkeypatch.setenv("MCP_OAUTH_CLIENT_SECRET_GOOGLE", "GOCSPX-secret")
    result = preregistered_client_for("https://calendarmcp.googleapis.com/mcp/v1")
    assert result == ("apps.googleusercontent.com", "GOCSPX-secret")


def test_preregistered_client_unset_returns_none(monkeypatch):
    monkeypatch.delenv("MCP_OAUTH_CLIENT_ID_SLACK", raising=False)
    assert preregistered_client_for("https://mcp.slack.com/mcp") is None


def test_preregistered_client_issuer_metadata_preferred(monkeypatch):
    """Issuer host from AS metadata (not the MCP origin) picks the client."""
    monkeypatch.setenv("MCP_OAUTH_CLIENT_ID_ATLASSIAN", "client-atlassian")
    result = preregistered_client_for(
        "https://mcp.atlassian.com/v2/mcp",
        {"issuer": "https://auth.atlassian.com"},
    )
    assert result == ("client-atlassian", None)


def test_preregistered_env_hint_names_the_variable():
    assert preregistered_env_hint("https://mcp.slack.com/mcp") == (
        "MCP_OAUTH_CLIENT_ID_SLACK"
    )
    assert preregistered_env_hint("https://mcp.linear.app/mcp") == (
        "MCP_OAUTH_CLIENT_ID_LINEAR"
    )
    assert "<PROVIDER>" in preregistered_env_hint("https://mcp.unknown.dev/mcp")


# ---------------------------------------------------------------------------
# Seeded items satisfy the Item model end to end
# ---------------------------------------------------------------------------


def test_every_static_entry_builds_a_valid_item(connectors):
    now = datetime.now(timezone.utc)
    built = 0
    for entry in connectors:
        if entry["platform_excluded"] or not entry.get("mcp_url"):
            continue
        item = _build_item(entry, now)
        assert isinstance(item, Item)
        assert item.name.startswith("Official: ")
        assert item.reliability.score > 0.5
        assert item.evidence[0].kind == "official_directory"
        built += 1
    assert built >= 240  # 246 minus the excluded gateways
