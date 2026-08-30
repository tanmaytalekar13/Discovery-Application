"""Targeted web extraction discovery adapter (Phase 08).

Per CODEX_EXECUTION_PLAN.md Section 6 ("Discovery strategy" ->
Additional sources for both MCP and A2A, item 5/6: "Targeted web
extraction"), this adapter safely fetches a *specific* URL already
surfaced by another discovery source (GitHub, web search, a registry
- see Section 2's product-loop diagram, where "Targeted Extraction"
sits downstream of "Web Search") and classifies it as a candidate
using the page's own text.

This adapter never crawls: it visits exactly the one URL it is given,
never follows links discovered inside the page, and never executes
any downloaded JavaScript or source code (Section 23: "Never execute
downloaded JavaScript or source code."). A page found here is still
only a candidate (rule #8: "A web search result is a candidate, not
trusted protocol metadata" applies equally to an arbitrary web page)
and must be resolved through the existing MCP/A2A protocol paths
(Phase 02/03) before it can enter the trusted catalog.

Every fetch goes through, in order (Section 23):

    SSRFGuard.validate()      scheme / hostname / DNS-IP policy
    RobotsChecker.is_allowed() robots.txt (rule #19)
    manual redirect loop       each hop re-validated from the top
    timeout + streamed read    response-size limit enforced while streaming
    content-type allowlist     reject non-text/html-ish responses
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

from app.models import DiscoverySource, ItemType, SourceType

from .robots import DEFAULT_USER_AGENT, RobotsChecker
from .ssrf import SSRFGuard, SSRFViolation

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB

# Deliberately conservative: only content types this adapter knows how
# to safely turn into inspectable text. Anything else (binaries,
# archives, executables, etc.) is rejected rather than guessed at.
ALLOWED_CONTENT_TYPES = frozenset({"text/html", "text/plain", "application/xhtml+xml"})


class WebExtractionError(RuntimeError):
    """Base error for targeted web extraction failures."""


class WebExtractionBlockedError(WebExtractionError):
    """Raised when a URL is blocked by SSRF policy or robots.txt."""


class WebExtractionResponseError(WebExtractionError):
    """Raised for failed, oversized, mistyped, or timed-out responses."""


class _TextExtractor(HTMLParser):
    """Minimal, dependency-free HTML -> (title, text) extractor.

    Only inspects text content; never evaluates `<script>` bodies as
    code (Section 23's "Never execute downloaded JavaScript or source
    code" - this parser cannot execute anything, it only reads tag
    structure to skip script/style text when collecting page text).
    """

    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title_parts.append(data)
        else:
            self._text_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title_parts).split())

    @property
    def text(self) -> str:
        return " ".join(" ".join(self._text_parts).split())


@dataclass(frozen=True)
class WebExtractionCandidate:
    """Untrusted candidate discovered through targeted web extraction."""

    title: str
    url: str
    text_excerpt: str
    item_type: ItemType
    evidence: tuple[str, ...]
    source: DiscoverySource
    content_type: str


class WebExtractionAdapter:
    """Safely fetch one URL at a time and classify it as a candidate."""

    def __init__(
        self,
        httpx_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        user_agent: str = DEFAULT_USER_AGENT,
        ssrf_guard: SSRFGuard | None = None,
        respect_robots: bool = True,
        robots_checker: RobotsChecker | None = None,
    ) -> None:
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()
        self._timeout = timeout_seconds
        self._max_redirects = max(0, max_redirects)
        self._max_response_bytes = max_response_bytes
        self._user_agent = user_agent
        self._guard = ssrf_guard or SSRFGuard()
        self._respect_robots = respect_robots
        self._robots = robots_checker or RobotsChecker(
            self._client, user_agent, timeout_seconds
        )

    async def __aenter__(self) -> "WebExtractionAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def extract(self, url: str) -> WebExtractionCandidate | None:
        """Fetch and classify one URL; `None` if it's not a candidate.

        Raises `WebExtractionBlockedError` (SSRF/robots) or
        `WebExtractionResponseError` (transport/size/content-type) so
        a caller iterating many URLs can isolate one failure from the
        rest - see `extract_many` and rule #21.
        """
        content_type, body = await self._fetch_text(url)

        extractor = _TextExtractor()
        extractor.feed(body)

        classification = self.classify_page(
            title=extractor.title,
            url=url,
            text=extractor.text,
        )
        if classification is None:
            return None

        item_type, evidence = classification

        return WebExtractionCandidate(
            title=extractor.title or url,
            url=url,
            text_excerpt=extractor.text[:500],
            item_type=item_type,
            evidence=tuple(evidence),
            source=DiscoverySource(
                type=SourceType.WEB_PAGE,
                id=url,
                url=url,
            ),
            content_type=content_type,
        )

    async def extract_many(
        self,
        urls: list[str],
    ) -> list[WebExtractionCandidate]:
        """Extract each URL independently.

        One URL's `WebExtractionError` (blocked, oversized, timed
        out, ...) is swallowed and skipped rather than aborting the
        remaining URLs (rule #21: "One discovery-source failure must
        not fail the complete search").
        """
        candidates: list[WebExtractionCandidate] = []
        for url in urls:
            try:
                candidate = await self.extract(url)
            except WebExtractionError:
                continue
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    async def _fetch_text(self, url: str) -> tuple[str, str]:
        current_url = url
        redirects_followed = 0

        while True:
            try:
                await self._guard.validate(current_url)
            except SSRFViolation as exc:
                raise WebExtractionBlockedError(str(exc)) from exc

            if self._respect_robots:
                allowed = await self._robots.is_allowed(current_url)
                if not allowed:
                    raise WebExtractionBlockedError(
                        f"robots.txt disallows fetching {current_url}"
                    )

            try:
                async with self._client.stream(
                    "GET",
                    current_url,
                    timeout=self._timeout,
                    follow_redirects=False,
                    headers={
                        "User-Agent": self._user_agent,
                        "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1",
                    },
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WebExtractionResponseError(
                                f"Redirect from {current_url} had no " "Location header"
                            )

                        redirects_followed += 1
                        if redirects_followed > self._max_redirects:
                            raise WebExtractionResponseError(
                                f"Too many redirects starting from {url}"
                            )

                        # Re-validate the new hop from the top of the
                        # loop (rule #17: "Validate redirects.") -
                        # a redirect can point anywhere, including an
                        # internal address.
                        current_url = urljoin(current_url, location)
                        continue

                    if response.status_code >= 400:
                        raise WebExtractionResponseError(
                            f"Request to {current_url} failed with "
                            f"HTTP {response.status_code}"
                        )

                    content_type = (
                        response.headers.get("content-type", "")
                        .split(";")[0]
                        .strip()
                        .lower()
                    )
                    if content_type and content_type not in ALLOWED_CONTENT_TYPES:
                        raise WebExtractionResponseError(
                            "Unsupported content-type for "
                            f"{current_url}: {content_type}"
                        )

                    content_length = response.headers.get("content-length")
                    if (
                        content_length is not None
                        and content_length.isdigit()
                        and int(content_length) > self._max_response_bytes
                    ):
                        raise WebExtractionResponseError(
                            f"Response for {current_url} exceeds max "
                            f"size ({self._max_response_bytes} bytes)"
                        )

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_response_bytes:
                            raise WebExtractionResponseError(
                                f"Response for {current_url} exceeded "
                                "max size while streaming"
                            )
                        chunks.append(chunk)

                    body = b"".join(chunks).decode("utf-8", errors="replace")
                    return content_type or "text/html", body
            except httpx.HTTPError as exc:
                raise WebExtractionResponseError(
                    f"Request to {current_url} failed: {exc}"
                ) from exc

    @staticmethod
    def classify_page(
        title: str,
        url: str,
        text: str,
    ) -> tuple[ItemType, list[str]] | None:
        """Classify using extracted page text only.

        Mirrors `GitHubDiscoveryAdapter.classify_repository` and
        `WebSearchDiscoveryAdapter.classify_search_result`: looks for
        the same concrete protocol markers, applied to a fetched
        page's own title/text/URL. Per rule #11 and Section 9, this
        never fabricates a schema/Agent Card - it only ever returns a
        classification plus evidence for further protocol resolution.
        """
        combined = " ".join((title, text)).lower()
        url_lower = url.lower()

        mcp_evidence: list[str] = []
        a2a_evidence: list[str] = []

        if "mcp server" in combined or "model context protocol" in combined:
            mcp_evidence.append("page text explicitly identifies an MCP server")

        if any(
            marker in combined
            for marker in ("tools/list", "tools/call", "mcp.server", "@mcp.tool")
        ):
            mcp_evidence.append("page text contains MCP protocol/tool evidence")

        if "modelcontextprotocol.io" in url_lower or "/mcp" in url_lower:
            mcp_evidence.append("page URL references MCP")

        if (
            "agent card" in combined
            or "a2a agent" in combined
            or "agent2agent" in combined
        ):
            a2a_evidence.append("page text mentions A2A/Agent Card")

        if any(
            marker in combined
            for marker in ("message/send", "a2a protocol", "protocolversion")
        ):
            a2a_evidence.append("page text contains A2A protocol evidence")

        if "agent-card" in url_lower or "well-known" in url_lower:
            a2a_evidence.append("page URL references an Agent Card")

        if mcp_evidence and not a2a_evidence:
            return ItemType.TOOL, mcp_evidence

        if a2a_evidence and not mcp_evidence:
            return ItemType.AGENT, a2a_evidence

        if mcp_evidence and a2a_evidence:
            return (
                ItemType.TOOL,
                mcp_evidence + ["page also contains A2A evidence"],
            )

        return None
