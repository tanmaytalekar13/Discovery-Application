"""Unit coverage for local MCP failure diagnostics (no Docker required)."""
from app.sandbox.local_mcp_client import LocalMCPClient


def test_startup_auth_error_is_actionable_and_extracts_env_name():
    message, reason, variables = LocalMCPClient()._analyze_error(
        "Error: GITHUB_TOKEN is required to authenticate", 1
    )

    assert reason == "unauthorized"
    assert variables == ["GITHUB_TOKEN"]
    assert message is not None
    assert "credentials" in message.lower()


def test_non_auth_exit_has_a_safe_configuration_message():
    message, reason, variables = LocalMCPClient()._analyze_error(
        "Error: Cannot find module 'missing-package'", 1
    )

    assert reason is None
    assert variables == []
    assert message is not None
    assert "MCP server error" in message
