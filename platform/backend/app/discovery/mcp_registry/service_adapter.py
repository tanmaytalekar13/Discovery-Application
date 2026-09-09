"""MCP Registry service discovery adapter (Phase 09+).

Search the official MCP Registry (`https://registry.modelcontextprotocol.io`)
for MCP servers matching a given service name (e.g. "slack", "figma", "zoom").

Per CODEX_EXECUTION_PLAN.md:
- Section 7: "Do not put provider-specific logic into the Search Orchestrator."
- Section 33: each discovery source is independently enabled/disabled.
- The registry is a discovery source that returns candidates; validation
  happens downstream (Phase 10).

This adapter deliberately avoids hardcoded service lists. It takes a
user-supplied query, passes it to the registry's `search` parameter, and
filters results to only official MCP servers (those with
`io.modelcontextprotocol.registry/official` metadata present and active).

The resulting candidates are ordinary `CandidateReference`s that flow
through the existing orchestrator just like any other source adapter's
output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from app.discovery.common.candidate import CandidateReference
from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_MCP_REGISTRY_URL = "https://registry.modelcontextprotocol.io"
DEFAULT_MCP_REGISTRY_TIMEOUT_SECONDS = 10.0
DEFAULT_MCP_REGISTRY_SEARCH_LIMIT = 50
DEFAULT_MCP_REGISTRY_MAX_PAGES = 5


class MCPSearchError(RuntimeError):
    """Base error for MCP Registry search failures."""


class MCPSearchRateLimitError(MCPSearchError):
    """Raised when the MCP Registry rate-limits the client."""


class MCPSearchAPIError(MCPSearchError):
    """Raised for invalid or unsuccessful registry search responses."""


@dataclass(frozen=True)
class MCPSearchResult:
    """One official MCP server matching the search query."""

    server_name: str
    title: str | None
    description: str
    version: str
    repository_url: str | None
    remote_urls: tuple[str, ...] = field(default_factory=tuple)
    source_id: str | None = None
    source_url: str | None = None
    verification_status: str = "official"
    evidence: tuple[str, ...] = field(default_factory=tuple)


def _filter_official(
    entry: dict[str, Any],
) -> bool:
    """Return True if the registry entry is from the official registry and active."""
    meta = entry.get("_meta", {})
    if not isinstance(meta, dict):
        return False
    official_meta = meta.get("io.modelcontextprotocol.registry/official", {})
    if not isinstance(official_meta, dict):
        return False
    status = official_meta.get("status", "")
    return status == "active"


def _parse_server(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one server object from a registry search page entry."""
    server = entry.get("server", {})
    if not isinstance(server, dict):
        return None

    name = server.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    version = server.get("version")
    description = server.get("description") or ""

    repository = server.get("repository")
    repository_url: str | None = None
    if isinstance(repository, dict):
        url = repository.get("url")
        if isinstance(url, str) and url:
            repository_url = url

    # Collect remote URLs
    remotes = server.get("remotes", [])
    remote_urls: list[str] = []
    if isinstance(remotes, list):
        for remote in remotes:
            if isinstance(remote, dict):
                url = remote.get("url")
                if isinstance(url, str) and url:
                    remote_urls.append(url)

    source_id = name
    # Derive a URL from repo if available
    source_url = repository_url

    return {
        "server_name": name,
        "title": server.get("title"),
        "description": str(description),
        "version": str(version) if version else "unknown",
        "repository_url": repository_url,
        "remote_urls": tuple(remote_urls),
        "source_id": source_id,
        "source_url": source_url,
    }


async def search_servers(
    query: str,
    *,
    base_url: str = DEFAULT_MCP_REGISTRY_URL,
    timeout_seconds: float = DEFAULT_MCP_REGISTRY_TIMEOUT_SECONDS,
    limit: int = DEFAULT_MCP_REGISTRY_SEARCH_LIMIT,
    max_pages: int = DEFAULT_MCP_REGISTRY_MAX_PAGES,
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[MCPSearchResult, ...]:
    """Search the official MCP Registry for servers matching `query`.

    Returns only entries that are from the official registry and marked
    active. The registry's ``search`` parameter matches against server
    name, title, and description.

    The returned candidates are protocol-agnostic and can be converted
    to ``CandidateReference`` via ``from_mcp_registry_candidate`` or
    used directly.
    """
    if not query.strip():
        raise MCPSearchError("MCP Registry service search query must not be empty")

    if limit < 1:
        raise MCPSearchError("limit must be at least 1")

    if max_pages < 1:
        raise MCPSearchError("max_pages must be at least 1")

    search_url = f"{base_url}/v0.1/servers"
    client = httpx_client or httpx.AsyncClient()

    results: list[MCPSearchResult] = []
    cursor: str | None = None

    for _page_idx in range(max_pages):
        params: dict[str, Any] = {
            "limit": limit,
            "search": query,
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = await client.get(
                search_url,
                params=params,
                timeout=timeout_seconds,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "agentic-discovery-platform",
                },
            )
        except httpx.HTTPError as exc:
            raise MCPSearchError(
                f"MCP Registry search request failed: {exc}"
            ) from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            suffix = f" Retry after: {retry_after}." if retry_after else ""
            raise MCPSearchRateLimitError(
                f"MCP Registry rate limit reached (HTTP 429).{suffix}"
            )

        if response.status_code >= 400:
            raise MCPSearchAPIError(
                f"MCP Registry search request failed with "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise MCPSearchError(
                "MCP Registry returned invalid JSON"
            ) from exc

        servers_raw = payload.get("servers", [])
        metadata = payload.get("metadata", {})

        # Filter to only official + active entries
        for raw in (servers_raw if isinstance(servers_raw, list) else []):
            parsed = _parse_server(raw)
            if parsed is None:
                continue

            # Only include officially verified servers
            if not _filter_official(raw):
                continue

            result = MCPSearchResult(
                server_name=parsed["server_name"],
                title=parsed["title"],
                description=parsed["description"],
                version=parsed["version"],
                repository_url=parsed["repository_url"],
                remote_urls=parsed["remote_urls"],
                source_id=parsed["source_id"],
                source_url=parsed["source_url"],
                evidence=(
                    f"official MCP Registry entry, version {parsed['version']}",
                ),
            )
            results.append(result)

        # Pagination: check if there's a next page
        next_cursor = metadata.get("nextCursor")
        if not next_cursor:
            break
        if not isinstance(next_cursor, str):
            # Malformed cursor — stop pagination
            break

        cursor = next_cursor

        # If we've gathered enough, stop early
        if len(results) >= limit:
            break

    await client.aclose()
    return tuple(results[:limit])


def from_mcp_service_candidate(
    result: MCPSearchResult,
) -> CandidateReference:
    """Convert an ``MCPSearchResult`` to a ``CandidateReference``.

    The protocol is set to ``"mcp"`` and the item_type to ``TOOL``
    since MCP registry entries are tools by nature.
    """
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=SourceType.MCP_REGISTRY,
        source_provider="MCP Registry Service Search",
        source_id=result.server_name,
        url=result.source_url,
        repository_url=result.repository_url,
        title=result.server_name,
        description=result.description,
        evidence=result.evidence,
        raw_metadata={
            "source_candidate": result,
            "mcp_server_name": result.server_name,
            "mcp_version": result.version,
            "mcp_remote_urls": list(result.remote_urls),
        },
    )


async def discover_services(
    query: str,
    *,
    base_url: str = DEFAULT_MCP_REGISTRY_URL,
    timeout_seconds: float = DEFAULT_MCP_REGISTRY_TIMEOUT_SECONDS,
    limit: int = DEFAULT_MCP_REGISTRY_SEARCH_LIMIT,
    max_pages: int = DEFAULT_MCP_REGISTRY_MAX_PAGES,
    httpx_client: httpx.AsyncClient | None = None,
) -> tuple[CandidateReference, ...]:
    """High-level convenience: search and convert to candidates in one step.

    This is the function callers should use. It searches the official
    MCP Registry for the given service name and returns ready-to-use
    ``CandidateReference`` objects that integrate directly with the
    existing orchestrator.
    """
    search_results = await search_servers(
        query=query,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        limit=limit,
        max_pages=max_pages,
        httpx_client=httpx_client,
    )
    return tuple(
        from_mcp_service_candidate(result) for result in search_results
    )