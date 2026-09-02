"""
Builds a ready-to-paste `mcpServers` JSON config block for a discovered
Item, derived entirely from data already captured during discovery.
Never fetches anything new, never fabricates content.

Priority order:
  1. Path A - a structured "mcp_registry_packages" entry inside
     item.artifacts.config_files (most reliable — comes straight from
     the official MCP registry's server.json format).
  2. Path B - extract a JSON object containing "mcpServers" out of
     item.artifacts.source_code (README / web-extracted docs text).
  3. Not available.

`item.artifacts.config_files` is `list[dict[str, Any]]` (untyped dicts),
so dict-style access is correct for entries inside it — only the outer
`item` / `item.artifacts` access needs to go through Pydantic attributes.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.models import Item


_CONFIG_KIND_REGISTRY_PACKAGES = "mcp_registry_packages"
_CONFIG_KIND_SYNTHESIZED = "synthesized_integration"

_DEFAULT_RUNTIME_BY_REGISTRY = {
    "npm": "npx",
    "pypi": "uvx",
    "nuget": "dnx",
}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "mcp-server"


def _find_config(item: Item, kind: str) -> Optional[dict[str, Any]]:
    for cf in item.artifacts.config_files:
        if cf.get("kind") == kind:
            return cf
    return None


def _resolve_positional_arg(arg: dict[str, Any]) -> str:
    hint = arg.get("valueHint") or arg.get("name") or "value"
    return f"<{hint}>"


def _build_args_from_package(pkg: dict[str, Any]) -> list[str]:
    args: list[str] = ["-y", pkg["identifier"]]
    for arg in pkg.get("packageArguments", []) or []:
        arg_type = arg.get("type")
        if arg_type == "positional":
            args.append(_resolve_positional_arg(arg))
        elif arg_type == "named":
            name = arg.get("name")
            if name:
                args.append(name)
            if arg.get("valueHint"):
                args.append(f"<{arg['valueHint']}>")
    return [a for a in args if a]


def _build_env_from_package(pkg: dict[str, Any]) -> dict[str, str]:
    env: dict[str, str] = {}
    for ev in pkg.get("environmentVariables", []) or []:
        name = ev.get("name")
        if name:
            env[name] = f"<{name}>"
    return env


def _pick_best_package(packages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for preferred in ("npm", "pypi"):
        for pkg in packages:
            if pkg.get("registryType") == preferred:
                return pkg
    return packages[0] if packages else None


def _build_from_registry_packages(packages: list[dict[str, Any]], server_name: str) -> Optional[dict]:
    pkg = _pick_best_package(packages)
    if not pkg or "identifier" not in pkg:
        return None

    registry_type = pkg.get("registryType", "npm")
    command = pkg.get("runtimeHint") or _DEFAULT_RUNTIME_BY_REGISTRY.get(registry_type, "npx")
    args = _build_args_from_package(pkg)
    env = _build_env_from_package(pkg)

    server_entry: dict[str, Any] = {"command": command, "args": args}
    if env:
        server_entry["env"] = env

    return {"mcpServers": {_slugify(server_name): server_entry}}


def _brace_match_json_candidates(text: str) -> list[str]:
    """Extract balanced-brace {...} substrings so nested JSON parses correctly."""
    candidates: list[str] = []
    depth = 0
    start: int | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidates.append(text[start : i + 1])
                    start = None
    return candidates


def _extract_json_config_block(text: str) -> Optional[dict]:
    if not text or "mcpServers" not in text:
        return None
    for candidate in _brace_match_json_candidates(text):
        if "mcpServers" not in candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "mcpServers" in parsed:
            return parsed
    return None


class IntegrationResult:
    """Plain container so schemas.py can wrap it into a Pydantic response model."""

    def __init__(
        self,
        available: bool,
        snippet: str | None = None,
        source: str | None = None,
        note: str | None = None,
    ) -> None:
        self.available = available
        self.snippet = snippet
        self.source = source
        self.note = note


def synthesize_integration(item: Item) -> IntegrationResult:
    if item.type.value != "tool" or item.tool is None:
        return IntegrationResult(
            available=False,
            note="Integration config generation currently supports MCP tools only.",
        )

    # 1. already-cached synthesis
    cached = _find_config(item, _CONFIG_KIND_SYNTHESIZED)
    if cached and cached.get("snippet"):
        return IntegrationResult(available=True, snippet=cached["snippet"], source="cached")

    # 2. Path A — structured registry packages
    packages_cf = _find_config(item, _CONFIG_KIND_REGISTRY_PACKAGES)
    if packages_cf and packages_cf.get("packages"):
        result = _build_from_registry_packages(packages_cf["packages"], item.name)
        if result:
            return IntegrationResult(
                available=True,
                snippet=json.dumps(result, indent=2),
                source="synthesized_from_registry",
            )

    # 3. Path B — extract from source_code / doc text
    if item.artifacts.source_code:
        result = _extract_json_config_block(item.artifacts.source_code)
        if result:
            return IntegrationResult(
                available=True,
                snippet=json.dumps(result, indent=2),
                source="extracted_from_docs",
            )

    # 4. nothing found
    return IntegrationResult(
        available=False,
        note="No integration config could be derived from registry metadata or source documentation.",
    )