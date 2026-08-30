"""robots.txt-aware permission checks for targeted web extraction.

Implements CODEX_EXECUTION_PLAN.md rule #19: "Respect robots.txt and
applicable provider terms for web extraction." Absence of a robots.txt
(or a transport failure fetching it) is treated as the documented
default-allow case, so a single unreachable robots.txt fetch does not
block extraction outright - it fails open, the same "one source
failure must not fail the complete search" spirit as rule #21.
"""

from __future__ import annotations

import urllib.robotparser
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_USER_AGENT = "AgenticDiscoveryPlatformBot/1.0 (+https://example.invalid/bot)"
DEFAULT_TIMEOUT_SECONDS = 10.0


class RobotsChecker:
    """Fetches and caches robots.txt per-origin, then answers can-fetch."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._cache: dict[str, urllib.robotparser.RobotFileParser] = {}

    @staticmethod
    def _robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    async def is_allowed(self, url: str) -> bool:
        robots_url = self._robots_url(url)

        parser = self._cache.get(robots_url)
        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(await self._fetch_lines(robots_url))
            self._cache[robots_url] = parser

        return parser.can_fetch(self._user_agent, url)

    async def _fetch_lines(self, robots_url: str) -> list[str]:
        try:
            response = await self._client.get(
                robots_url,
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": self._user_agent},
            )
        except httpx.HTTPError:
            # Unreachable robots.txt: fail open (default-allow).
            return []

        if response.status_code >= 400:
            # No robots.txt published: default-allow.
            return []

        return response.text.splitlines()
