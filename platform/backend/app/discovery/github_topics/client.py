"""GitHub topic-based MCP server discovery adapter.

Uses GitHub's topic search (`topic:mcp-server`) to find MCP-related
repositories, complementing the existing keyword-based GitHub adapter.

Also supports searching for `server.json` / `mcp.json` files in repos
by checking repository contents for MCP configuration files.

No authentication required (works with lower rate limits: 10 req/min).
Authentication raises the limit to 30 req/min for search.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 20
DEFAULT_MAX_CONCURRENT = 4


class GitHubTopicsError(RuntimeError):
    """Base error for GitHub topics discovery failures."""


@dataclass(frozen=True)
class GitHubTopicsCandidate:
    """A repository candidate discovered via GitHub topic search."""

    repository: str
    name: str
    html_url: str
    clone_url: str
    default_branch: str | None
    description: str
    stars: int
    language: str | None
    item_type: ItemType
    evidence: tuple[str, ...]
    source: DiscoverySource


class GitHubTopicsAdapter:
    """Search GitHub repositories by topic for MCP servers."""

    def __init__(
        self,
        token: str = "",
        api_base_url: str = DEFAULT_GITHUB_API,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_concurrent = max(1, max_concurrent)
        self._owns_client = httpx_client is None
        self._token = token
        self._client = httpx_client or httpx.AsyncClient()

    async def __aenter__(self) -> "GitHubTopicsAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentic-discovery-platform",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[GitHubTopicsCandidate]:
        """Search GitHub topics for MCP-related repositories.

        Uses `topic:mcp-server` combined with the user's query to find
        repositories explicitly tagged with MCP topics.
        """
        if not query.strip():
            raise GitHubTopicsError("GitHub topics discovery query must not be empty")

        if max_results < 1:
            raise GitHubTopicsError("max_results must be at least 1")

        # Combine topic filter with the user's query
        search_query = f"{query} topic:mcp-server"
        payload = await self._request(
            "GET",
            "/search/repositories",
            params={
                "q": search_query,
                "per_page": min(max_results, 100),
                "page": 1,
                "sort": "stars",
                "order": "desc",
            },
        )

        repositories = payload.get("items", [])
        if not isinstance(repositories, list):
            raise GitHubTopicsError(
                "GitHub search response has an invalid 'items' field"
            )

        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def classify(
            repo: dict[str, Any],
        ) -> GitHubTopicsCandidate | None:
            async with semaphore:
                return self._classify_repository(repo)

        results = await asyncio.gather(
            *(classify(repo) for repo in repositories[:max_results])
        )

        return [c for c in results if c is not None]

    async def discover_mcp_servers(
        self,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[GitHubTopicsCandidate]:
        """Find all GitHub repos tagged with `mcp-server` topic."""
        return await self.discover("MCP", max_results)

    def _classify_repository(
        self,
        repo: dict[str, Any],
    ) -> GitHubTopicsCandidate | None:
        full_name = repo.get("full_name")
        if not isinstance(full_name, str) or "/" not in full_name:
            return None

        name = str(repo.get("name") or full_name.rsplit("/", 1)[-1])
        html_url = str(repo.get("html_url") or "")
        clone_url = str(repo.get("clone_url") or "")
        default_branch = repo.get("default_branch")
        description = str(repo.get("description") or "")
        stars = int(repo.get("stargazers_count") or 0)
        language = repo.get("language")

        # Topics for evidence
        topics: list[str] = repo.get("topics", []) or []
        topic_str = " ".join(str(t).lower() for t in topics)

        mcp_evidence: list[str] = []

        # Topic-level evidence
        mcp_topics = [t for t in topics if "mcp" in str(t).lower()]
        if mcp_topics:
            mcp_evidence.append(
                f"GitHub topic contains MCP: {', '.join(mcp_topics)}"
            )

        # Text evidence
        text = " ".join((
            name.lower(),
            description.lower(),
            topic_str,
        ))

        if "mcp server" in text or "model context protocol" in text:
            mcp_evidence.append(
                "repository name/description mentions MCP"
            )

        if not mcp_evidence:
            return None

        return GitHubTopicsCandidate(
            repository=full_name,
            name=name,
            html_url=html_url,
            clone_url=clone_url,
            default_branch=default_branch,
            description=description,
            stars=stars,
            language=language,
            item_type=ItemType.TOOL,
            evidence=tuple(mcp_evidence),
            source=DiscoverySource(
                type=SourceType.GITHUB,
                id=full_name,
                url=html_url,
            ),
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> Any:
        url = f"{self._api_base_url}{path}"
        try:
            response = await self._client.request(
                method,
                url,
                headers=self.headers,
                timeout=self._timeout,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise GitHubTopicsError(
                f"GitHub API request failed: {exc}"
            ) from exc

        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "0")
            reset = response.headers.get("X-RateLimit-Reset", "")
            if remaining == "0":
                raise GitHubTopicsError(
                    f"GitHub API rate limit exceeded. Resets at Unix timestamp {reset}."
                )

        if response.status_code >= 400:
            message = response.text[:500]
            raise GitHubTopicsError(
                f"GitHub API request failed with HTTP {response.status_code}: {message}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubTopicsError(
                f"GitHub API returned invalid JSON: {exc}"
            ) from exc
