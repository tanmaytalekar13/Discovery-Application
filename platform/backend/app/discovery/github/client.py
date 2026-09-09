"""GitHub discovery adapter for Phase 04.

GitHub is a discovery source, not an MCP server. This adapter uses the
GitHub REST API to find repository candidates. A candidate is untrusted
until the existing MCP/A2A protocol resolver validates it.
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 20
DEFAULT_MAX_CONCURRENT = 4

# These words describe the protocol, not the service the user is looking for.
# Keeping them out of the service portion of a GitHub query makes
# ``google-calendar`` and ``Google Calendar MCP server`` converge on the same
# repository search, while adding one MCP constraint below.
_QUERY_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_PROTOCOL_QUERY_TERMS = {
    "mcp",
    "model",
    "context",
    "protocol",
    "server",
    "servers",
    "tool",
    "tools",
}


class GitHubDiscoveryError(RuntimeError):
    """Base error for GitHub discovery failures."""


class GitHubRateLimitError(GitHubDiscoveryError):
    """Raised when GitHub rate-limits the request."""


class GitHubAPIError(GitHubDiscoveryError):
    """Raised for non-success GitHub API responses."""


def github_mcp_search_query(query: str) -> str:
    """Build a GitHub repository query that preserves service intent.

    GitHub treats whitespace-separated terms as AND terms.  Normalising hyphens
    and removing protocol boilerplate means all supported query spellings use
    the same service terms, and the explicit ``mcp`` term prevents generic
    Google/Slack repositories from consuming the result window.
    """
    tokens = _QUERY_TOKEN_PATTERN.findall(query.lower())
    service_terms = [term for term in tokens if term not in _PROTOCOL_QUERY_TERMS]
    return " ".join([*service_terms, "mcp"]) if service_terms else "mcp"


@dataclass(frozen=True)
class GitHubCandidate:
    """Untrusted repository candidate discovered through GitHub."""

    repository: str
    name: str
    html_url: str
    clone_url: str
    default_branch: str | None
    description: str
    item_type: ItemType
    evidence: tuple[str, ...]
    source: DiscoverySource
    readme: str = ""
    root_entries: tuple[dict[str, Any], ...] = ()


class GitHubDiscoveryAdapter:
    """Search GitHub repositories and classify protocol candidates."""

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

    async def __aenter__(self) -> "GitHubDiscoveryAdapter":
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
    ) -> list[GitHubCandidate]:
        """Search repositories and return positively classified candidates."""
        if not query.strip():
            raise ValueError("GitHub discovery query must not be empty")

        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        # Ask GitHub for a wider *retrieval* window than the requested result
        # count. GitHub's popularity-biased API ordering must not decide which
        # low-star but relevant server gets a chance to be protocol classified.
        retrieval_limit = min(100, max(max_results, max_results * 3, 30))
        payload = await self._request(
            "GET",
            "/search/repositories",
            params={
                "q": github_mcp_search_query(query),
                "per_page": retrieval_limit,
                "page": 1,
            },
        )

        repositories = payload.get("items", [])

        if not isinstance(repositories, list):
            raise GitHubAPIError("GitHub search response has an invalid 'items' field")

        semaphore = asyncio.Semaphore(self._max_concurrent)

        async def classify(
            repo: dict[str, Any],
        ) -> GitHubCandidate | None:
            async with semaphore:
                return await self._classify_repository(repo)

        results = await asyncio.gather(
            *(classify(repo) for repo in repositories[:retrieval_limit])
        )

        # ``repositories`` remains GitHub-ranked only for retrieval. Returning
        # the first classified matches keeps the caller's limit intact; final
        # cross-source relevance ranking happens after catalog merging.
        return [candidate for candidate in results if candidate is not None][
            :max_results
        ]

    async def discover_mcp(
        self,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[GitHubCandidate]:
        return await self.discover(
            '"MCP server" OR "Model Context Protocol"',
            max_results,
        )

    async def discover_a2a(
        self,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[GitHubCandidate]:
        return await self.discover(
            '"A2A agent" OR "Agent Card"',
            max_results,
        )

    async def _classify_repository(
        self,
        repo: dict[str, Any],
    ) -> GitHubCandidate | None:
        full_name = repo.get("full_name")

        if not isinstance(full_name, str) or "/" not in full_name:
            return None

        readme = await self._fetch_readme(full_name)
        root_entries = await self._fetch_root_entries(full_name)

        classification = self.classify_repository(
            repo,
            readme,
            root_entries,
        )

        if classification is None:
            return None

        item_type, evidence = classification

        return GitHubCandidate(
            repository=full_name,
            name=str(repo.get("name") or full_name.rsplit("/", 1)[-1]),
            html_url=str(repo.get("html_url") or ""),
            clone_url=str(repo.get("clone_url") or ""),
            default_branch=repo.get("default_branch"),
            description=str(repo.get("description") or ""),
            item_type=item_type,
            evidence=tuple(evidence),
            source=DiscoverySource(
                type=SourceType.GITHUB,
                id=full_name,
                url=repo.get("html_url"),
            ),
            readme=readme,
            root_entries=tuple(root_entries),
        )

    @staticmethod
    def classify_repository(
        repo: dict[str, Any],
        readme: str = "",
        root_entries: list[dict[str, Any]] | None = None,
    ) -> tuple[ItemType, list[str]] | None:
        """Classify with protocol evidence and reject weak false positives."""

        root_entries = root_entries or []

        name = str(repo.get("name") or "").lower()
        description = str(repo.get("description") or "").lower()

        topics = " ".join(str(topic).lower() for topic in repo.get("topics", []) or [])

        text = " ".join(
            (
                name,
                description,
                topics,
                readme.lower(),
            )
        )

        paths = [str(entry.get("path") or "").lower() for entry in root_entries]

        mcp_evidence: list[str] = []
        a2a_evidence: list[str] = []

        # ---------------------------------------------------------
        # MCP evidence
        # ---------------------------------------------------------

        if "mcp server" in text:
            mcp_evidence.append(
                "repository metadata/documentation "
                "explicitly identifies an MCP server"
            )

        if any(
            "mcp" in path and path.endswith((".json", ".yaml", ".yml", ".toml", ".md"))
            for path in paths
        ):
            mcp_evidence.append(
                "repository contains an MCP-related " "configuration/documentation file"
            )

        if any(
            marker in text
            for marker in (
                "tools/list",
                "tools/call",
                "mcp.server",
                "@mcp.tool",
            )
        ):
            mcp_evidence.append("documentation contains MCP protocol/tool evidence")

        # ---------------------------------------------------------
        # A2A evidence
        # ---------------------------------------------------------

        if "agent card" in text or "a2a agent" in text or "agent2agent" in text:
            a2a_evidence.append(
                "repository metadata/documentation " "mentions A2A/Agent Card"
            )

        if any(
            "agent-card" in path or "agent_card" in path or "well-known" in path
            for path in paths
        ):
            a2a_evidence.append("repository contains Agent Card/.well-known evidence")

        if any(
            marker in text
            for marker in (
                "message/send",
                "a2a protocol",
                "protocolversion",
            )
        ):
            a2a_evidence.append("documentation contains A2A protocol evidence")

        # ---------------------------------------------------------
        # Final classification
        # ---------------------------------------------------------

        if mcp_evidence and not a2a_evidence:
            return ItemType.TOOL, mcp_evidence

        if a2a_evidence and not mcp_evidence:
            return ItemType.AGENT, a2a_evidence

        if mcp_evidence and a2a_evidence:
            return (
                ItemType.TOOL,
                mcp_evidence + ["repository also contains A2A evidence"],
            )

        return None

    async def _fetch_readme(
        self,
        full_name: str,
    ) -> str:
        try:
            payload = await self._request(
                "GET",
                f"/repos/{full_name}/readme",
            )
        except GitHubAPIError as exc:
            if "HTTP 404" in str(exc):
                return ""

            raise

        content = payload.get("content", "")

        if not content:
            return ""

        try:
            return base64.b64decode(content).decode(
                "utf-8",
                errors="replace",
            )
        except (ValueError, UnicodeError):
            return ""

    async def _fetch_root_entries(
        self,
        full_name: str,
    ) -> list[dict[str, Any]]:
        payload = await self._request(
            "GET",
            f"/repos/{full_name}/contents",
        )

        if not isinstance(payload, list):
            return []

        return [entry for entry in payload if isinstance(entry, dict)]

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
            raise GitHubAPIError(f"GitHub request failed: {exc}") from exc

        if response.status_code in (403, 429):
            remaining = response.headers.get("X-RateLimit-Remaining")

            if response.status_code == 429 or remaining == "0":
                retry_after = response.headers.get(
                    "Retry-After"
                ) or response.headers.get("X-RateLimit-Reset")

                suffix = f" Retry after: {retry_after}." if retry_after else ""

                raise GitHubRateLimitError(
                    "GitHub API rate limit reached "
                    f"(HTTP {response.status_code}).{suffix}"
                )

        if response.status_code >= 400:
            message = response.text[:500]

            raise GitHubAPIError(
                "GitHub API request failed with "
                f"HTTP {response.status_code}: {message}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise GitHubAPIError("GitHub API returned invalid JSON") from exc
