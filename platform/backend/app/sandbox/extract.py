"""Evidence-backed execution planner for GitHub MCP repositories.

Repository understanding is kept separate from execution. The planner reads a
bounded set of high-signal files and never converts an install command into a
server command.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.artifacts.source_resolver import get_repository_tree, get_source_file, resolve_source
from app.models import Item
from app.sandbox.schemas import EnvironmentRequirement, LocalRunConfig, RemoteCandidate

_MANIFESTS = ("mcp.json", "server.json", ".mcpb/mcpb.json")
_DOC_NAMES = ("readme.md", "readme", "quickstart.md", "quick-start.md", "setup.md", "installation.md", "install.md")
_SECRET_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
_URL_RE = re.compile(r"https?://[^\s<>)`\]\"']+")


def _paths(tree: list[dict[str, Any]]) -> set[str]:
    return {str(node.get("path")) for node in tree if node.get("type") == "blob" and node.get("path")}


async def _file(item: Item, paths: set[str], path: str) -> str | None:
    if path not in paths:
        return None
    result = await get_source_file(item, path)
    return result.content if result.available and result.content else None


def _requirement(name: str, evidence: str, description: str | None = None) -> EnvironmentRequirement:
    return EnvironmentRequirement(name=name, evidence=evidence, description=description,
        is_secret=any(marker in name for marker in _SECRET_MARKERS))


def _requirements_from_text(text: str, evidence: str) -> list[EnvironmentRequirement]:
    return [_requirement(name, evidence) for name in dict.fromkeys(_ENV_RE.findall(text))
            if "_" in name or name in {"PORT", "HOST", "TRANSPORT"} or any(marker in name for marker in _SECRET_MARKERS)]


def _dedupe_requirements(requirements: list[EnvironmentRequirement]) -> list[EnvironmentRequirement]:
    seen: set[str] = set()
    return [r for r in requirements if not (r.name in seen or seen.add(r.name))]


def _remote_candidates(text: str, evidence: str) -> list[tuple[RemoteCandidate, str]]:
    """Find MCP endpoints only when URL or immediate context says MCP."""
    candidates: list[tuple[RemoteCandidate, str]] = []
    lowered = text.lower()
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;:")
        context = lowered[max(0, match.start() - 180):match.end() + 180]
        path = url.lower().split("?", 1)[0]
        if "mcp" not in context and not path.endswith(("/mcp", "/sse")):
            continue
        if "sse" in context or path.endswith("/sse"):
            transport = "sse"
        elif any(word in context for word in ("streamable", "http transport", "transport=http", "mcp endpoint")) or path.endswith("/mcp"):
            transport = "streamable-http"
        else:
            continue
        candidates.append((RemoteCandidate(type=transport, url=url), evidence))
    return candidates


def _command_from_package(data: dict[str, Any]) -> tuple[str | None, list[str], str | None]:
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    # Lifecycle/install/test/build scripts are deliberately excluded.
    for name in ("mcp", "start:mcp", "serve:mcp", "start", "serve"):
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            return "npm", ["run", name], f"package.json scripts.{name}"
    bin_value = data.get("bin")
    values = [bin_value] if isinstance(bin_value, str) else list(bin_value.values()) if isinstance(bin_value, dict) else []
    for value in values:
        if isinstance(value, str) and value:
            return "node", [value], "package.json bin"
    return None, [], None


def _command_from_pyproject(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"(?mi)^\s*([\w.-]*(?:mcp|server)[\w.-]*)\s*=\s*['\"]", text)
    return (match.group(1), "pyproject.toml project.scripts") if match else (None, None)


def _command_from_docs(text: str) -> tuple[str | None, list[str], str | None, str | None]:
    for line in text.splitlines():
        cleaned = line.strip().strip("`")
        match = re.search(r"(?:(?:[A-Z][A-Z0-9_]*=[^\s]+)\s+)*(npm\s+(?:run\s+)?(?:start|serve|mcp)|node\s+[^\s]+|python(?:3)?\s+-m\s+[\w.]+|uv\s+run\s+[^\s]+)", cleaned)
        if not match:
            continue
        full = match.group(0)
        prefix = full[:match.start(1)]
        parts = full[match.start(1):].split()
        if parts[:2] == ["npm", "start"]:
            return "npm", ["start"], prefix, cleaned
        if parts[:2] == ["npm", "run"] and len(parts) >= 3:
            return "npm", ["run", parts[2]], prefix, cleaned
        return parts[0], parts[1:], prefix, cleaned
    return None, [], None, None


async def extract_local_run_config(item: Item) -> LocalRunConfig:
    """Return an evidence-backed candidate execution plan for a GitHub repo."""
    tree_result, _ = await get_repository_tree(item)
    tree = tree_result.tree or [] if tree_result.available else []
    paths = _paths(tree)
    docs: list[tuple[str, str]] = []
    readme_result, _ = await resolve_source(item)
    if readme_result.available and readme_result.content:
        docs.append(("README", readme_result.content))
    for path in sorted(paths):
        lower = path.lower()
        if lower in _DOC_NAMES or (lower.startswith("docs/") and any(word in lower for word in ("quick", "setup", "install", "mcp"))):
            content = await _file(item, paths, path)
            if content and all(content != old for _, old in docs):
                docs.append((path, content))

    requirements: list[EnvironmentRequirement] = []
    remotes: list[tuple[RemoteCandidate, str]] = []
    for source, text in docs:
        requirements.extend(_requirements_from_text(text, source))
        remotes.extend(_remote_candidates(text, source))

    command: str | None = None
    args: list[str] = []
    runtime: str | None = None
    install: str | None = None
    source = "heuristic"
    transport = "stdio"
    rationale: str | None = None

    for manifest_path in _MANIFESTS:
        content = await _file(item, paths, manifest_path)
        if not content:
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue
        entry = next(iter((data.get("mcpServers") or {}).values()), data) if isinstance(data, dict) else {}
        if isinstance(entry, dict):
            if isinstance(entry.get("url"), str) and entry.get("type") in ("sse", "streamable-http"):
                remotes.append((RemoteCandidate(type=entry["type"], url=entry["url"]), manifest_path))
            if isinstance(entry.get("command"), str):
                command, args, source, rationale = entry["command"], list(entry.get("args") or []), "manifest", manifest_path
                requirements.extend(_requirement(name, manifest_path) for name in (entry.get("env") or {}) if isinstance(name, str))
                break

    # README client-config blocks are common repository evidence.  Treat them
    # exactly like a manifest, but only after an actual repository manifest.
    if not command:
        for doc_source, text in docs:
            for block in re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL):
                try:
                    config = json.loads(block)
                except ValueError:
                    continue
                servers = config.get("mcpServers") if isinstance(config, dict) else None
                entry = next(iter(servers.values()), None) if isinstance(servers, dict) else None
                if not isinstance(entry, dict):
                    continue
                if isinstance(entry.get("url"), str) and entry.get("type") in ("sse", "streamable-http"):
                    remotes.append((RemoteCandidate(type=entry["type"], url=entry["url"]), doc_source))
                if isinstance(entry.get("command"), str):
                    command, args, source, rationale = entry["command"], list(entry.get("args") or []), "readme", f"{doc_source} MCP configuration"
                    requirements.extend(_requirement(name, doc_source) for name in (entry.get("env") or {}) if isinstance(name, str))
                    break
            if command:
                break

    package = await _file(item, paths, "package.json")
    if package and not command:
        try:
            command, args, rationale = _command_from_package(json.loads(package))
        except ValueError:
            pass
        if command:
            runtime, install, source = "node", "npm ci" if "package-lock.json" in paths else "npm install", "package"

    pyproject = await _file(item, paths, "pyproject.toml")
    if pyproject and not command:
        command, rationale = _command_from_pyproject(pyproject)
        if command:
            args, runtime, install, source = [], "python", "pip install -e . --break-system-packages", "python"

    for doc_source, text in docs:
        if command:
            break
        doc_command, doc_args, env_prefix, line = _command_from_docs(text)
        if doc_command:
            command, args, rationale, source = doc_command, doc_args, f"{doc_source}: {line}", "readme"
            runtime = "node" if command in ("npm", "node") else "python"
            install = "npm ci" if runtime == "node" and "package-lock.json" in paths else "npm install" if runtime == "node" else "pip install -e . --break-system-packages"
            requirements.extend(_requirements_from_text(env_prefix or "", doc_source))
            if "TRANSPORT=http" in (env_prefix or "").upper():
                transport = "streamable-http"

    if not runtime:
        if "Dockerfile" in paths:
            runtime, install = "docker", "docker build -t mcp-test ."
        elif "package.json" in paths:
            runtime, install = "node", "npm ci" if "package-lock.json" in paths else "npm install"
        elif "pyproject.toml" in paths or "requirements.txt" in paths:
            runtime, install = "python", "pip install -e . --break-system-packages" if "pyproject.toml" in paths else "pip install -r requirements.txt --break-system-packages"

    requirements = _dedupe_requirements(requirements)
    combined = "\n".join(text for _, text in docs).lower()
    external = ["This server documents an external application, service, or network dependency."] if re.search(r"\b(requires?|connect(?:s)? to|depends on)\b.{0,100}\b(database|redis|docker|hardware|application|service|network)", combined) else []
    unique: dict[str, tuple[RemoteCandidate, str]] = {}
    for candidate, doc_source in remotes:
        unique.setdefault(candidate.url, (candidate, doc_source))
    remote = next(iter(unique.values()), (None, None))[0]
    evidence = [f"remote endpoint in {doc_source}: {candidate.url}" for candidate, doc_source in unique.values()]
    if rationale:
        evidence.append(rationale)
    execution_type = "both" if remote and command else "remote" if remote else "local" if command else "ambiguous"
    if execution_type == "ambiguous":
        rationale = "Repository has no evidence-backed MCP server command."
    return LocalRunConfig(source=source, runtime=runtime, install_command=install, command=command, args=args,
        env_vars=[r.name for r in requirements], remote=remote, execution_type=execution_type, transport=transport,
        required_env=requirements, external_dependencies=external, evidence=evidence, confidence=0.95 if source == "manifest" else 0.8 if command or remote else 0.2,
        rationale=rationale, candidates=[] if command or remote else ["No manifest, MCP entrypoint, package start script, or documented start command was found."])
