"""Static prefilter for ingested MCP servers (spec v2 Section 4).

Cheap, synchronous, no quality judgment: eliminate structurally
invalid entries before spending verification compute. A server that
fails here gets `status = "malformed"` (or `"rejected"` for
known-incompatible hosts, Section 7.4) and never enters the
verification queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


from app.verification.models import McpServerRecord, Transport
from app.sandbox.package_size import (
    NPM_UNPACKED_SIZE_LIMIT_BYTES,
    PYPI_DEPENDENCY_LIMIT,
    PackageSizeEstimate,
    PackageSizeEstimator,
    describe_size,
)

# ---------------------------------------------------------------------------
# Section 7.4: known-incompatible third-party hosting platforms.
#
# Deliberately an explicit, named exclusion list - NOT a general heuristic -
# so it can be revisited if a platform's protocol changes. Every entry is a
# third-party aggregator/gateway that either broke the standard MCP protocol
# or sits behind platform-level auth our pipeline cannot complete:
#
# - Smithery: acquired by Arcade.dev and removed the standard MCP initialize
#   handshake/session IDs for its hosted remotes during the Arcade runtime
#   migration, so a standard initialize fails regardless of auth correctness.
# - Glama / PulseMCP / Composio / Zapier / Waystation: hosted MCP gateways
#   that require a platform account + platform-issued key (two-level auth,
#   spec v2 Section 7.3 Case B) before any MCP traffic; a standard handshake
#   with the upstream server's own credentials does not work.
#
# These servers are rejected directly at prefilter time and never enqueued.
# ---------------------------------------------------------------------------

KNOWN_INCOMPATIBLE_HOSTS: dict[str, str] = {
    "smithery.ai": "smithery_hosted_incompatible",
    "glama.ai": "glama_hosted_gateway_incompatible",
    "pulsemcp.com": "pulsemcp_hosted_proxy_incompatible",
    "composio.dev": "composio_hosted_gateway_incompatible",
    "mcp.zapier.com": "zapier_hosted_gateway_incompatible",
    "waystation.ai": "waystation_hosted_gateway_incompatible",
}

# Backwards-compatible alias for the original Smithery-only list.
SMITHERY_HOSTS = frozenset({"server.smithery.ai", "smithery.ai"})

# Local (stdio) install commands that route through an excluded platform's
# proxy runtime - e.g. `npx -y @smithery/cli install ...` launches Smithery's
# own gateway rather than the upstream server.
_INCOMPATIBLE_INSTALL_MARKERS: tuple[str, ...] = ("@smithery/cli",)


def _host_matches_excluded(host: str) -> str | None:
    """Return the exclusion reason when `host` is on the exclusion list."""
    host = host.lower()
    for known, reason in KNOWN_INCOMPATIBLE_HOSTS.items():
        if host == known or host.endswith(f".{known}"):
            return reason
    return None


def known_incompatible_reason(url: str | None) -> str | None:
    """Exclusion reason when `url` points at an excluded hosting platform.

    Pure hostname check (case-insensitive, exact match on the host or
    its registrar suffix) - no network calls. A URL like
    `https://not-server.smithery.ai.evil.example` does NOT match.
    """
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except (ValueError, TypeError):
        return None
    if not host:
        return None
    return _host_matches_excluded(host)


def is_smithery_hosted(url: str | None) -> bool:
    """True when `url` points at a Smithery-hosted remote endpoint."""
    return known_incompatible_reason(url) == "smithery_hosted_incompatible"


def is_known_incompatible_host(url: str | None) -> bool:
    """True when `url` points at any excluded third-party platform."""
    return known_incompatible_reason(url) is not None


def install_cmd_is_incompatible(install_cmd: str | None) -> bool:
    """True when a local install command routes through an excluded proxy."""
    if not install_cmd:
        return False
    lowered = install_cmd.lower()
    return any(marker in lowered for marker in _INCOMPATIBLE_INSTALL_MARKERS)


@dataclass
class PrefilterResult:
    """Outcome of the static prefilter for one server record."""

    passed: bool
    status: str  # "passed" | "malformed" | "rejected"
    failures: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.failures) if self.failures else "ok"


_INSTALL_HINT = re.compile(r"\b(npx|uvx|pipx|pip install|npm install|docker run)\b")

# ---------------------------------------------------------------------------
# Sandbox-size prefilter (exit 137 prevention at the source)
#
# Heavyweight packages (dozens of transitive dependencies) transiently
# exceed the sandbox memory cap during install and get OOM-killed. Rather
# than letting every such package die inside a container at verification
# time, estimate the install footprint from registry metadata while the
# record is still being ingested and mark it `malformed` - it never enters
# the verification queue and never shows up as a testable server.
# ---------------------------------------------------------------------------


def local_package_from_install_cmd(install_cmd: str | None) -> tuple[str, str] | None:
    """(registry_type, identifier) from a documented local install command.

    Mirrors the runtime parsing in verifiers._local_tool_config but only
    for the two package registries this size check applies to (npm, pypi).
    Returns None for anything else (remote, github-source, docker, node/python
    scripts) - those have no registry metadata to measure cheaply.
    """
    cmd = (install_cmd or "").strip()
    if not cmd:
        return None
    tokens = cmd.split()
    if not tokens:
        return None
    head = tokens[0].lower()
    rest = [
        t for t in tokens[1:]
        if t not in ("-y", "--yes", "run") and not t.startswith("--")
    ]
    if not rest:
        return None
    if head == "npx":
        return "npm", rest[0]
    if head in ("uvx", "pipx"):
        return "pypi", rest[0]
    return None


def _size_prefilter_failure(estimate: PackageSizeEstimate) -> list[str]:
    return [
        f"package_too_large: {describe_size(estimate)} exceeds the sandbox "
        f"install limit (npm {NPM_UNPACKED_SIZE_LIMIT_BYTES // (1024 * 1024)}MB "
        f"/ pypi {PYPI_DEPENDENCY_LIMIT} deps)"
    ]


async def prefilter_server_with_size(
    record: McpServerRecord,
    *,
    estimator: PackageSizeEstimator | None = None,
    dns_resolver=None,
) -> PrefilterResult:
    """Prefilter plus a registry-metadata size check for LOCAL records.

    Fail-open: when the size is unknown (registry error, missing metadata)
    the record passes this stage and the normal verification path decides.
    """
    result = prefilter_server(record, dns_resolver=dns_resolver)
    if not result.passed or record.transport is not Transport.LOCAL:
        return result

    package = local_package_from_install_cmd(record.install_cmd)
    if package is None:
        return result

    registry_type, identifier = package
    owns_estimator = estimator is None
    estimator = estimator or PackageSizeEstimator()
    try:
        estimate = await estimator.estimate(registry_type, identifier)
    finally:
        if owns_estimator:
            await estimator.aclose()

    if estimate.known and estimate.over_limit:
        return PrefilterResult(
            passed=False,
            status="malformed",
            failures=_size_prefilter_failure(estimate),
        )
    return result


def prefilter_server(
    record: McpServerRecord,
    *,
    dns_resolver=None,
) -> PrefilterResult:
    """Run the static checks.

    `dns_resolver` is an optional callable `host -> bool` (True when the
    host resolves). When omitted, remote endpoint URLs are only checked
    for well-formedness; DNS resolution is re-checked with a real
    resolver at verification time so a transient DNS blip at ingestion
    cannot permanently malformed-tag a good server.
    """
    failures: list[str] = []

    # A registry entry needs *something* addressable.
    if record.transport is Transport.LOCAL:
        if not (record.install_cmd or "").strip():
            failures.append("local server has no documented install command")
        elif install_cmd_is_incompatible(record.install_cmd):
            return PrefilterResult(
                passed=False,
                status="rejected",
                failures=["third_party_proxy_install_command"],
            )
    else:
        endpoint = (record.endpoint_url or "").strip()
        if not endpoint:
            failures.append("remote server has no endpoint URL")
        else:
            parsed = urlparse(endpoint)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                failures.append(f"endpoint URL is not well-formed: {endpoint}")
            elif record.source_url.startswith("file://"):
                failures.append("source_url must be an http(s) reference")

            # Section 7.4: known-incompatible third-party host -> reject
            # outright, before any other check.
            exclusion_reason = known_incompatible_reason(endpoint)
            if exclusion_reason:
                return PrefilterResult(
                    passed=False,
                    status="rejected",
                    failures=[exclusion_reason],
                )

            if dns_resolver is not None and parsed.hostname:
                try:
                    resolves = bool(dns_resolver(parsed.hostname))
                except Exception:  # noqa: BLE001 - resolver failure != malformed
                    resolves = True
                if not resolves:
                    failures.append(f"endpoint host does not resolve: {parsed.hostname}")

    if failures:
        return PrefilterResult(
            passed=False,
            status="malformed",
            failures=failures,
        )
    return PrefilterResult(passed=True, status="passed")


def prefilter_from_candidate(record: McpServerRecord) -> PrefilterResult:
    """Convenience wrapper used by the ingestion normalizer."""
    return prefilter_server(record)


async def resolve_dns(host: str) -> bool:
    """Real DNS resolution used by the async verification path."""
    import asyncio

    loop = asyncio.get_running_loop()
    import socket

    def _resolve() -> bool:
        try:
            socket.getaddrinfo(host, None)
            return True
        except OSError:
            return False

    return await loop.run_in_executor(None, _resolve)


__all__ = [
    "PrefilterResult",
    "prefilter_server",
    "prefilter_server_with_size",
    "prefilter_from_candidate",
    "local_package_from_install_cmd",
    "is_smithery_hosted",
    "is_known_incompatible_host",
    "known_incompatible_reason",
    "install_cmd_is_incompatible",
    "KNOWN_INCOMPATIBLE_HOSTS",
    "SMITHERY_HOSTS",
    "resolve_dns",
]
