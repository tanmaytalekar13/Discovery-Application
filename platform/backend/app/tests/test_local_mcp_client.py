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


def test_syntax_error_provides_shebang_guidance():
    message, reason, variables = LocalMCPClient()._analyze_error(
        "/root/.npm/_npx/6e134f06b5a49f1f/node_modules/.bin/weather: line 7: syntax error: unexpected '('", 2
    )

    assert reason is None
    assert variables == []
    assert message is not None
    assert "syntax error" in message
    assert "shebang" in message


def test_local_tool_config_builds_node_runner_command_for_npm():
    from app.sandbox.container_manager import LocalToolConfig

    config = LocalToolConfig(
        registry_type="npm",
        identifier="@alexdemichieli/mcp-weather-server",
        runtime_arguments=["--city", "Paris"],
    )
    cmd = config.build_command()

    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert "node -e" in cmd[2]
    assert "@alexdemichieli/mcp-weather-server" in cmd[2]
    assert "--city" in cmd[2]
    assert "Paris" in cmd[2]


def test_local_tool_config_builds_pip_command():
    from app.sandbox.container_manager import LocalToolConfig

    config = LocalToolConfig(
        registry_type="pip",
        identifier="weather-ai-mcp",
    )
    cmd = config.build_command()

    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert "pipx run weather-ai-mcp" in cmd[2]
