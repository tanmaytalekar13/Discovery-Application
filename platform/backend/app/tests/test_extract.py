"""Unit tests for sandbox.extract.extract_local_run_config.

Mocks the three source_resolver functions so these run without any
network access - each test asserts exactly one resolution path wins.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.sandbox.extract import extract_local_run_config


def _make_item(item_id: str = "test-item") -> object:
    """Minimal stand-in for an Item - extract.py only ever passes this
    through to the mocked source_resolver functions, never reads its
    fields directly, so a bare object with an item_id is enough.
    """
    item = type("FakeItem", (), {})()
    item.item_id = item_id
    return item


class _FakeTreeResult:
    def __init__(self, tree):
        self.available = True
        self.tree = tree


class _FakeFileResult:
    def __init__(self, content):
        self.available = content is not None
        self.content = content


class _FakePreviewResult:
    def __init__(self, content):
        self.available = content is not None
        self.content = content


@pytest.mark.asyncio
async def test_manifest_wins_when_present():
    """A valid mcp.json takes priority over README/heuristics."""
    tree = [{"path": "mcp.json", "type": "blob"}, {"path": "package.json", "type": "blob"}]
    manifest_content = (
        '{"command": "node", "args": ["dist/index.js"], "env": {"API_KEY": "x"}}'
    )

    with (
        patch(
            "app.sandbox.extract.get_repository_tree",
            new=AsyncMock(return_value=(_FakeTreeResult(tree), "cache/path")),
        ),
        patch(
            "app.sandbox.extract.get_source_file",
            new=AsyncMock(return_value=_FakeFileResult(manifest_content)),
        ),
        patch(
            "app.sandbox.extract.resolve_source",
            new=AsyncMock(return_value=(_FakePreviewResult(None), None)),
        ),
    ):
        result = await extract_local_run_config(_make_item())

    assert result.source == "manifest"
    assert result.command == "node"
    assert result.args == ["dist/index.js"]
    assert result.env_vars == ["API_KEY"]


@pytest.mark.asyncio
async def test_readme_wins_when_no_manifest():
    """No mcp.json on disk - falls back to the README's mcpServers block."""
    tree = [{"path": "pyproject.toml", "type": "blob"}, {"path": "README.md", "type": "blob"}]
    readme_content = """
# My MCP Server

```json
{
  "mcpServers": {
    "myserver": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/example/myserver.git", "myserver"],
      "env": {"MY_TOKEN": "..."}
    }
  }
}
```
"""

    with (
        patch(
            "app.sandbox.extract.get_repository_tree",
            new=AsyncMock(return_value=(_FakeTreeResult(tree), "cache/path")),
        ),
        patch(
            "app.sandbox.extract.get_source_file",
            new=AsyncMock(return_value=_FakeFileResult(None)),
        ),
        patch(
            "app.sandbox.extract.resolve_source",
            new=AsyncMock(return_value=(_FakePreviewResult(readme_content), None)),
        ),
    ):
        result = await extract_local_run_config(_make_item())

    assert result.source == "readme"
    assert result.runtime == "python"  # pyproject.toml present
    assert result.command == "uvx"
    assert result.args == ["--from", "git+https://github.com/example/myserver.git", "myserver"]
    assert result.env_vars == ["MY_TOKEN"]


@pytest.mark.asyncio
async def test_unrelated_manifest_json_is_rejected():
    """A manifest.json that isn't ours (e.g. a Slack app manifest) must not
    be mistaken for an MCP manifest - falls through to heuristic instead.
    """
    tree = [{"path": "package.json", "type": "blob"}]
    # Note: 'manifest.json' isn't even in _MCP_MANIFEST_FILENAMES, but this
    # also guards the case where a repo *does* ship mcp.json with an
    # unrelated shape (e.g. oauth_config keys instead of mcpServers/server).
    fake_manifest_content = '{"oauth_config": {"scopes": {"bot": ["chat:write"]}}}'

    with (
        patch(
            "app.sandbox.extract.get_repository_tree",
            new=AsyncMock(return_value=(_FakeTreeResult(tree + [{"path": "mcp.json", "type": "blob"}]), "cache/path")),
        ),
        patch(
            "app.sandbox.extract.get_source_file",
            new=AsyncMock(return_value=_FakeFileResult(fake_manifest_content)),
        ),
        patch(
            "app.sandbox.extract.resolve_source",
            new=AsyncMock(return_value=(_FakePreviewResult(None), None)),
        ),
    ):
        result = await extract_local_run_config(_make_item())

    assert result.source == "heuristic"
    assert result.runtime == "node"
    assert result.command is None


@pytest.mark.asyncio
async def test_heuristic_fallback_when_nothing_structured_found():
    """No manifest, no mcpServers block in README - runtime guess only."""
    tree = [{"path": "Dockerfile", "type": "blob"}, {"path": "package.json", "type": "blob"}]

    with (
        patch(
            "app.sandbox.extract.get_repository_tree",
            new=AsyncMock(return_value=(_FakeTreeResult(tree), "cache/path")),
        ),
        patch(
            "app.sandbox.extract.get_source_file",
            new=AsyncMock(return_value=_FakeFileResult(None)),
        ),
        patch(
            "app.sandbox.extract.resolve_source",
            new=AsyncMock(return_value=(_FakePreviewResult("# No config snippet here"), None)),
        ),
    ):
        result = await extract_local_run_config(_make_item())

    assert result.source == "heuristic"
    assert result.runtime == "docker"  # Dockerfile takes priority over package.json
    assert result.install_command == "docker build -t mcp-test ."
    assert result.command is None
    assert result.env_vars == []


@pytest.mark.asyncio
async def test_package_start_script_is_a_local_stdio_command():
    tree = [{"path": "package.json", "type": "blob"}]
    package = '{"scripts":{"start":"node dist/server.js"}}'
    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(return_value=_FakeFileResult(package))),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult("Set WEATHER_API_KEY first."), None))),
    ):
        result = await extract_local_run_config(_make_item())
    assert result.source == "package"
    assert result.command == "npm"
    assert result.args == ["run", "start"]
    assert result.install_command == "npm install"
    assert result.env_vars == ["WEATHER_API_KEY"]


@pytest.mark.asyncio
async def test_readme_remote_is_not_treated_as_local():
    tree = [{"path": "README.md", "type": "blob"}]
    readme = '''```json
    {"mcpServers":{"hosted":{"type":"streamable-http","url":"https://mcp.example.test/mcp","env":{"API_KEY":""}}}}
    ```'''
    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(return_value=_FakeFileResult(None))),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult(readme), None))),
    ):
        result = await extract_local_run_config(_make_item())
    assert result.remote is not None
    assert result.remote.type == "streamable-http"
    assert result.env_vars == ["API_KEY"]


@pytest.mark.asyncio
async def test_python_console_script_is_detected():
    tree = [{"path": "pyproject.toml", "type": "blob"}]
    pyproject = '[project.scripts]\nweather-mcp = "weather.server:main"\n'
    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(return_value=_FakeFileResult(pyproject))),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult(None), None))),
    ):
        result = await extract_local_run_config(_make_item())
    assert result.source == "python"
    assert result.command == "weather-mcp"


@pytest.mark.asyncio
async def test_quickstart_hosted_endpoint_wins_over_local_start_command():
    """A repo may offer both modes; discovery tests use the hosted MCP first."""
    tree = [
        {"path": "README.md", "type": "blob"},
        {"path": "quickstart.md", "type": "blob"},
        {"path": "package.json", "type": "blob"},
        {"path": "package-lock.json", "type": "blob"},
    ]
    files = {
        "quickstart.md": """Use the hosted MCP endpoint https://host.example.test/mcp\nTRANSPORT=http PORT=3015 npm start\nRequires ZoomISO application/network access.\n""",
        "package.json": '{"scripts":{"start":"node dist/server.js"}}',
    }

    async def read_file(_, path):
        return _FakeFileResult(files.get(path))

    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(side_effect=read_file)),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult("# Server"), None))),
    ):
        result = await extract_local_run_config(_make_item())

    assert result.execution_type == "both"
    assert result.remote and result.remote.url == "https://host.example.test/mcp"
    assert result.remote.type == "streamable-http"
    assert result.command == "npm"
    assert result.args == ["run", "start"]
    assert result.install_command == "npm ci"
    assert {item.name for item in result.required_env} >= {"TRANSPORT", "PORT"}
    assert result.external_dependencies


@pytest.mark.asyncio
async def test_documented_environment_requirements_are_described_without_values():
    tree = [{"path": "README.md", "type": "blob"}, {"path": "package.json", "type": "blob"}]
    readme = "Set API_KEY, SERVICE_HOST and SERVICE_PORT, then run npm start."

    async def read_file(_, path):
        return _FakeFileResult('{"scripts":{"start":"node server.js"}}' if path == "package.json" else None)

    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(side_effect=read_file)),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult(readme), None))),
    ):
        result = await extract_local_run_config(_make_item())
    reqs = {item.name: item for item in result.required_env}
    assert set(reqs) >= {"API_KEY", "SERVICE_HOST", "SERVICE_PORT"}
    assert reqs["API_KEY"].is_secret is True
    assert all(not hasattr(item, "value") for item in result.required_env)


@pytest.mark.asyncio
async def test_install_script_is_never_selected_as_run_command():
    tree = [{"path": "package.json", "type": "blob"}]
    package = '{"scripts":{"install":"node setup.js","build":"tsc"}}'
    with (
        patch("app.sandbox.extract.get_repository_tree", new=AsyncMock(return_value=(_FakeTreeResult(tree), None))),
        patch("app.sandbox.extract.get_source_file", new=AsyncMock(return_value=_FakeFileResult(package))),
        patch("app.sandbox.extract.resolve_source", new=AsyncMock(return_value=(_FakePreviewResult(None), None))),
    ):
        result = await extract_local_run_config(_make_item())
    assert result.command is None
    assert result.execution_type == "ambiguous"
