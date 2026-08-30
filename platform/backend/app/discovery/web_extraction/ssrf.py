"""SSRF protection for targeted web extraction (Phase 08).

Per CODEX_EXECUTION_PLAN.md Section 23 ("Web Discovery Security"),
every external URL visited by the platform must pass:

    URL validation -> scheme validation -> hostname validation ->
    DNS/IP policy -> private/internal target blocking -> redirect
    validation -> timeout -> response-size limit -> content-type
    validation -> safe extraction

This module implements the first part of that pipeline (everything up
through "private/internal target blocking"). Redirect validation,
timeouts, response-size limits and content-type validation live in
`client.py`, which re-runs this same guard on every redirect hop.

Deliberately resolves DNS itself instead of trusting the URL's literal
hostname: a hostname that only *resolves* to a private/loopback/
link-local address (including the 169.254.169.254 cloud metadata
address, which falls under "link-local") must be blocked exactly like
an IP literal typed directly into the URL would be. This defends
against DNS-rebinding style bypasses, not just naive literal checks.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Hostnames that are unambiguously internal regardless of what they
# resolve to (or even if they fail to resolve at all).
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal"})
_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal")


class SSRFViolation(RuntimeError):
    """Raised when a URL fails SSRF/target-safety validation."""


class SSRFGuard:
    """Validates a URL is safe to fetch before any request is made."""

    def __init__(self, resolver=None) -> None:
        # Injectable for tests; defaults to real DNS resolution.
        self._resolver = resolver or self._default_resolve

    @staticmethod
    def _default_resolve(host: str) -> list[str]:
        infos = socket.getaddrinfo(host, None)
        return sorted({info[4][0] for info in infos})

    async def validate(self, url: str) -> str:
        """Validate `url`, returning its hostname, or raise `SSRFViolation`."""
        parts = urlsplit(url)

        scheme = parts.scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            raise SSRFViolation(
                f"Unsupported URL scheme {parts.scheme!r}; "
                f"only {sorted(ALLOWED_SCHEMES)} are allowed"
            )

        host = parts.hostname
        if not host:
            raise SSRFViolation(f"URL has no hostname: {url!r}")

        host_lower = host.lower()
        if host_lower in _BLOCKED_HOSTNAMES or host_lower.endswith(
            _BLOCKED_HOST_SUFFIXES
        ):
            raise SSRFViolation(f"Blocked internal hostname: {host}")

        # An IP literal in the URL still needs to be checked directly
        # (getaddrinfo would just hand it back unchanged, but doing
        # the ipaddress parse first avoids relying on that).
        literal_ip = self._try_parse_ip(host)
        candidate_ips: list[str]
        if literal_ip is not None:
            candidate_ips = [str(literal_ip)]
        else:
            try:
                candidate_ips = await asyncio.to_thread(self._resolver, host)
            except OSError as exc:
                raise SSRFViolation(
                    f"DNS resolution failed for host {host!r}: {exc}"
                ) from exc

        if not candidate_ips:
            raise SSRFViolation(
                f"DNS resolution returned no addresses for host {host!r}"
            )

        for raw_ip in candidate_ips:
            self._check_ip(host, raw_ip)

        return host

    @staticmethod
    def _try_parse_ip(
        host: str,
    ) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    @staticmethod
    def _check_ip(host: str, raw_ip: str) -> None:
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError as exc:
            raise SSRFViolation(
                f"Host {host!r} resolved to an unparseable address: {raw_ip!r}"
            ) from exc

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # covers 169.254.169.254 cloud metadata
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise SSRFViolation(
                f"Blocked non-public address for host {host!r}: {raw_ip}"
            )
