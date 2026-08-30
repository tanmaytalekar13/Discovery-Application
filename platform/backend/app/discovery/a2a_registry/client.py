"""A2A / agent registry discovery adapter (Phase 06).

Per CODEX_EXECUTION_PLAN.md Section 6 (A2A discovery sources ->
Mandatory/core, item 1): "A2A/agent registries or catalogs."

Unlike MCP (Section 9), there is no single official, universally
adopted A2A registry endpoint the platform can hardcode. This adapter
therefore speaks a small, documented list-endpoint contract and must
be pointed at one or more *configured* registry base URLs, rather than
shipping a hardcoded default. Registry entries are always untrusted
candidates (rule #7/#8 in the plan): they are never treated as a
validated Agent Card. Callers must resolve `agent_card_url` (or
`endpoint` + the well-known path) through
`app.discovery.a2a.client.resolve_a2a_agent` (Phase 03) before an
agent may enter the trusted catalog.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_A2A_REGISTRY_TIMEOUT_SECONDS = 10.0
DEFAULT_A2A_REGISTRY_PAGE_SIZE = 50
DEFAULT_A2A_REGISTRY_MAX_RESULTS = 100
DEFAULT_A2A_REGISTRY_MAX_PAGES = 10
DEFAULT_A2A_REGISTRY_LIST_PATH = "/agents"


class A2ARegistryError(RuntimeError):
    """Base error for A2A/agent registry discovery failures."""


class A2ARegistryRateLimitError(A2ARegistryError):
    """Raised when the configured registry rate-limits the client."""


class A2ARegistryAPIError(A2ARegistryError):
    """Raised for invalid or unsuccessful registry responses."""


@dataclass(frozen=True)
class A2ARegistryCandidate:
    """Untrusted agent metadata returned by an A2A/agent registry."""

    agent_name: str
    description: str
    version: str | None
    endpoint: str | None
    agent_card_url: str | None
    repository_url: str | None
    raw_entry: dict[str, Any]
    source: DiscoverySource
    validation_required: bool = field(default=True, compare=False)

    @property
    def item_type(self) -> ItemType:
        return ItemType.AGENT

    @property
    def protocol(self) -> str:
        return "a2a"


class A2ARegistryClient:
    """Read-only client for a configured A2A/agent registry discovery API.

    Expected response shape (``GET {base_url}{list_path}``)::

        {
          "agents": [
            {
              "name": "string",
              "description": "string",
              "version": "string (optional)",
              "status": "active | deleted | removed (optional)",
              "url": "https://... (agent endpoint, optional)",
              "agentCardUrl": "https://... (direct Agent Card URL, optional)",
              "repository": {"url": "https://..."} (optional)
            },
            ...
          ],
          "nextCursor": "string | null (optional, cursor pagination)"
        }

    An entry with neither ``url`` nor ``agentCardUrl`` cannot ever be
    protocol-resolved, so it is dropped rather than kept as a dead
    candidate.
    """

    def __init__(
        self,
        base_url: str,
        list_path: str = DEFAULT_A2A_REGISTRY_LIST_PATH,
        timeout_seconds: float = DEFAULT_A2A_REGISTRY_TIMEOUT_SECONDS,
        page_size: int = DEFAULT_A2A_REGISTRY_PAGE_SIZE,
        max_pages: int = DEFAULT_A2A_REGISTRY_MAX_PAGES,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must not be empty")
        if page_size < 1 or page_size > 200:
            raise ValueError("page_size must be between 1 and 200")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")

        self._base_url = base_url.rstrip("/")
        self._list_path = list_path if list_path.startswith("/") else f"/{list_path}"
        self._timeout = timeout_seconds
        self._page_size = page_size
        self._max_pages = max_pages
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()

    async def __aenter__(self) -> "A2ARegistryClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}{self._list_path}"

    async def search(
        self,
        query: str | None = None,
        max_results: int = DEFAULT_A2A_REGISTRY_MAX_RESULTS,
    ) -> list[A2ARegistryCandidate]:
        """Search the configured registry using bounded cursor pagination."""
        if query is not None and not query.strip():
            raise ValueError("A2A registry search query must not be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        candidates: list[A2ARegistryCandidate] = []
        cursor: str | None = None

        for _ in range(self._max_pages):
            params: dict[str, Any] = {
                "limit": min(self._page_size, max_results - len(candidates)),
            }
            if query:
                params["q"] = query.strip()
            if cursor:
                params["cursor"] = cursor

            payload = await self._request(params=params)
            entries = payload.get("agents")

            if not isinstance(entries, list):
                raise A2ARegistryAPIError(
                    "A2A registry response has an invalid 'agents' field"
                )

            for entry in entries:
                candidate = self._parse_candidate(entry)
                if candidate is not None:
                    candidates.append(candidate)
                    if len(candidates) >= max_results:
                        return candidates[:max_results]

            next_cursor = payload.get("nextCursor")
            if not next_cursor:
                break
            if not isinstance(next_cursor, str):
                raise A2ARegistryAPIError("A2A registry 'nextCursor' must be a string")
            cursor = next_cursor

        return candidates[:max_results]

    @staticmethod
    def _parse_candidate(entry: Any) -> A2ARegistryCandidate | None:
        if not isinstance(entry, dict):
            return None

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        if entry.get("status") in ("deleted", "removed"):
            return None

        endpoint = entry.get("url")
        if not isinstance(endpoint, str) or not endpoint:
            endpoint = None

        agent_card_url = entry.get("agentCardUrl")
        if not isinstance(agent_card_url, str) or not agent_card_url:
            agent_card_url = None

        if endpoint is None and agent_card_url is None:
            return None

        repository = entry.get("repository")
        repository_url = None
        if isinstance(repository, dict):
            value = repository.get("url")
            if isinstance(value, str) and value:
                repository_url = value

        version = entry.get("version")
        if not isinstance(version, str):
            version = None

        return A2ARegistryCandidate(
            agent_name=name,
            description=str(entry.get("description") or ""),
            version=version,
            endpoint=endpoint,
            agent_card_url=agent_card_url,
            repository_url=repository_url,
            raw_entry=dict(entry),
            source=DiscoverySource(
                type=SourceType.A2A_CATALOG,
                id=name,
                url=agent_card_url or endpoint or repository_url,
            ),
        )

    async def _request(self, *, params: dict[str, Any]) -> Any:
        try:
            response = await self._client.get(
                self.endpoint,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "agentic-discovery-platform",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise A2ARegistryAPIError(f"A2A registry request failed: {exc}") from exc

        if response.status_code in (403, 429):
            retry_after = response.headers.get("Retry-After") or response.headers.get(
                "X-RateLimit-Reset"
            )
            suffix = f" Retry after: {retry_after}." if retry_after else ""
            raise A2ARegistryRateLimitError(
                "A2A registry rate limit reached "
                f"(HTTP {response.status_code}).{suffix}"
            )

        if response.status_code >= 400:
            raise A2ARegistryAPIError(
                "A2A registry request failed with "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise A2ARegistryAPIError("A2A registry returned invalid JSON") from exc
