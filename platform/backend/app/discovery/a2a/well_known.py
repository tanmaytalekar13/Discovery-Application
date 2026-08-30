"""A2A discovery: Well-Known and direct Agent Card resolution (Phase 06).

Per CODEX_EXECUTION_PLAN.md Section 6 (A2A discovery sources ->
Mandatory/core):

    4. `.well-known/agent-card.json` where applicable.
    5. Direct configured Agent Card URLs.

and Section 39 (Phase 06 Definition of Done):

    - A2A registry works;
    - Well-Known Agent Card resolution works;
    - Agent Cards validate.

This module turns a *candidate host or configured URL* - discovered by
another adapter (e.g. GitHub, Section 4 acceptance criteria) or
supplied directly through configuration - into a resolved A2A agent
candidate by attempting to fetch and validate its Agent Card, reusing
the Phase 03 protocol client (`app.discovery.a2a.client.A2AClient`) so
well-known resolution never re-implements Agent Card fetch/validation.

It never fabricates a card: a host that does not serve one is simply
not an A2A candidate, and only genuine transport/protocol failures are
surfaced as `error` on the result (rule #21 - one source failure must
not fail the whole search).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.discovery.a2a.client import (
    DEFAULT_AGENT_CARD_PATH,
    A2AClient,
    A2AResolutionResult,
)
from app.discovery.a2a.errors import A2AResolutionError
from app.discovery.a2a.schema import normalize_agent_card
from app.models import DiscoverySource, SourceType

DEFAULT_MAX_CONCURRENT = 4


@dataclass(frozen=True)
class AgentCardProbeResult:
    """Outcome of probing one URL/host for a real Agent Card."""

    target: str
    found: bool
    resolution: A2AResolutionResult | None
    error: str | None
    source: DiscoverySource


async def _resolve_agent_card_candidate(
    target: str,
    *,
    source_type: SourceType,
    card_path: str,
    httpx_client: httpx.AsyncClient | None,
) -> AgentCardProbeResult:
    display_url = f"{target.rstrip('/')}{card_path}" if card_path else target
    source = DiscoverySource(
        type=source_type,
        id=target,
        url=display_url,
    )

    try:
        async with A2AClient(base_url=target, httpx_client=httpx_client) as client:
            raw_card = await client.fetch_agent_card(card_path=card_path)
    except A2AResolutionError as exc:
        return AgentCardProbeResult(
            target=target,
            found=False,
            resolution=None,
            error=str(exc),
            source=source,
        )

    endpoint = raw_card["url"]
    agent_metadata = normalize_agent_card(endpoint=endpoint, raw_card=raw_card)
    resolution = A2AResolutionResult(
        endpoint=endpoint,
        protocol_version=raw_card.get("protocolVersion"),
        raw_agent_card=raw_card,
        agent=agent_metadata,
    )
    return AgentCardProbeResult(
        target=target,
        found=True,
        resolution=resolution,
        error=None,
        source=source,
    )


async def probe_well_known_agent_card(
    host: str,
    *,
    card_path: str = DEFAULT_AGENT_CARD_PATH,
    httpx_client: httpx.AsyncClient | None = None,
) -> AgentCardProbeResult:
    """Probe a single candidate host for a real Agent Card.

    `host` must be a full base URL (e.g. ``https://example.com``), not
    a bare hostname - scheme selection is a caller/discovery-source
    concern, not this module's.
    """
    return await _resolve_agent_card_candidate(
        host,
        source_type=SourceType.WELL_KNOWN,
        card_path=card_path,
        httpx_client=httpx_client,
    )


async def discover_well_known_agents(
    hosts: list[str],
    *,
    card_path: str = DEFAULT_AGENT_CARD_PATH,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[AgentCardProbeResult]:
    """Probe many candidate hosts concurrently for Agent Cards.

    Bounded concurrency mirrors `GitHubDiscoveryAdapter` (Phase 04):
    one slow or unreachable host must not block or fail discovery of
    the rest of the batch.
    """
    if not hosts:
        return []

    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def _bounded(host: str) -> AgentCardProbeResult:
        async with semaphore:
            return await probe_well_known_agent_card(host, card_path=card_path)

    return await asyncio.gather(*(_bounded(host) for host in hosts))


async def resolve_configured_agent_card(
    url: str,
    *,
    httpx_client: httpx.AsyncClient | None = None,
) -> AgentCardProbeResult:
    """Resolve one directly configured Agent Card URL (Section 6, item 5).

    `url` is the full Agent Card URL, not a host: it is fetched
    directly rather than joined with the well-known path.
    """
    return await _resolve_agent_card_candidate(
        url,
        source_type=SourceType.CONFIGURED,
        card_path="",
        httpx_client=httpx_client,
    )
