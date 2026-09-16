"""Ingestion normalizer (spec v2 Sections 3 and 9.1).

Converts candidates from the two supported sources - the official MCP
Registry and GitHub - into `McpServerRecord`s, classifies auth metadata
(oauth flow/provider, api_key), deduplicates forks/mirrors by canonical
repo identity, and pushes new entries into the verification queue with
`status = "pending"`.

Cold-miss-triggered ingestion (Section 9.1) uses the same normalizer
and de-duplicates against anything the scheduled cron is already
processing: an existing `pending`/queued record short-circuits the
insert.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.verification.models import (
    AuthType,
    McpServerRecord,
    OAuthFlow,
    ServerStatus,
    Transport,
)

logger = logging.getLogger(__name__)

_AUTH_MARKERS = ("api_key", "apikey", "api-key", "token", "secret", "credential")
_OAUTH_PROVIDER_HINTS = {
    "google": ("google", "gmail", "google drive", "google calendar", "youtube", "bigquery"),
    "github": ("github",),
    "notion": ("notion",),
    "slack": ("slack",),
}


@dataclass
class IngestionResult:
    """Outcome of ingesting one candidate."""

    record: McpServerRecord
    created: bool
    skipped_reason: str | None = None


def normalize_repo_identity(url: str | None) -> str | None:
    """Canonical `owner/repo` identity for dedupe of forks/mirrors.

    Returns None for non-repository URLs. GitHub and common mirror
    hosts are normalized to lowercase `owner/repo`.
    """
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return None
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com", "gitee.com", "gitlab.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = re.sub(r"\.git$", "", repo)
    return f"{owner}/{repo}".lower()


def classify_auth(
    *,
    text: str | None = None,
    env_names: list[str] | None = None,
    oauth_mentions: bool = False,
) -> tuple[bool, AuthType, OAuthFlow | None, str | None]:
    """Infer auth metadata from manifest/README/registry metadata.

    Returns (auth_required, auth_type, oauth_flow, oauth_provider).
    """
    env_names = env_names or []
    haystack = (text or "").lower()
    env_hit = any(any(marker in name.lower() for marker in _AUTH_MARKERS) for name in env_names)
    text_hit = any(marker in haystack for marker in _AUTH_MARKERS)

    provider: str | None = None
    for name, hints in _OAUTH_PROVIDER_HINTS.items():
        if any(hint in haystack for hint in hints):
            provider = name
            break

    if (
        oauth_mentions
        or "oauth" in haystack
        or "client_credentials" in haystack
        or "client credentials" in haystack
    ):
        flow = (
            OAuthFlow.CLIENT_CREDENTIALS
            if "client_credentials" in haystack or "client credentials" in haystack
            else OAuthFlow.AUTHORIZATION_CODE
        )
        return True, AuthType.OAUTH2, flow, provider

    if env_hit or text_hit:
        return True, AuthType.API_KEY, None, provider

    if provider:
        # The record names a known provider; assume delegated OAuth.
        return True, AuthType.OAUTH2, OAuthFlow.AUTHORIZATION_CODE, provider

    return False, AuthType.NONE, None, None


def _first_remote_url(remotes: tuple[dict[str, Any], ...] | list) -> str | None:
    """Pick the first usable streamable-http/SSE remote URL."""
    for remote in remotes:
        if not isinstance(remote, dict):
            continue
        url = remote.get("url")
        if not isinstance(url, str) or not url:
            continue
        transport = str(remote.get("type") or remote.get("transport") or "").lower()
        if transport in ("", "streamable-http", "sse", "streamable_http", "http"):
            return url
    return None


def _first_package(packages: tuple[dict[str, Any], ...] | list) -> dict[str, Any] | None:
    for package in packages:
        if isinstance(package, dict) and package.get("identifier"):
            return package
    return None


def from_mcp_registry_candidate(candidate: Any) -> McpServerRecord:
    """Normalize an `MCPRegistryCandidate` into a server record."""
    registry_name = candidate.server_name
    remotes = candidate.remotes
    packages = candidate.packages

    remote_url = _first_remote_url(remotes)
    package = _first_package(packages)

    if remote_url:
        transport = Transport.REMOTE
        endpoint_url = remote_url
        install_cmd = None
    elif package:
        transport = Transport.LOCAL
        endpoint_url = None
        registry_type = str(package.get("registry_type") or package.get("registryType") or "npm")
        identifier = str(package.get("identifier"))
        runtime_hint = str(package.get("runtime_hint") or package.get("runtimeHint") or "")
        if registry_type == "pypi" or "uvx" in runtime_hint or "pip" in registry_type:
            install_cmd = f"uvx {identifier}" if "uvx" in runtime_hint else f"pipx run {identifier}"
        else:
            install_cmd = f"npx -y {identifier}"
    else:
        # Neither a remote nor a package: structurally untestable.
        transport = Transport.REMOTE
        endpoint_url = None
        install_cmd = None

    env_names = [
        str(ev.get("name") or "")
        for ev in (package or {}).get("environment_variables", []) or []
        if isinstance(ev, dict)
    ]
    raw_text = f"{candidate.description or ''} {candidate.title or ''}"
    auth_required, auth_type, oauth_flow, oauth_provider = classify_auth(
        text=raw_text, env_names=env_names
    )

    return McpServerRecord(
        name=candidate.title or registry_name,
        source_url=f"https://registry.modelcontextprotocol.io/v0.1/servers/{registry_name}",
        transport=transport,
        install_cmd=install_cmd,
        endpoint_url=endpoint_url,
        declared_tools=[],
        description=candidate.description or "",
        registry_name=registry_name,
        repository_url=candidate.repository_url,
        auth_required=auth_required,
        auth_type=auth_type,
        oauth_flow=oauth_flow,
        oauth_provider=oauth_provider,
        status=ServerStatus.PENDING,
    )


_INSTALL_COMMAND_PATTERN = re.compile(
    r"\b(?:npx|uvx|pipx run)\s+-?y?\s*[\w@/.:-]+",
)


def extract_install_command(readme: str | None) -> str | None:
    """Best-effort documented install command from a repo README.

    Only runnable MCP launch commands (npx/uvx/pipx) are extracted -
    generic `npm install x` lines are not start commands. Never
    invented: an empty result means none was documented.
    """
    if not readme:
        return None
    match = _INSTALL_COMMAND_PATTERN.search(readme)
    if not match:
        return None
    command = match.group(0).strip()
    # Normalize 'npx -y pkg' / 'npx pkg' into the canonical form.
    parts = command.split()
    head = parts[0].lower()
    rest = [p for p in parts[1:] if p not in ("-y", "--yes")]
    return " ".join([head, *rest]) if rest else None


def from_github_candidate(candidate: Any) -> McpServerRecord:
    """Normalize a GitHub discovery candidate into a server record."""
    repo_url = str(getattr(candidate, "html_url", "") or "")
    name = getattr(candidate, "name", "") or repo_url.rsplit("/", 1)[-1]
    description = getattr(candidate, "description", "") or ""
    readme = str(getattr(candidate, "readme", "") or "")

    auth_required, auth_type, oauth_flow, oauth_provider = classify_auth(
        text=f"{description} {readme[:2000]}"
    )

    return McpServerRecord(
        name=name,
        source_url=repo_url or f"https://github.com/{name}",
        transport=Transport.LOCAL,
        install_cmd=extract_install_command(readme),
        endpoint_url=None,
        declared_tools=[],
        description=description,
        repository_url=repo_url or None,
        auth_required=auth_required,
        auth_type=auth_type,
        oauth_flow=oauth_flow,
        oauth_provider=oauth_provider,
        status=ServerStatus.PENDING,
    )


class VerificationIngestionService:
    """Deduplicating ingestion into the scored registry."""

    def __init__(self, repository) -> None:
        self._repository = repository

    async def ingest(
        self,
        record: McpServerRecord,
        *,
        skip_if_status_in: set[str] | None = None,
    ) -> IngestionResult:
        """Insert a new pending record unless an equivalent one exists.

        Dedupe rules (Section 3):
        - same source_url -> same server, skip;
        - same registry_name -> same official-registry identity, skip;
        - same canonical repo identity (owner/repo) -> fork/mirror of an
          already-known server, skip.
        """
        skip_if_status_in = skip_if_status_in or {"pending", "review", "retry_pending",
                                                  "partial_verified", "oauth_pending_consent"}

        existing_by_url = await self._repository.get_by_source_url(record.source_url)
        if existing_by_url is not None:
            return IngestionResult(
                record=existing_by_url, created=False,
                skipped_reason="duplicate source_url",
            )

        if record.registry_name:
            existing_by_name = await self._repository.get_by_registry_name(record.registry_name)
            if existing_by_name is not None:
                return IngestionResult(
                    record=existing_by_name, created=False,
                    skipped_reason="duplicate registry_name",
                )

        repo_identity = normalize_repo_identity(record.repository_url)
        if repo_identity:
            for existing in await self._repository.list_status(
                "pending", "review", "retry_pending", "partial_verified",
                "oauth_pending_consent", "verified", limit=500,
            ):
                if normalize_repo_identity(existing.repository_url) == repo_identity:
                    return IngestionResult(
                        record=existing, created=False,
                        skipped_reason=f"duplicate repo identity {repo_identity}",
                    )

        created = await self._repository.create(record)
        return IngestionResult(record=created, created=True)
