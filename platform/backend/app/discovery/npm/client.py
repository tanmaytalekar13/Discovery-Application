"""npm registry discovery adapter.

Searches https://registry.npmjs.org/-/v1/search for packages matching
"mcp" keywords, then returns candidates with MCP evidence.

No authentication required. Rate limits: standard npm registry limits
(generous for public access).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_NPM_REGISTRY = "https://registry.npmjs.org"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 20


class npmDiscoveryError(RuntimeError):
    """Base error for npm discovery failures."""


@dataclass(frozen=True)
class NpmCandidate:
    """A package candidate discovered from the npm registry."""

    name: str
    version: str
    description: str
    homepage: str
    repository_url: str
    npm_url: str
    downloads_monthly: int
    score_final: float
    item_type: ItemType
    evidence: tuple[str, ...]
    source: DiscoverySource


class NpmDiscoveryAdapter:
    """Search npm registry for MCP-related packages."""

    # MCP-related keywords to filter packages
    _MCP_KEYWORDS = frozenset([
        "mcp", "mcp-server", "mcp-tool", "model-context-protocol",
        "mcp-client", "mcp-sdk",
    ])

    # Keywords that indicate an MCP package even without the mcp namespace
    _MCP_INDICATOR_TERMS = frozenset([
        "mcp server", "model context protocol", "mcp-client",
        "mcp-sdk", "@modelcontextprotocol",
    ])

    def __init__(
        self,
        registry_url: str = DEFAULT_NPM_REGISTRY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._registry_url = registry_url.rstrip("/")
        self._timeout = timeout_seconds
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()

    async def __aenter__(self) -> "NpmDiscoveryAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[NpmCandidate]:
        """Search npm for packages matching `query` with MCP evidence."""
        if not query.strip():
            raise npmDiscoveryError("npm discovery query must not be empty")

        # Search npm registry for mcp-related packages
        search_url = f"{self._registry_url}/-/v1/search"
        params = {
            "text": f"{query} mcp",
            "size": min(max_results * 2, 100),  # fetch more, filter down
            "quality": "0.65",
            "maintenance": "0.65",
            "popularity": "0.65",
        }

        try:
            response = await self._client.get(
                search_url,
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise npmDiscoveryError(f"npm registry request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise npmDiscoveryError(f"npm registry returned invalid JSON: {exc}") from exc

        objects: list[dict[str, Any]] = payload.get("objects", [])
        if not isinstance(objects, list):
            raise npmDiscoveryError("npm registry response has invalid 'objects' field")

        candidates: list[NpmCandidate] = []
        semaphore = asyncio.Semaphore(8)

        async def classify(
            entry: dict[str, Any],
        ) -> NpmCandidate | None:
            async with semaphore:
                return self._classify_package(entry)

        results = await asyncio.gather(
            *(classify(entry) for entry in objects)
        )

        for candidate in results:
            if candidate is not None:
                candidates.append(candidate)

        # Sort by score and limit
        candidates.sort(key=lambda c: c.score_final, reverse=True)
        return candidates[:max_results]

    def _classify_package(
        self,
        entry: dict[str, Any],
    ) -> NpmCandidate | None:
        """Classify a package as MCP or not based on metadata."""
        package: dict[str, Any] = entry.get("package", {})
        score_info: dict[str, Any] = entry.get("score", {})

        name = str(package.get("name", ""))
        version = str(package.get("version", ""))
        description = str(package.get("description", "")).lower()
        links: dict[str, str] = package.get("links", {})
        homepage = links.get("homepage", "")
        repository_url = links.get("repository", "")
        npm_url = links.get("npm", "")
        keywords: list[str] = package.get("keywords", []) or []

        # Skip packages with no description and no relevant keywords
        if not description and not keywords:
            return None

        mcp_evidence: list[str] = []

        # Check keywords
        keyword_set = frozenset(str(k).lower() for k in keywords)
        matching_keywords = keyword_set & self._MCP_KEYWORDS
        if matching_keywords:
            mcp_evidence.append(
                f"npm package keywords contain MCP indicators: {', '.join(sorted(matching_keywords))}"
            )

        # Check description for MCP terms
        for term in self._MCP_INDICATOR_TERMS:
            if term in description:
                mcp_evidence.append(
                    f"npm package description mentions '{term}'"
                )
                break

        # Check name for mcp prefix
        name_lower = name.lower()
        if name_lower.startswith("mcp") or "@mcp/" in name_lower:
            mcp_evidence.append(f"npm package name indicates MCP: {name}")

        # Check repository URL
        if repository_url and "mcp" in repository_url.lower():
            mcp_evidence.append("npm package repository URL contains 'mcp'")

        if not mcp_evidence:
            return None

        # Determine item type - assume tool for MCP packages
        item_type = ItemType.TOOL

        downloads: dict[str, Any] = entry.get("downloads", {}) or {}
        monthly = int(downloads.get("monthly") or 0)
        score_final = float(score_info.get("final", 0.0))

        return NpmCandidate(
            name=name,
            version=version,
            description=str(package.get("description", "")),
            homepage=homepage,
            repository_url=repository_url,
            npm_url=npm_url or f"https://www.npmjs.com/package/{name}",
            downloads_monthly=monthly,
            score_final=score_final,
            item_type=item_type,
            evidence=tuple(mcp_evidence),
            source=DiscoverySource(
                type=SourceType.CONFIGURED,
                id=f"npm:{name}",
                url=npm_url or f"https://www.npmjs.com/package/{name}",
                provider="npm_registry",
            ),
        )
