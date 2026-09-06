"""Awesome-list MCP server discovery adapter.

Parses community-curated Markdown lists (awesome-mcp-servers) to find
MCP server entries. Entries include GitHub URLs, descriptions, and
install commands extracted from the structured Markdown.

Sources:
  - https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md
  - https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md

No authentication required. Rate limits: standard GitHub raw content limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.models import DiscoverySource, ItemType, SourceType

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_RESULTS = 50


class AwesomeListError(RuntimeError):
    """Base error for awesome-list discovery failures."""


@dataclass(frozen=True)
class AwesomeListCandidate:
    """An MCP server entry parsed from an awesome-list."""

    name: str
    description: str
    html_url: str
    install_command: str
    category: str
    badge: str | None
    evidence: tuple[str, ...]
    source: DiscoverySource


# Known awesome-list URLs (both are public, no auth needed)
AWESOME_LIST_SOURCES = [
    "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md",
    "https://raw.githubusercontent.com/wong2/awesome-mcp-servers/main/README.md",
]


def _parse_github_url(raw: str) -> tuple[str, str]:
    """Split a raw URL that may include a trailing "tooltip" (space + quoted text).

    Input:  "https://github.com/user/repo \\"A tooltip\\""
    Output: ("https://github.com/user/repo", "A tooltip")

    Input:  "https://github.com/user/repo"
    Output: ("https://github.com/user/repo", "")
    """
    # Match: URL, then optional space+quoted tooltip
    m = re.search(r'^(https?://github\.com/[^"]+) *"(.*)"$', raw)
    if m:
        return m.group(1).strip(), m.group(2)
    return raw, ""


class AwesomeListAdapter:
    """Parse awesome-MCP-servers GitHub README files for MCP server entries."""

    # Regex to match Markdown links: [name](url) or [name](url "tooltip")
    # URL captures up to ')', tooltip is extracted separately from the URL string.
    _LINK_RE = re.compile(
        r'\[([^\]]{2,100})\]'
        r'\((https?://github\.com/[^)]+)\)'
        r'(?:\s+"([^"]*)")?'
    )

    # Regex to match install commands in backtick blocks
    _INSTALL_RE = re.compile(
        r"`(npm\s+install|npx\s+[-@a-z0-9/]+|pip\s+install|uvx?\s+[-@a-z0-9/]+|"
        r"docker\s+run|go\s+install)[^`]*`",
        re.IGNORECASE,
    )

    # Category headers in awesome lists
    _CATEGORY_RE = re.compile(
        r'^##?\s+(.+)$',
        re.MULTILINE,
    )

    # Badges that indicate official/status
    _BADGE_RE = re.compile(r'\[!?\[.*?\]\(.*?\)\]\(.*?\)')

    def __init__(
        self,
        sources: list[str] | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sources = sources or AWESOME_LIST_SOURCES
        self._timeout = timeout_seconds
        self._owns_client = httpx_client is None
        self._client = httpx_client or httpx.AsyncClient()

    async def __aenter__(self) -> "AwesomeListAdapter":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def discover(
        self,
        query: str,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[AwesomeListCandidate]:
        """Fetch and parse awesome-list READMEs, filter by query."""
        if not query.strip():
            raise AwesomeListError("awesome-list discovery query must not be empty")

        candidates: list[AwesomeListCandidate] = []
        query_lower = query.lower()

        for source_url in self._sources:
            try:
                entries = await self._fetch_and_parse(source_url)
            except AwesomeListError:
                continue

            for entry in entries:
                # Text match against name, description, category, install command
                text = " ".join((
                    entry.name.lower(),
                    entry.description.lower(),
                    entry.category.lower(),
                    entry.install_command.lower(),
                ))
                if query_lower in text:
                    candidates.append(entry)

        # Sort by name and dedupe by GitHub URL
        seen: set[str] = set()
        unique: list[AwesomeListCandidate] = []
        for c in candidates:
            if c.html_url not in seen:
                seen.add(c.html_url)
                unique.append(c)

        return unique[:max_results]

    async def _fetch_and_parse(
        self,
        url: str,
    ) -> list[AwesomeListCandidate]:
        """Fetch one awesome-list README and parse MCP server entries."""
        try:
            response = await self._client.get(url, timeout=self._timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AwesomeListError(f"Failed to fetch {url}: {exc}") from exc

        content = response.text
        source_name = self._extract_source_name(url)
        return self._parse(content, source_name, url)

    def _extract_source_name(self, url: str) -> str:
        """Extract a human-readable source name from the URL."""
        if "punkpeye" in url:
            return "awesome-mcp-servers (punkpeye)"
        elif "wong2" in url:
            return "awesome-mcp-servers (wong2)"
        return "awesome-mcp-servers"

    def _parse(
        self,
        content: str,
        source_name: str,
        source_url: str,
    ) -> list[AwesomeListCandidate]:
        """Parse Markdown content for MCP server entries."""
        candidates: list[AwesomeListCandidate] = []

        # Find current category context
        lines = content.split("\n")
        current_category = "General"

        # Process lines
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Update current category on header lines
            category_match = self._CATEGORY_RE.match(stripped)
            if category_match:
                current_category = category_match.group(1).strip()
                i += 1
                continue

            # Skip non-list items
            if not stripped.startswith("- ") and not stripped.startswith("* "):
                i += 1
                continue

            # Extract the list item text
            item_text = stripped[2:].strip()

            # Try to find a GitHub link in this line or following lines
            # (links can span multiple lines in markdown)
            full_text = item_text
            look_ahead = 0
            while (
                i + 1 + look_ahead < len(lines)
                and not lines[i + 1 + look_ahead].strip().startswith("- ")
                and not lines[i + 1 + look_ahead].strip().startswith("* ")
                and not lines[i + 1 + look_ahead].strip().startswith("##")
            ):
                full_text += " " + lines[i + 1 + look_ahead].strip()
                look_ahead += 1

            # Find all GitHub links in this entry
            links = list(self._LINK_RE.finditer(full_text))
            if not links:
                i += 1
                continue

            for link_match in links:
                name = link_match.group(1).strip()
                url_raw = link_match.group(2).strip()
                # The regex URL pattern `[^)]+` may capture a trailing "tooltip" if
                # present. Extract and remove it here.
                url, tooltip = _parse_github_url(url_raw)

                if not name or not url or "github.com" not in url:
                    continue

                # Extract description: text before the link
                desc_start = full_text.find(link_match.group(0))
                description = full_text[:desc_start].strip()
                # Clean up description: remove badge markup, extra punctuation
                description = self._BADGE_RE.sub("", description)
                description = description.strip(" -:").strip()
                if not description:
                    description = tooltip or ""

                # Extract install command from surrounding context
                install_cmd = self._extract_install_command(full_text)

                # Clean name: remove badge markup
                name = self._BADGE_RE.sub("", name).strip()

                candidates.append(AwesomeListCandidate(
                    name=name,
                    description=description[:300] if description else "",
                    html_url=url,
                    install_command=install_cmd,
                    category=current_category,
                    badge=None,
                    evidence=(
                        f"listed in {source_name} under category '{current_category}'",
                    ),
                    source=DiscoverySource(
                        type=SourceType.CONFIGURED,
                        id=f"awesome:{url.rstrip('/')}",
                        url=url,
                        provider=source_name,
                    ),
                ))

            i += 1

        return candidates

    def _extract_install_command(self, text: str) -> str:
        """Extract an install/run command from text."""
        match = self._INSTALL_RE.search(text)
        if match:
            # Extract the backtick-wrapped content
            cmd_match = re.search(r'`([^`]+)`', match.group(0))
            if cmd_match:
                return cmd_match.group(1).strip()
        return ""
