"""Common candidate model shared across discovery-source adapters (Phase 09).

Per CODEX_EXECUTION_PLAN.md Section 7 ("Source Adapter Architecture"):

    Do not put provider-specific logic into the Search Orchestrator.
    Use interfaces. ... Each adapter returns a common
    `CandidateReference`.

Every existing discovery adapter (GitHub - Phase 04, MCP Registry -
Phase 05, A2A Registry/Well-Known - Phase 06, Web Search - Phase 07,
Web Extraction - Phase 08) already returns its own provenance-carrying
candidate dataclass. This module does not change any of those
adapters; it defines the single `CandidateReference` shape from
Section 8 and one pure conversion function per source so the Phase 09
orchestrator can aggregate heterogeneous adapter output into one
uniform, source-agnostic list without ever inventing data that source
did not actually provide (rule #11: "Never fabricate schemas, Agent
Cards, source code, registry responses, reliability scores, or
execution results.").

`CandidateReference` is deliberately *not* a replacement for the
richer per-source dataclasses (`GitHubCandidate`,
`MCPRegistryCandidate`, ...): downstream protocol resolution
(Section 9/10, Phase 10) still needs the source-specific fields
(`packages`/`remotes` for an MCP Registry entry, `agent_card_url` for
an A2A Registry entry, ...), which is why every `CandidateReference`
carries the original object back in `raw_metadata["source_candidate"]`
in addition to the flattened common fields used for aggregation,
display and deduplication input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl

from app.models import ItemType, SourceType

Protocol = Literal["mcp", "a2a", "unknown"]


class CandidateReference(BaseModel):
    """The Section 8 candidate model, normalized from any source adapter."""

    candidate_id: UUID = Field(default_factory=uuid4)

    protocol: Protocol
    item_type: ItemType

    source_type: SourceType
    source_provider: str
    source_id: str

    url: HttpUrl | None = None
    repository_url: HttpUrl | None = None

    title: str
    description: str = ""

    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Section 13 provenance evidence: why this candidate was classified
    # the way it was. Never invented - only ever copied from the
    # source adapter's own classification evidence.
    evidence: tuple[str, ...] = ()

    raw_metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class SourceOutcome:
    """Isolated result of attempting one discovery source (rule #21).

    A `DiscoveryOrchestrator`/`MCPDiscoveryAdapter`/`A2ADiscoveryAdapter`
    run several of these concurrently. One source's exception must
    never prevent the others from completing or from being reflected
    in the search response - this is why each source's outcome is
    captured explicitly as data (`succeeded`/`error`) instead of
    letting an exception propagate out of the aggregate call.
    """

    source: str
    succeeded: bool
    candidates: tuple[CandidateReference, ...] = ()
    error: str | None = None


def _url_or_none(value: str | None) -> str | None:
    return value if value else None


def from_github_candidate(candidate: Any) -> CandidateReference:
    """Convert a `GitHubDiscoveryAdapter` `GitHubCandidate`."""
    protocol: Protocol = "mcp" if candidate.item_type is ItemType.TOOL else "a2a"

    return CandidateReference(
        protocol=protocol,
        item_type=candidate.item_type,
        source_type=candidate.source.type,
        source_provider="GitHub",
        source_id=candidate.source.id,
        url=_url_or_none(str(candidate.source.url) if candidate.source.url else None),
        repository_url=_url_or_none(candidate.html_url),
        title=candidate.name,
        description=candidate.description,
        evidence=candidate.evidence,
        raw_metadata={"source_candidate": candidate},
    )


def from_mcp_registry_candidate(candidate: Any) -> CandidateReference:
    """Convert an `MCPRegistryClient` `MCPRegistryCandidate`."""
    return CandidateReference(
        protocol="mcp",
        item_type=ItemType.TOOL,
        source_type=candidate.source.type,
        source_provider="MCP Registry",
        source_id=candidate.source.id,
        url=_url_or_none(str(candidate.source.url) if candidate.source.url else None),
        repository_url=_url_or_none(candidate.repository_url),
        title=candidate.title or candidate.server_name,
        description=candidate.description,
        evidence=(
            "returned by the official MCP Registry " f"as version {candidate.version}",
        ),
        raw_metadata={"source_candidate": candidate},
    )


def from_a2a_registry_candidate(candidate: Any) -> CandidateReference:
    """Convert an `A2ARegistryClient` `A2ARegistryCandidate`."""
    return CandidateReference(
        protocol="a2a",
        item_type=ItemType.AGENT,
        source_type=candidate.source.type,
        source_provider="A2A Registry",
        source_id=candidate.source.id,
        url=_url_or_none(str(candidate.source.url) if candidate.source.url else None),
        repository_url=_url_or_none(candidate.repository_url),
        title=candidate.agent_name,
        description=candidate.description,
        evidence=("returned by a configured A2A/agent registry",),
        raw_metadata={"source_candidate": candidate},
    )


def from_web_search_candidate(
    candidate: Any,
    *,
    source_provider: str = "Web Search",
) -> CandidateReference:
    """Convert a `WebSearchDiscoveryAdapter` `WebSearchCandidate`."""
    protocol: Protocol = "mcp" if candidate.item_type is ItemType.TOOL else "a2a"

    return CandidateReference(
        protocol=protocol,
        item_type=candidate.item_type,
        source_type=candidate.source.type,
        source_provider=source_provider,
        source_id=candidate.source.id,
        url=_url_or_none(candidate.url),
        title=candidate.title,
        description=candidate.snippet,
        evidence=candidate.evidence,
        raw_metadata={"source_candidate": candidate},
    )


def from_web_extraction_candidate(candidate: Any) -> CandidateReference:
    """Convert a `WebExtractionAdapter` `WebExtractionCandidate`."""
    protocol: Protocol = "mcp" if candidate.item_type is ItemType.TOOL else "a2a"

    return CandidateReference(
        protocol=protocol,
        item_type=candidate.item_type,
        source_type=candidate.source.type,
        source_provider="Web Extraction",
        source_id=candidate.source.id,
        url=_url_or_none(candidate.url),
        title=candidate.title,
        description=candidate.text_excerpt,
        evidence=candidate.evidence,
        raw_metadata={"source_candidate": candidate},
    )


def from_well_known_probe(probe: Any) -> CandidateReference | None:
    """Convert a found `probe_well_known_agent_card` `AgentCardProbeResult`.

    Returns `None` when the probe found no real Agent Card - a host
    that does not serve one is simply not a candidate (never
    fabricated), matching the module's own contract.
    """
    if not probe.found or probe.resolution is None:
        return None

    agent = probe.resolution.agent

    return CandidateReference(
        protocol="a2a",
        item_type=ItemType.AGENT,
        source_type=probe.source.type,
        source_provider="A2A Well-Known",
        source_id=probe.source.id,
        url=_url_or_none(str(probe.source.url) if probe.source.url else None),
        title=probe.target,
        description=", ".join(agent.skills) if agent.skills else "",
        evidence=("resolved a validated Agent Card at the probed target",),
        raw_metadata={"source_candidate": probe},
    )


def from_configured_endpoint(
    *,
    url: str,
    protocol: Protocol,
    title: str | None = None,
    description: str = "",
) -> CandidateReference:
    """Wrap one directly configured MCP/Agent-Card endpoint (Section 6).

    A configured endpoint is not resolved here - it is only tagged as
    a `CONFIGURED` candidate for downstream protocol resolution
    (Phase 10), exactly like every other source adapter's untrusted
    output.
    """
    item_type = ItemType.TOOL if protocol == "mcp" else ItemType.AGENT

    return CandidateReference(
        protocol=protocol,
        item_type=item_type,
        source_type=SourceType.CONFIGURED,
        source_provider="Configured Endpoint",
        source_id=url,
        url=_url_or_none(url),
        title=title or url,
        description=description,
        evidence=("explicitly configured as a trusted discovery endpoint",),
        raw_metadata={},
    )
