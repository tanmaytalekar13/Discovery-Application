"""Extract a runnable config for a GitHub-sourced MCP server item.

Calls the same service-layer functions the 'Source' preview tab uses
(get_repository_tree, get_source_file, resolve_source in
app.artifacts.source_resolver) directly, in-process - not via HTTP.
This reuses their disk caching, branch fallback (main/master/HEAD),
and GitHub API handling instead of duplicating it behind a network hop.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.artifacts.source_resolver import (
    get_repository_tree,
    get_source_file,
    resolve_source,
)
from app.models import Item
from app.sandbox.schemas import LocalRunConfig

_MCP_MANIFEST_FILENAMES = ("mcp.json", "server.json", ".mcpb/mcpb.json")

# Loosely matches a fenced JSON code block anywhere in a markdown README.
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

# (filename to look for, runtime label, best-effort install command)
_RUNTIME_FILE_PRIORITY = (
    ("Dockerfile", "docker", "docker build -t mcp-test ."),
    ("uv.lock", "python-uv", "uv sync"),
    ("pyproject.toml", "python", "pip install -e . --break-system-packages"),
    ("requirements.txt", "python", "pip install -r requirements.txt --break-system-packages"),
    ("package.json", "node", "npm install"),
    ("go.mod", "go", "go build ./..."),
    ("Cargo.toml", "rust", "cargo build --release"),
)


def _looks_like_mcp_manifest(data: dict[str, Any]) -> bool:
    """Reject files named mcp.json/manifest.json that aren't actually ours -
    e.g. a Slack app manifest, which has oauth_config/settings instead.
    """
    return any(
        key in data
        for key in ("mcpServers", "server", "packages", "remotes", "tools", "command")
    )

async def _find_repo_manifest(
    item: Item, tree: list[dict[str, Any]]
) -> dict[str, Any] | None:
    paths = {node.get("path") for node in tree if node.get("type") == "blob"}
    for filename in _MCP_MANIFEST_FILENAMES:
        if filename not in paths:
            continue
        file_result = await get_source_file(item, filename)
        if not file_result.available or not file_result.content:
            continue
        try:
            data = json.loads(file_result.content)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and _looks_like_mcp_manifest(data):
            return data
    return None


def _extract_from_readme(readme_text: str) -> dict[str, Any] | None:
    """Pull the first `mcpServers` entry out of a README's JSON fences -
    the de-facto convention authors use for the copy-pasteable client
    config snippet (command/args/env).
    """
    for match in _JSON_FENCE_RE.finditer(readme_text):
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        servers = data.get("mcpServers") or data.get("mcp_servers")
        if isinstance(servers, dict) and servers:
            entry = next(iter(servers.values()))
            if isinstance(entry, dict) and ("command" in entry or "url" in entry):
                return entry
    return None


def _detect_runtime(tree: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    paths = {node.get("path") for node in tree if node.get("type") == "blob"}
    for filename, runtime, install_command in _RUNTIME_FILE_PRIORITY:
        if filename in paths:
            return runtime, install_command
    return None, None


async def extract_local_run_config(item: Item) -> LocalRunConfig:
    """Resolve a runnable config for a github_repository-sourced item.

    Called lazily when the user clicks 'Test Tool' on a `local_source`
    classification result - see classify.classify_tool for why this
    can't happen at classification time.
    """
    tree_result, _ = await get_repository_tree(item)
    tree = tree_result.tree or [] if tree_result.available else []

    manifest = await _find_repo_manifest(item, tree)
    if manifest is not None:
        return LocalRunConfig(
            source="manifest",
            command=manifest.get("command"),
            args=manifest.get("args") or [],
            env_vars=list((manifest.get("env") or {}).keys()),
        )

    readme_result, _ = await resolve_source(item)
    if readme_result.available and readme_result.content:
        readme_entry = _extract_from_readme(readme_result.content)
        if readme_entry:
            runtime, install_command = _detect_runtime(tree)
            return LocalRunConfig(
                source="readme",
                runtime=runtime,
                install_command=install_command,
                command=readme_entry.get("command"),
                args=readme_entry.get("args") or [],
                env_vars=list((readme_entry.get("env") or {}).keys()),
            )

    # Nothing structured found - heuristic only, command/args/env stay empty.
    runtime, install_command = _detect_runtime(tree)
    return LocalRunConfig(
        source="heuristic",
        runtime=runtime,
        install_command=install_command,
    )