"""Web search discovery adapter (Phase 07).

Per CODEX_EXECUTION_PLAN.md Section 6 (discovery sources -> Mandatory/core,
item 3 for both MCP and A2A): "Web search."

A web search result is a candidate, not trusted protocol metadata (rule
#8). This module only ever classifies candidates from the *search
result* itself (title/URL/snippet) - it never fetches or extracts the
linked page's content. Visiting an arbitrary discovered URL to extract
its content is "Targeted Extraction" (Section 2's product-loop diagram
places it as a distinct downstream step from "Web Search") and belongs
to Phase 08, which is where SSRF protection (Section 23) is added. A
candidate produced here still must be resolved through the existing
MCP/A2A protocol paths (Phase 02/03) before it can enter the trusted
catalog.

There is no single canonical "the" web search API the way MCP has an
official registry, so this adapter is split into:

    WebSearchProvider   - a small interface any search backend can
                           implement (`search(query, max_results)`).
    FirecrawlWebSearchProvider
                        - one concrete, real implementation against
                           the Firecrawl Search API
                           (https://api.firecrawl.dev/v2/search),
                           the configured default.
    WebSearchDiscoveryAdapter
                        - provider-agnostic classification/discovery,
                           mirroring `GitHubDiscoveryAdapter`
                           (Phase 04): takes any `WebSearchProvider`
                           and turns its raw results into classified
                           `WebSearchCandidate`s.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_FIRECRAWL_SEARCH_API = "https://api.firecrawl.dev"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULTS = 20

DEFAULT_MCP_SEARCH_QUERY = '"MCP server" OR "Model Context Protocol" tool'
DEFAULT_A2A_SEARCH_QUERY = '"A2A agent" OR "Agent Card" protocol'


class WebSearchError(RuntimeError):
    """Base error for web search discovery failures."""


class WebSearchRateLimitError(WebSearchError):
    """Raised when the configured search provider rate-limits the client."""


class WebSearchAPIError(WebSearchError):
    """Raised for invalid or unsuccessful search provider responses."""


@dataclass(frozen=True)
class RawSearchResult:
    """One untouched result as returned by a search provider."""

    title: str
    url: str
    snippet: str
    raw_result: dict[str, Any]


class WebSearchProvider(ABC):
    """Interface a concrete web search backend must implement.

    Keeping this as a small interface (Section 7 - "Do not put
    provider-specific logic into the Search Orchestrator. Use
    interfaces.") means a future provider (Bing, Google Programmable
    Search, SerpAPI, ...) can be swapped in without touching
    `WebSearchDiscoveryAdapter`'s classification logic.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[RawSearchResult]:
        """Return raw, unclassified search results for `query`."""

    async def __aenter__(self) -> "WebSearchProvider":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FirecrawlWebSearchProvider(WebSearchProvider):
    """Real `WebSearchProvider` backed by the Firecrawl Search API.

    Documented contract used here (https://docs.firecrawl.dev/api-reference/endpoint/search):
        POST {base_url}/v2/search
        Header: Authorization: Bearer {api_key}
        Body: {"query": "...", "limit": n}
        Response: {"success": true, "data": [{"title", "url", "description"}, ...]}

    Note this is a POST with a JSON body (unlike Brave's GET+querystring),
    and the api_key goes in an `Authorization: Bearer` header, not
    `X-Subscription-Token`.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_FIRECRAWL_SEARCH_API,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/v2/search"

    async def search(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[RawSearchResult]:
        if not query.strip():
            raise ValueError("web search query must not be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        try:
            response = await self._client.post(
                self.endpoint,
                json={
                    "query": query,
                    "limit": min(max_results, 20),
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._api_key}",
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise WebSearchAPIError(f"Web search request failed: {exc}") from exc

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            suffix = f" Retry after: {retry_after}." if retry_after else ""
            raise WebSearchRateLimitError(
                f"Web search rate limit reached (HTTP 429).{suffix}"
            )

        if response.status_code >= 400:
            raise WebSearchAPIError(
                "Web search request failed with "
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise WebSearchAPIError(
                "Web search provider returned invalid JSON"
            ) from exc

        if isinstance(payload, dict) and payload.get("success") is False:
            raise WebSearchAPIError(
                f"Web search provider reported failure: {payload.get('error') or payload}"
            )

        entries = payload.get("data") if isinstance(payload, dict) else None

        if not isinstance(entries, list):
            raise WebSearchAPIError(
                "Web search response has an invalid 'data' field"
            )

        results: list[RawSearchResult] = []
        for entry in entries[:max_results]:
            if not isinstance(entry, dict):
                continue

            url = entry.get("url")
            title = entry.get("title")
            if not isinstance(url, str) or not url:
                continue
            if not isinstance(title, str) or not title:
                continue

            results.append(
                RawSearchResult(
                    title=title,
                    url=url,
                    snippet=str(entry.get("description") or ""),
                    raw_result=dict(entry),
                )
            )

        return results


@dataclass(frozen=True)
class WebSearchCandidate:
    """Untrusted candidate discovered through web search."""

    title: str
    url: str
    snippet: str
    item_type: ItemType
    evidence: tuple[str, ...]
    source: DiscoverySource
    raw_result: dict[str, Any]


class WebSearchDiscoveryAdapter:
    """Search the web through a configured provider and classify candidates.

    A raw search result is only kept as a candidate when the result's
    own title/URL/snippet text gives concrete protocol evidence -
    matching GitHub's Phase 04 "reject weak false positives" behavior.
    No page content is fetched to make this decision (see module
    docstring).
    """

    def __init__(self, provider: WebSearchProvider) -> None:
        self._provider = provider

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[WebSearchCandidate]:
        if not query.strip():
            raise ValueError("web search discovery query must not be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least 1")

        raw_results = await self._provider.search(query, max_results)

        candidates: list[WebSearchCandidate] = []
        for result in raw_results:
            classification = self.classify_search_result(
                title=result.title,
                url=result.url,
                snippet=result.snippet,
            )
            if classification is None:
                continue

            item_type, evidence = classification
            candidates.append(
                WebSearchCandidate(
                    title=result.title,
                    url=result.url,
                    snippet=result.snippet,
                    item_type=item_type,
                    evidence=tuple(evidence),
                    source=DiscoverySource(
                        type=SourceType.WEB_SEARCH,
                        id=result.url,
                        url=result.url,
                    ),
                    raw_result=result.raw_result,
                )
            )

        return candidates

    async def discover_mcp(
        self,
        query: str = DEFAULT_MCP_SEARCH_QUERY,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[WebSearchCandidate]:
        return await self.discover(query, max_results)

    async def discover_a2a(
        self,
        query: str = DEFAULT_A2A_SEARCH_QUERY,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[WebSearchCandidate]:
        return await self.discover(query, max_results)

    @staticmethod
    def classify_search_result(
        title: str,
        url: str,
        snippet: str,
    ) -> tuple[ItemType, list[str]] | None:
        """Classify a search result using only its own text.

        Deliberately conservative: a bare keyword match on "mcp" or
        "agent" is not evidence (too many false positives - blog
        posts, unrelated products, etc), so this looks for the same
        kind of concrete protocol markers `GitHubDiscoveryAdapter`
        requires, applied to the title/URL/snippet instead of a
        fetched README.
        """
        text = " ".join((title, snippet)).lower()
        url_lower = url.lower()

        mcp_evidence: list[str] = []
        a2a_evidence: list[str] = []

        if "mcp server" in text or "model context protocol" in text:
            mcp_evidence.append(
                "result title/snippet explicitly identifies an MCP server"
            )

        if any(
            marker in text
            for marker in ("tools/list", "tools/call", "mcp.server", "@mcp.tool")
        ):
            mcp_evidence.append(
                "result title/snippet contains MCP protocol/tool evidence"
            )

        if "modelcontextprotocol.io" in url_lower or "/mcp" in url_lower:
            mcp_evidence.append("result URL references MCP")

        if "agent card" in text or "a2a agent" in text or "agent2agent" in text:
            a2a_evidence.append("result title/snippet mentions A2A/Agent Card")

        if any(
            marker in text
            for marker in ("message/send", "a2a protocol", "protocolversion")
        ):
            a2a_evidence.append("result title/snippet contains A2A protocol evidence")

        if "agent-card" in url_lower or "well-known" in url_lower:
            a2a_evidence.append("result URL references an Agent Card")

        if mcp_evidence and not a2a_evidence:
            return ItemType.TOOL, mcp_evidence

        if a2a_evidence and not mcp_evidence:
            return ItemType.AGENT, a2a_evidence

        if mcp_evidence and a2a_evidence:
            return (
                ItemType.TOOL,
                mcp_evidence + ["result also contains A2A evidence"],
            )

        return None
