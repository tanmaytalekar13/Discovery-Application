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
  4. github_repository entry with README-documented public MCP endpoint
     -> remote (README-sourced, same shape as a registry remote)
  5. github_repository entry with README-documented npx/uvx command
     and a resolvable package identifier -> local_stdio
  6. github_repository entry with no usable entrypoint -> local_source
     with an actionable reason (coordinates-only; run config extracted
     lazily on 'Test Tool' click - see sandbox.extract)
  7. anything else (web search articles, blog posts, comparisons)
     -> not_testable

A package whose transport URL is localhost/127.0.0.1/0.0.0.0 is
treated as local even when its transport is HTTP-based, because the
server still needs to be running on the user's own machine.

GitHub is a discovery source, not a registry: it never declares
transport/runtime info up front the way the MCP Registry does. The
classify_tool function reads README text (already captured in a
github_readme config entry) and classifies it exactly like a registry
entry would: a documented public endpoint is `remote`, a documented
npx/uvx command is `local_stdio`. Only when neither is found does the
item fall back to the coordinates-only `local_source` mode with a
clear actionable reason.

Key additions:
- Remote detection uses contextual anchors (not bare URLs) to avoid
  false positives from badges, package registry links, etc.
- Local commands are only classified as `local_stdio` when they use
  npx/uvx with a resolvable package identifier, or when there is a
  valid local installation/execution path.
- When no usable entrypoint exists, a clear actionable reason is
  returned instead of a generic "No manifest..." error.
- GitHub-discovered metadata is normalized into RemoteCandidate /
  LocalPackageHint structures matching the MCP Registry schemas so
  that classification, configuration, credentials, execution, and
  error handling behave consistently across sources.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from app.sandbox.schemas import (
    ClassificationResult,
    GithubSourceHint,
    LocalPackageHint,
    RemoteCandidate,
)


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})

_REMOTE_TRANSPORT_TYPES = frozenset({"streamable-http", "sse"})

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")

# Hosts that show up constantly in READMEs but are never the MCP endpoint
# itself (badges, the repo's own GitHub page, package registries, etc.).
_NON_ENDPOINT_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "npmjs.com",
        "www.npmjs.com",
        "pypi.org",
        "shields.io",
        "img.shields.io",
        "badge.fury.io",
        "codecov.io",
        "coveralls.io",
    }
)

_NON_ENDPOINT_PATH_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg", ".gif", ".md")

# Words that signal "this URL is where the server lives", as opposed to a
# link to docs, a badge, or the license.
_ENDPOINT_HINT_RE = re.compile(
    r"(endpoint|connect|remote|streamable|hosted|mcp server url|mcp url|server url|available at)",
    re.IGNORECASE,
)

# `npx [-flags ...] <package>` / `uvx [-flags ...] <package>`, run inside a
# shell fence or an inline code span - both common README conventions.
_NPX_COMMAND_RE = re.compile(r"\bnpx\s+(?:-{1,2}[\w-]+\s+)*(@?[\w][\w./@-]*)")
_UVX_COMMAND_RE = re.compile(r"\buvx\s+(?:-{1,2}[\w-]+\s+)*(@?[\w][\w./@-]*)")

# Environment-variable-looking tokens (SCREAMING_SNAKE_CASE with at least one
# underscore, so common all-caps acronyms like MCP/HTTP/SSE don't match).
_ENV_VAR_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


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


def _build_github_source_hint(entry: dict[str, Any]) -> GithubSourceHint | None:
    """Pull repo coordinates out of a github_repository config entry.

    This is intentionally thin: classify_tool only decides *whether*
    the item is testable and *which* mode to use. The actual run
    config (runtime, install command, entrypoint, required env vars)
    is derived later, lazily, from the repo's file tree - see
    sandbox.extract.extract_local_run_config. That step needs network
    calls (tree + blob fetch) so it must not run at classification
    time, which is expected to stay fast and synchronous.
    """
    repository = entry.get("repository")
    clone_url = entry.get("clone_url")
    if not repository or not clone_url:
        return None
    return GithubSourceHint(
        repository=repository,
        clone_url=clone_url,
        default_branch=entry.get("default_branch"),
    )


def _extract_remote_candidate_from_readme(readme: str) -> RemoteCandidate | None:
    """Find a documented public MCP endpoint in README prose, if any.

    Returns the first URL that is (a) not localhost, (b) not a badge/docs/
    package-registry link, and (c) either shaped like an MCP endpoint path
    or sits next to language that says so explicitly.
    """
    for match in _URL_RE.finditer(readme):
        raw_url = match.group(0).rstrip(".,;:)]}\"'")
        if _is_local_host(raw_url):
            continue

        parsed = urlparse(raw_url)
        host = (parsed.hostname or "").lower()
        if host in _NON_ENDPOINT_HOSTS:
            continue

        path = parsed.path.lower()
        if path.endswith(_NON_ENDPOINT_PATH_SUFFIXES):
            continue

        window_start = max(0, match.start() - 60)
        context = readme[window_start : match.end() + 20]

        looks_like_endpoint = (
            "/mcp" in path or path.endswith("/sse") or "/sse" in path
        )
        if not looks_like_endpoint and not _ENDPOINT_HINT_RE.search(context):
            continue

        transport_type = "sse" if "sse" in path or "sse" in context.lower() else (
            "streamable-http"
        )
        return RemoteCandidate(type=transport_type, url=raw_url, auth_header=None)
    return None


def _extract_env_var_hints(readme: str) -> list[dict[str, Any]]:
    seen: dict[str, None] = {}
    for name in _ENV_VAR_RE.findall(readme):
        seen.setdefault(name, None)
    return [{"name": name} for name in seen]


def _extract_local_package_from_readme(readme: str) -> LocalPackageHint | None:
    """Find a documented `npx`/`uvx` run command in README prose, if any.

    Only returns a hint when the command has a resolvable package identifier
    (i.e. not a bare local path command). This prevents classifying a README
    snippet like `npx node build/index.js` as a runnable local package when
    it actually requires a local checkout + build step.
    """
    env_vars = _extract_env_var_hints(readme)

    npx_match = _NPX_COMMAND_RE.search(readme)
    if npx_match:
        identifier = npx_match.group(1)
        if not _is_resolvable_identifier(identifier):
            return None
        return LocalPackageHint(
            registryType="npm",
            identifier=identifier,
            runtimeHint="npx",
            installCommand=_build_install_command("npx", "npm", identifier),
            environmentVariables=env_vars,
        )

    uvx_match = _UVX_COMMAND_RE.search(readme)
    if uvx_match:
        identifier = uvx_match.group(1)
        if not _is_resolvable_identifier(identifier):
            return None
        return LocalPackageHint(
            registryType="pypi",
            identifier=identifier,
            runtimeHint="uvx",
            installCommand=_build_install_command("uvx", "pypi", identifier),
            environmentVariables=env_vars,
        )

    return None


def _is_resolvable_identifier(identifier: str | None) -> bool:
    """Return True if the identifier is an npm package or GitHub repo shorthand.

    npm packages: ``@scope/name``, ``name``, ``name@tag``, ``name@version``
    GitHub shorthand: ``owner/repo``, ``owner/repo#branch``, ``owner/repo/path``
    PyPI packages: ``package``, ``package[extra]``, ``package==1.0``

    Local file paths (``./``, ``../``, absolute ``/``) are rejected.
    """
    if not identifier:
        return False
    ident = identifier.strip()

    # Local paths: ./foo, ../foo, /foo
    if ident.startswith(("./", "../", "/")):
        return False

    # npx/uvx package identifier
    if ident.startswith("@"):
        # @scope/name[@version] or @scope/name
        return "/" in ident
    # Plain package name, possibly with version specifier
    if "/" not in ident:
        return True  # npm or pypi package name
    # owner/repo or owner/repo/subpath
    if ident.count("/") == 1:
        return True
    return "/" in ident  # owner/repo/subpath (still GitHub shorthand)


def classify_tool(item: Any) -> ClassificationResult:
    """Classify a single discovered tool item for live testing.

    Accepts either an ``Item`` pydantic model or a plain dict - both
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
    github_entry = _find_config_entry(config_files, "github_repository")
    github_readme_entry = _find_config_entry(config_files, "github_readme")

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

    # --- 4. / 5. GitHub-sourced item with README-documented server -----------
    # GitHub never declares transport/runtime info up front the way the MCP
    # Registry does, so a repo's README is the only place a public endpoint
    # or run command would be documented. The README content was captured
    # during normalization (see normalization.pipeline._artifact_config_files).
    if github_entry and github_readme_entry:
        readme_content = github_readme_entry.get("content")
        if isinstance(readme_content, str) and readme_content.strip():
            # 4. Check for a documented public endpoint first (remote preferred)
            remote_candidate = _extract_remote_candidate_from_readme(readme_content)
            if remote_candidate:
                return ClassificationResult(
                    testable=True,
                    mode="remote",
                    detail=[remote_candidate],
                )

            # 5. Check for a documented npx/uvx command (only if resolvable)
            local_package = _extract_local_package_from_readme(readme_content)
            if local_package:
                return ClassificationResult(
                    testable=False,
                    mode="local_stdio",
                    detail=[local_package],
                    reason=(
                        "This tool needs to be installed and run on your own "
                        "machine; the app can show setup instructions but can't "
                        "execute it live."
                    ),
                )

    # --- 6. GitHub-sourced item, no usable README-documented endpoint/run ---
    # These items only carry repo coordinates (repository, clone_url,
    # default_branch). If there's a README but it didn't yield an endpoint
    # or resolvable command, we still classify as local_source (the lazy
    # sandbox.extract.extract_local_run_config step will inspect the full
    # file tree on 'Test Tool' click and may discover a manifest or
    # package.json the README never mentioned). When there's no README
    # at all we provide an actionable reason.
    if github_entry:
        hint = _build_github_source_hint(github_entry)
        if hint:
            if not github_readme_entry or not (
                isinstance(github_readme_entry.get("content"), str)
                and github_readme_entry["content"].strip()
            ):
                return ClassificationResult(
                    testable=False,
                    mode="local_source",
                    detail=hint,
                    reason=(
                        "Repository found but no README or package manifest was "
                        "available at classification time. Click 'Test Tool' to "
                        "fetch the repository tree and inspect for a manifest, "
                        "package.json, or documented entrypoint."
                    ),
                )
            return ClassificationResult(
                testable=True,
                mode="local_source",
                detail=hint,
            )

    # --- 7. Fallback --------------------------------------------------------
    return ClassificationResult(
        testable=False,
        mode="not_testable",
        detail=None,
        reason=(
            "No remote endpoint or installable package was found for this "
            "item (e.g. web search result or blog article)."
        ),
    )
