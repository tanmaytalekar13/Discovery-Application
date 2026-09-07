"""Classify discovered MCP tool items for live testing.

The classification decision tells the frontend which action button to
render (or hide) and tells the backend which URL to dial when the user
clicks 'Test Tool'.

Decision tree (first match wins):

  1. mcp_registry_remotes entry with at least one non-localhost URL
     -> remote
  2. mcp_registry_packages entry with streamable-http/sse transport
     pointing at a non-localhost URL
     -> remote_via_package
  3. mcp_registry_packages entry with stdio transport, or http/sse
     transport pointing at localhost
     -> local_stdio
  4. anything else (web search articles, blog posts, comparisons)
     -> not_testable

A package whose transport URL is localhost/127.0.0.1/0.0.0.0 is
treated as local even when its transport is HTTP-based, because the
server still needs to be running on the user's own machine.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.sandbox.schemas import (
    ClassificationResult,
    LocalPackageHint,
    RemoteCandidate,
)


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})

_REMOTE_TRANSPORT_TYPES = frozenset({"streamable-http", "sse"})


def _is_local_host(url: str | None) -> bool:
    """Return True if `url` is missing, unparseable, or points at a local host.

    Invalid URLs are treated as unsafe-to-dial-locally -> local, so the
    backend never accidentally attempts to connect to a malformed
    registry entry.
    """
    if not url:
        return True
    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        return True
    if not host:
        return True
    return host.lower() in _LOCAL_HOSTS


def _find_config_entry(
    config_files: list[dict[str, Any]], kind: str
) -> dict[str, Any] | None:
    for entry in config_files or []:
        if isinstance(entry, dict) and entry.get("kind") == kind:
            return entry
    return None


def _build_remote_candidates(
    remotes: list[dict[str, Any]],
) -> list[RemoteCandidate]:
    """Convert raw remotes into RemoteCandidate objects, dropping localhost.

    Streamable HTTP is preferred because it is the current MCP transport;
    legacy SSE candidates remain available as a fallback. The Registry does
    not require publishers to put their remotes in preference order, so this
    must not depend on the source-array order.

    Auth headers are extracted from the first required header entry in
    the remote's header list. The MCP registry uses 'x-api-key' for
    tools that require a bearer/API-key header instead of HTTP Basic.
    """
    candidates: list[RemoteCandidate] = []
    for remote in remotes or []:
        if not isinstance(remote, dict):
            continue
        url = remote.get("url")
        transport_type = remote.get("type")
        if not url or transport_type not in _REMOTE_TRANSPORT_TYPES:
            continue
        if _is_local_host(url):
            continue

        # Extract auth header from the first required header entry.
        # Registry entries with isRequired=True headers (e.g. x-api-key)
        # override the default Authorization: Bearer header.
        auth_header: str | None = None
        headers = remote.get("headers") or []
        if isinstance(headers, list):
            for h in headers:
                if isinstance(h, dict) and h.get("isRequired") is True:
                    auth_header = h.get("name")
                    break

        candidates.append(
            RemoteCandidate(
                type=transport_type,
                url=url,
                auth_header=auth_header,
            )
        )
    # Python's sort is stable, retaining publisher order within each
    # transport while placing the modern transport first.
    return sorted(
        candidates,
        key=lambda candidate: candidate.type != "streamable-http",
    )


def _required_auth_header(entry: dict[str, Any]) -> str | None:
    """Return the required custom auth header declared by a registry entry."""
    headers = entry.get("headers") or []
    if not isinstance(headers, list):
        return None
    for header in headers:
        if isinstance(header, dict) and header.get("isRequired") is True:
            name = header.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def _summarize_package(pkg: dict[str, Any]) -> LocalPackageHint:
    """Pull only the fields the 'Run Locally' UI needs."""
    transport = pkg.get("transport") or {}
    transport_type = transport.get("type") if isinstance(transport, dict) else None

    runtime_hint = pkg.get("runtimeHint")
    registry_type = pkg.get("registryType")
    identifier = pkg.get("identifier")

    install_command = _build_install_command(runtime_hint, registry_type, identifier)

    env_vars = pkg.get("environmentVariables") or []
    if not isinstance(env_vars, list):
        env_vars = []

    # Extract runtime arguments if present
    # MCP registry stores these as [{"value": "-y", "type": "positional"}, ...]
    # but the backend expects a flat list of strings.
    raw_runtime_args = pkg.get("runtimeArguments") or []
    if not isinstance(raw_runtime_args, list):
        raw_runtime_args = []
    runtime_arguments: list[str] = []
    for arg in raw_runtime_args:
        if isinstance(arg, str):
            runtime_arguments.append(arg)
        elif isinstance(arg, dict):
            value = arg.get("value")
            if isinstance(value, str):
                runtime_arguments.append(value)

    # Extract allowed domains for network whitelisting
    allowed_domains = pkg.get("allowedDomains") or []
    if not isinstance(allowed_domains, list):
        allowed_domains = []

    return LocalPackageHint(
        registryType=registry_type,
        identifier=identifier,
        runtimeHint=runtime_hint,
        transportType=transport_type,
        installCommand=install_command,
        environmentVariables=env_vars,
        runtimeArguments=runtime_arguments,
        allowedDomains=allowed_domains,
    )


def _build_install_command(
    runtime_hint: str | None,
    registry_type: str | None,
    identifier: str | None,
) -> str | None:
    """Best-effort install command for the 'Run Locally' info panel.

    We do not try to be clever here - the goal is a copy-pasteable hint,
    not a fully-validated shell command.
    """
    if not identifier:
        return None
    runtime = (runtime_hint or "").lower()
    if "npx" in runtime or registry_type == "npm":
        return f"npx -y {identifier}"
    if "uvx" in runtime or registry_type == "pypi":
        return f"uvx {identifier}"
    if "pip" in runtime:
        return f"pip install {identifier}"
    if "docker" in runtime or registry_type == "oci":
        return f"docker run -i --rm {identifier}"
    if runtime_hint:
        return f"{runtime_hint} {identifier}".strip()
    return identifier


def classify_tool(item: Any) -> ClassificationResult:
    """Classify a single discovered tool item for live testing.

    Accepts either an `Item` pydantic model or a plain dict - both
    shapes are tolerated so the function can be called from the API
    layer (Item) and from tests / scripts (dict).
    """
    if isinstance(item, dict):
        artifacts = item.get("artifacts") or {}
        config_files = artifacts.get("config_files") or []
    else:
        artifacts = getattr(item, "artifacts", None)
        config_files = getattr(artifacts, "config_files", None) or []

    if not isinstance(config_files, list):
        config_files = []

    remotes_entry = _find_config_entry(config_files, "mcp_registry_remotes")
    packages_entry = _find_config_entry(config_files, "mcp_registry_packages")

    # --- 1. Pure remote (mcp_registry_remotes entry) ----------------------
    if remotes_entry:
        candidates = _build_remote_candidates(remotes_entry.get("remotes") or [])
        if candidates:
            return ClassificationResult(
                testable=True,
                mode="remote",
                detail=candidates,
            )

    # --- 2. / 3. Packages --------------------------------------------------
    if packages_entry:
        raw_packages = packages_entry.get("packages") or []
        if not isinstance(raw_packages, list):
            raw_packages = []

        remote_package_candidates: list[RemoteCandidate] = []
        local_packages: list[LocalPackageHint] = []

        for pkg in raw_packages:
            if not isinstance(pkg, dict):
                continue
            transport = pkg.get("transport") or {}
            transport_type = (
                transport.get("type") if isinstance(transport, dict) else None
            )
            transport_url = (
                transport.get("url") if isinstance(transport, dict) else None
            )

            if (
                transport_type in _REMOTE_TRANSPORT_TYPES
                and transport_url
                and not _is_local_host(transport_url)
            ):
                # Package-based remote entries can declare headers either on
                # the package or its transport. Preserve this so a supplied
                # API key is sent using e.g. x-api-key instead of always as a
                # Bearer token.
                auth_header = _required_auth_header(pkg) or _required_auth_header(
                    transport
                )
                remote_package_candidates.append(
                    RemoteCandidate(
                        type=transport_type, url=transport_url, auth_header=auth_header
                    )
                )
            else:
                # stdio, or http/sse pointing at localhost -> local
                local_packages.append(_summarize_package(pkg))

        if remote_package_candidates:
            return ClassificationResult(
                testable=True,
                mode="remote_via_package",
                detail=remote_package_candidates,
            )

        if local_packages:
            return ClassificationResult(
                testable=False,
                mode="local_stdio",
                detail=local_packages,
                reason=(
                    "This tool needs to be installed and run on your own "
                    "machine; the app can show setup instructions but can't "
                    "execute it live."
                ),
            )

    # --- 4. Fallback ------------------------------------------------------
    return ClassificationResult(
        testable=False,
        mode="not_testable",
        detail=None,
        reason=(
            "No remote endpoint or installable package was found for this "
            "item (e.g. web search result or blog article)."
        ),
    )
