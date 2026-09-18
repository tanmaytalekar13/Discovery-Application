"""Common candidate model shared across discovery-source adapters.

Every discovery adapter (GitHub, MCP Registry) returns its own
provenance-carrying candidate dataclass. This module defines the single
`CandidateReference` shape and one pure conversion function per source
so the orchestrator can aggregate heterogeneous adapter output into one
uniform, source-agnostic list without ever inventing data that source
did not actually provide.

`CandidateReference` is deliberately *not* a replacement for the richer
per-source dataclasses (`GitHubCandidate`, `MCPRegistryCandidate`):
downstream protocol resolution still needs the source-specific fields
(`packages`/`remotes` for an MCP Registry entry), which is why every
`CandidateReference` carries the original object back in
`raw_metadata["source_candidate"]` in addition to the flattened common
fields used for aggregation, display and deduplication input.
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
    """The common candidate model, normalized from any source adapter."""

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

    # Provenance evidence: why this candidate was classified the way it
    # was. Never invented - only ever copied from the source adapter's
    # own classification evidence.
    evidence: tuple[str, ...] = ()

    raw_metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class SourceOutcome:
    """Isolated result of attempting one discovery source.

    A `MCPDiscoveryAdapter` runs several of these concurrently. One
    source's exception must never prevent the others from completing or
    from being reflected in the search response - this is why each
    source's outcome is captured explicitly as data (`succeeded`/
    `error`) instead of letting an exception propagate out of the
    aggregate call.
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
