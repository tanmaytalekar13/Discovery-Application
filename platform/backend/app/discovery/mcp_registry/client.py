"""Official MCP Registry discovery adapter (Phase 05).

The registry is a discovery source. Its metadata is never treated as proof
that a server is executable or trustworthy. Discovered entries are returned
as candidates and must continue through the existing MCP validation path
before entering the trusted catalog.

Official API:
    https://registry.modelcontextprotocol.io/v0.1/servers

The official registry exposes cursor-based pagination and supports search
and latest-version filtering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_MCP_REGISTRY_URL = (
    "https://registry.modelcontextprotocol.io"
)
DEFAULT_MCP_REGISTRY_TIMEOUT_SECONDS = 10.0
DEFAULT_MCP_REGISTRY_PAGE_SIZE = 50
DEFAULT_MCP_REGISTRY_MAX_RESULTS = 100
DEFAULT_MCP_REGISTRY_MAX_PAGES = 10


class MCPRegistryError(RuntimeError):
    """Base error for MCP Registry discovery failures."""


class MCPRegistryRateLimitError(MCPRegistryError):
    """Raised when the MCP Registry rate-limits the client."""


class MCPRegistryAPIError(MCPRegistryError):
    """Raised for invalid or unsuccessful registry responses."""


@dataclass(frozen=True)
class MCPRegistryCandidate:
    """Untrusted MCP server metadata returned by the registry."""

    server_name: str
    title: str | None
    description: str
    version: str
    repository_url: str | None
    packages: tuple[dict[str, Any], ...]
    remotes: tuple[dict[str, Any], ...]
    raw_server: dict[str, Any]
    source: DiscoverySource
    validation_required: bool = field(default=True, compare=False)

    @property
    def item_type(self) -> ItemType:
        return ItemType.TOOL

    @property
    def protocol(self) -> str:
        return "mcp"


class MCPRegistryClient:
    """Read-only client for the official MCP Registry discovery API."""

    def __init__(
        self,
        base_url: str = DEFAULT_MCP_REGISTRY_URL,
        timeout_seconds: float = DEFAULT_MCP_REGISTRY_TIMEOUT_SECONDS,
        page_size: int = DEFAULT_MCP_REGISTRY_PAGE_SIZE,
        max_pages: int = DEFAULT_MCP_REGISTRY_MAX_PAGES,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")

        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._page_size = page_size
        self._max_pages = max_pages
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()

    async def __aenter__(self) -> "MCPRegistryClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/v0.1/servers"

    async def search(
        self,
        query: str | None = None,
        max_results: int = DEFAULT_MCP_REGISTRY_MAX_RESULTS,
    ) -> list[MCPRegistryCandidate]:
        """Search the registry using bounded cursor pagination.

        ``version=latest`` prevents multiple historical versions of the same
        server from flooding a discovery result set.
        """
        if query is not None and not query.strip():
            raise ValueError("MCP Registry search query must not be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        candidates: list[MCPRegistryCandidate] = []
        cursor: str | None = None

        for _ in range(self._max_pages):
            params: dict[str, Any] = {
                "limit": min(self._page_size, max_results - len(candidates)),
                "version": "latest",
            }
            if query:
                params["search"] = query.strip()
            if cursor:
                params["cursor"] = cursor

            payload = await self._request(params=params)
            entries = payload.get("servers")

            if not isinstance(entries, list):
                raise MCPRegistryAPIError(
                    "MCP Registry response has an invalid 'servers' field"
                )

            for entry in entries:
                candidate = self._parse_candidate(entry)
                if candidate is not None:
                    candidates.append(candidate)
                    if len(candidates) >= max_results:
                        return candidates[:max_results]

            metadata = payload.get("metadata")
            if not isinstance(metadata, dict):
                break

            next_cursor = metadata.get("nextCursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str):
                raise MCPRegistryAPIError(
                    "MCP Registry metadata.nextCursor must be a string"
                )

            cursor = next_cursor

        return candidates[:max_results]

    async def get_latest(
        self,
        server_name: str,
    ) -> MCPRegistryCandidate:
        """Fetch one specific server's latest registry metadata."""
        if not server_name.strip():
            raise ValueError("server_name must not be empty")

        from urllib.parse import quote

        encoded_name = quote(server_name.strip(), safe="")
        payload = await self._request(
            path=(
                f"/v0.1/servers/{encoded_name}/versions/latest"
            )
        )

        candidate = self._parse_candidate(payload)
        if candidate is None:
            raise MCPRegistryAPIError(
                "MCP Registry returned an invalid server detail response"
            )
        return candidate

    @staticmethod
    def _parse_candidate(
        entry: Any,
    ) -> MCPRegistryCandidate | None:
        """Parse the wrapped official registry response shape.

        Expected list shape:
            {"server": {...}, "_meta": {...}}

        The detail endpoint may return the server object directly, so both
        forms are accepted.
        """
        if not isinstance(entry, dict):
            return None

        server = entry.get("server", entry)
        if not isinstance(server, dict):
            return None

        name = server.get("name")
        version = server.get("version")

        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None

        status = server.get("status")
        if status == "deleted":
            return None

        packages = server.get("packages", [])
        remotes = server.get("remotes", [])

        if not isinstance(packages, list):
            packages = []
        if not isinstance(remotes, list):
            remotes = []

        packages = tuple(
            package
            for package in packages
            if isinstance(package, dict)
        )
        remotes = tuple(
            remote
            for remote in remotes
            if isinstance(remote, dict)
        )

        repository = server.get("repository")
        repository_url = None
        if isinstance(repository, dict):
            value = repository.get("url")
            if isinstance(value, str) and value:
                repository_url = value

        return MCPRegistryCandidate(
            server_name=name,
            title=(
                str(server["title"])
                if server.get("title") is not None
                else None
            ),
            description=str(server.get("description") or ""),
            version=version,
            repository_url=repository_url,
            packages=packages,
            remotes=remotes,
            raw_server=dict(server),
            source=DiscoverySource(
                type=SourceType.MCP_REGISTRY,
                id=name,
                url=repository_url,
            ),
        )

    async def _request(
        self,
        *,
        params: dict[str, Any] | None = None,
        path: str = "/v0.1/servers",
    ) -> Any:
        url = f"{self._base_url}{path}"

        try:
            response = await self._client.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "agentic-discovery-platform",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise MCPRegistryAPIError(
                f"MCP Registry request failed: {exc}"
            ) from exc

        if response.status_code in (403, 429):
            retry_after = (
                response.headers.get("Retry-After")
                or response.headers.get("X-RateLimit-Reset")
            )
            suffix = (
                f" Retry after: {retry_after}."
                if retry_after
                else ""
            )
            raise MCPRegistryRateLimitError(
                "MCP Registry rate limit reached "
                f"(HTTP {response.status_code}).{suffix}"
            )

        if response.status_code >= 400:
            raise MCPRegistryAPIError(
                "MCP Registry request failed with "
                f"HTTP {response.status_code}: "
                f"{response.text[:500]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise MCPRegistryAPIError(
                "MCP Registry returned invalid JSON"
            ) from exc
