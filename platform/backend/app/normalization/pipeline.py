from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid5

from app.config import Settings
from app.db.repositories import ItemRepository
from app.deduplication.identity import canonical_identity
from app.discovery.common.candidate import CandidateReference
from app.models import (
    AgentMetadata,
    ArtifactMetadata,
    DiscoveryEvidence,
    DiscoveryMetadata,
    DiscoverySource,
    Item,
    ItemStatus,
    ItemType,
    Reliability,
    SourceType,
    ToolMetadata,
)
from app.query.embeddings import LocalEmbeddingModel
from app.reliability.engine import apply_evaluation, evaluate


MCPResolver = Callable[
    [CandidateReference],
    Awaitable[dict[str, Any] | list[dict[str, Any]] | None]
    | dict[str, Any]
    | list[dict[str, Any]]
    | None,
]
A2AResolver = Callable[[CandidateReference], Awaitable[dict[str, Any]] | dict[str, Any]]


class CandidateRejected(Exception):
    def __init__(self, reason: str, evidence: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence or [reason]


class Phase10Pipeline:
    """Phase 10 boundary: resolve -> normalize -> deduplicate -> score -> persist.

    Discovery candidates remain untrusted until protocol resolution succeeds. Every
    rejection is retained as evidence, while only reliability-approved items enter
    the Item catalog.
    """

    def __init__(
        self,
        repository: ItemRepository,
        settings: Settings,
        *,
        mcp_resolver: MCPResolver | None = None,
        a2a_resolver: A2AResolver | None = None,
        embedder: LocalEmbeddingModel | None = None,
        persist_rejections: bool = True,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.mcp_resolver = mcp_resolver
        self.a2a_resolver = a2a_resolver
        self.embedder = embedder or LocalEmbeddingModel(settings.embedding_dimensions)
        self.persist_rejections = persist_rejections

    async def process(
        self, candidates: list[CandidateReference] | tuple[CandidateReference, ...]
    ) -> "Phase10Result":
        approved: list[Item] = []
        rejected: list[Rejection] = []
        resolved = 0

        for candidate in candidates:
            try:
                items = await self._normalize_candidate(candidate)
                resolved += 1
                approved.extend(items)
            except CandidateRejected as exc:
                rejection = Rejection.from_candidate(
                    candidate, exc.reason, exc.evidence
                )
                rejected.append(rejection)
                if self.persist_rejections:
                    await self.repository.persist_rejection(rejection)
            except Exception as exc:
                rejection = Rejection.from_candidate(
                    candidate, "unexpected normalization/resolution failure", [str(exc)]
                )
                rejected.append(rejection)
                if self.persist_rejections:
                    await self.repository.persist_rejection(rejection)

        deduplicated = self._deduplicate(approved)
        persisted: list[Item] = []
        for item in deduplicated:
            protocol_validated = _candidate_has_explicit_protocol_evidence(item)
            evaluation = evaluate(
                item,
                self.settings,
                protocol_validated=protocol_validated,
                available=True,
            )
            apply_evaluation(item, evaluation)
            if not evaluation.approved:
                rejection = Rejection.from_item(
                    item,
                    "reliability score below approval threshold",
                    evaluation.reasons,
                )
                rejected.append(rejection)
                if self.persist_rejections:
                    await self.repository.persist_rejection(rejection)
                continue
            if item.embedding is None:
                item.embedding = self.embedder.embed_item(item)
            await self.repository.upsert_catalog_item(item, evaluation)
            persisted.append(item)

        return Phase10Result(
            candidates_seen=len(candidates),
            resolved=resolved,
            deduplicated=len(deduplicated),
            approved=tuple(persisted),
            rejected=tuple(rejected),
        )

    async def _normalize_candidate(self, candidate: CandidateReference) -> list[Item]:
        if candidate.protocol == "a2a":
            return [await self._normalize_a2a(candidate)]
        if candidate.protocol == "mcp":
            return await self._normalize_mcp(candidate)
        raise CandidateRejected(
            "unsupported protocol", [f"protocol={candidate.protocol}"]
        )

    async def _normalize_a2a(self, candidate: CandidateReference) -> Item:
        resolution = self._extract_a2a_resolution(candidate)
        if resolution is None and self.a2a_resolver is not None:
            resolution = await _maybe_await(self.a2a_resolver(candidate))
        if resolution is None:
            resolution = await self._resolve_a2a_with_existing_client(candidate)
        if not resolution:
            raise CandidateRejected(
                "A2A Agent Card could not be resolved and validated",
                list(candidate.evidence) or ["no Agent Card resolution available"],
            )

        agent = resolution.get("agent") or {}
        endpoint = (
            resolution.get("endpoint")
            or agent.get("endpoint")
            or str(candidate.url or "")
        )
        if not endpoint:
            raise CandidateRejected(
                "validated A2A Agent Card did not declare an endpoint"
            )
        card = resolution.get("raw_agent_card") or resolution.get("agent_card") or {}
        identity = str(card.get("name") or candidate.source_id)
        version = card.get("version") or resolution.get("protocol_version")
        now = datetime.now(timezone.utc)
        source = _source(candidate)
        evidence = _evidence(
            candidate, source, now, "discovery", list(candidate.evidence)
        )
        evidence.append(
            _new_evidence(
                candidate,
                source,
                now,
                "protocol_validation",
                "A2A Agent Card fetched and schema-validated",
                {"protocol_version": resolution.get("protocol_version")},
            )
        )
        canonical = canonical_identity(
            candidate, {"agent_identity": identity, "endpoint": endpoint}
        )
        item = Item(
            item_id=uuid5(NAMESPACE_URL, f"phase10:{canonical}"),
            canonical_id=canonical,
            type=ItemType.AGENT,
            name=identity,
            description=candidate.description or str(card.get("description") or ""),
            source=source,
            provenance=[source],
            evidence=evidence,
            version=version,
            status=ItemStatus.ACTIVE,
            reliability=Reliability(score=0.0, confidence=0.0),
            discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
            agent=AgentMetadata(
                endpoint=endpoint,
                agent_card=card,
                skills=list(agent.get("skills") or []),
                capabilities=list(agent.get("capabilities") or []),
                declared_dependencies=list(agent.get("declared_dependencies") or []),
            ),
            artifacts=ArtifactMetadata(
                source_available=bool(candidate.repository_url),
                source_url=candidate.repository_url,
            ),
        )
        return item

    async def _normalize_mcp(self, candidate: CandidateReference) -> list[Item]:
        resolution = None
        if self.mcp_resolver is not None:
            resolution = await _maybe_await(self.mcp_resolver(candidate))
        if resolution is None:
            resolution = await self._resolve_mcp_with_existing_client(candidate)
        if resolution is None:
            if _can_accept_best_effort_mcp(candidate):
                return [self._best_effort_mcp_item(candidate)]
            raise CandidateRejected(
                "MCP candidate could not be protocol-validated; initialize/tools/list requires a resolvable stdio transport",
                list(candidate.evidence)
                + ["MCP Registry metadata alone is not proof of an executable server"],
            )
        if isinstance(resolution, dict):
            tools = resolution.get("tools")
            server_info = resolution.get("server_info") or {}
        else:
            tools = resolution
            server_info = {}
        if not isinstance(tools, list) or not tools:
            raise CandidateRejected("MCP tools/list returned no usable tools")
        now = datetime.now(timezone.utc)
        source = _source(candidate)
        result: list[Item] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_name = str(tool.get("tool_name") or tool.get("name") or "").strip()
            if not tool_name:
                continue
            server_id = str(server_info.get("name") or candidate.source_id)
            version = server_info.get("version") or _candidate_version(candidate)
            schema = tool.get("mcp_schema") or tool.get("inputSchema") or {}
            if not isinstance(schema, dict):
                raise CandidateRejected(
                    f"MCP tool '{tool_name}' has invalid input schema"
                )
            evidence = _evidence(
                candidate, source, now, "discovery", list(candidate.evidence)
            )
            evidence.append(
                _new_evidence(
                    candidate,
                    source,
                    now,
                    "protocol_validation",
                    f"MCP initialize and tools/list validated tool '{tool_name}'",
                    {"server": server_id},
                )
            )
            canonical = canonical_identity(
                candidate,
                {
                    "server_id": server_id,
                    "version": version,
                    "tool_name": tool_name,
                    "endpoint": candidate.url,
                },
            )
            result.append(
                Item(
                    item_id=uuid5(NAMESPACE_URL, f"phase10:{canonical}"),
                    canonical_id=canonical,
                    type=ItemType.TOOL,
                    name=tool_name,
                    description=str(
                        tool.get("description") or candidate.description or ""
                    ),
                    source=source,
                    provenance=[source],
                    evidence=evidence,
                    version=version,
                    status=ItemStatus.ACTIVE,
                    reliability=Reliability(score=0.0, confidence=0.0),
                    discovery=DiscoveryMetadata(
                        first_seen=now, last_seen=now, last_synced=now
                    ),
                    tool=ToolMetadata(
                        server_id=server_id, tool_name=tool_name, mcp_schema=schema
                    ),
                    artifacts=_artifact_metadata(candidate),
                )
            )
        if not result:
            raise CandidateRejected(
                "MCP tools/list contained no valid tool definitions"
            )
        return result

    def _best_effort_mcp_item(self, candidate: CandidateReference) -> Item:
        now = datetime.now(timezone.utc)
        source = _source(candidate)
        evidence = _evidence(
            candidate,
            source,
            now,
            "discovery",
            list(candidate.evidence) or ["live discovery result matched protocol keywords"],
        )
        evidence.append(
            _new_evidence(
                candidate,
                source,
                now,
                "protocol_validation",
                "best-effort live candidate accepted from source-backed protocol evidence",
                {"source": source.type.value, "url": str(candidate.url or "")},
            )
        )
        canonical = canonical_identity(
            candidate,
            {"source_type": source.type.value, "source_id": source.id, "url": str(candidate.url or candidate.source_id)},
        )
        return Item(
            item_id=uuid5(NAMESPACE_URL, f"phase10:{canonical}"),
            canonical_id=canonical,
            type=candidate.item_type,
            name=candidate.title or candidate.source_id,
            description=candidate.description or "",
            source=source,
            provenance=[source],
            evidence=evidence,
            status=ItemStatus.ACTIVE,
            reliability=Reliability(score=0.0, confidence=0.0),
            discovery=DiscoveryMetadata(first_seen=now, last_seen=now, last_synced=now),
            tool=ToolMetadata(
                server_id=candidate.source_id,
                tool_name=candidate.title or candidate.source_id,
                mcp_schema={"type": "object"},
            ) if candidate.item_type == ItemType.TOOL else None,
            artifacts=_artifact_metadata(candidate),
        )

    async def _resolve_a2a_with_existing_client(
        self, candidate: CandidateReference
    ) -> dict[str, Any] | None:
        raw = candidate.raw_metadata.get("source_candidate")
        target = None
        card_path = "/.well-known/agent-card.json"
        if (
            raw is not None
            and hasattr(raw, "agent_card_url")
            and getattr(raw, "agent_card_url")
        ):
            target = getattr(raw, "agent_card_url")
            card_path = ""
        elif (
            raw is not None
            and hasattr(raw, "resolution")
            and getattr(raw, "resolution")
        ):
            resolution = getattr(raw, "resolution")
            return {
                "endpoint": resolution.endpoint,
                "protocol_version": resolution.protocol_version,
                "raw_agent_card": resolution.raw_agent_card,
                "agent": resolution.agent.model_dump(mode="json"),
            }
        else:
            target = str(candidate.url or "")
        if not target:
            return None
        from app.discovery.a2a.client import resolve_a2a_agent

        try:
            resolution = await resolve_a2a_agent(target, card_path=card_path)
        except Exception as exc:
            raise CandidateRejected(
                "A2A Agent Card resolution failed", [str(exc)]
            ) from exc
        return {
            "endpoint": resolution.endpoint,
            "protocol_version": resolution.protocol_version,
            "raw_agent_card": resolution.raw_agent_card,
            "agent": resolution.agent.model_dump(mode="json"),
        }

    async def _resolve_mcp_with_existing_client(
        self, candidate: CandidateReference
    ) -> dict[str, Any] | None:
        raw = candidate.raw_metadata.get("source_candidate")
        command = None
        args: list[str] = []
        if raw is not None:
            command = getattr(raw, "stdio_command", None)
            args = list(getattr(raw, "stdio_args", ()) or ())
        if not command:
            # Deliberately do not execute arbitrary registry package identifiers.
            # A deployment can inject an MCPResolver that supplies an explicitly
            # approved StdioServerParameters instance.
            return None
        from mcp import StdioServerParameters
        from app.discovery.mcp.client import resolve_mcp_server

        params = StdioServerParameters(command=command, args=args)
        resolved = await resolve_mcp_server(candidate.source_id, params)
        return {
            "server_info": resolved.server_info.__dict__,
            "tools": [tool.model_dump(mode="json") for tool in resolved.tools],
        }

    @staticmethod
    def _extract_a2a_resolution(candidate: CandidateReference) -> dict[str, Any] | None:
        raw = candidate.raw_metadata.get("source_candidate")
        if raw is None or not hasattr(raw, "resolution"):
            return None
        resolution = getattr(raw, "resolution")
        if resolution is None:
            return None
        return {
            "endpoint": resolution.endpoint,
            "protocol_version": resolution.protocol_version,
            "raw_agent_card": resolution.raw_agent_card,
            "agent": resolution.agent.model_dump(mode="json"),
        }

    @staticmethod
    def _deduplicate(items: list[Item]) -> list[Item]:
        groups: dict[str, Item] = {}
        for item in items:
            current = groups.get(item.canonical_id or str(item.item_id))
            if current is None:
                groups[item.canonical_id or str(item.item_id)] = item
                continue
            current.provenance = _merge_sources(current.provenance, item.provenance)
            current.evidence = _merge_evidence(current.evidence, item.evidence)
            if len(item.description) > len(current.description):
                current.description = item.description
        return list(groups.values())


class Rejection:
    def __init__(
        self,
        candidate_id: UUID,
        protocol: str,
        source: DiscoverySource,
        reason: str,
        evidence: list[str],
        observed_at: datetime,
        details: dict[str, Any] | None = None,
        item_id: UUID | None = None,
    ) -> None:
        self.candidate_id = candidate_id
        self.protocol = protocol
        self.source = source
        self.reason = reason
        self.evidence = evidence
        self.observed_at = observed_at
        self.details = details or {}
        self.item_id = item_id

    @classmethod
    def from_candidate(
        cls, candidate: CandidateReference, reason: str, evidence: list[str]
    ) -> "Rejection":
        return cls(
            candidate.candidate_id,
            candidate.protocol,
            _source(candidate),
            reason,
            evidence,
            datetime.now(timezone.utc),
            candidate.raw_metadata,
        )

    @classmethod
    def from_item(cls, item: Item, reason: str, evidence: list[str]) -> "Rejection":
        return cls(
            item.item_id,
            item.type.value,
            item.source,
            reason,
            evidence,
            datetime.now(timezone.utc),
            {"canonical_id": item.canonical_id},
            item.item_id,
        )


class Phase10Result:
    def __init__(
        self,
        *,
        candidates_seen: int,
        resolved: int,
        deduplicated: int,
        approved: tuple[Item, ...],
        rejected: tuple[Rejection, ...],
    ) -> None:
        self.candidates_seen = candidates_seen
        self.resolved = resolved
        self.deduplicated = deduplicated
        self.approved = approved
        self.rejected = rejected


def _source(candidate: CandidateReference) -> DiscoverySource:
    return DiscoverySource(
        type=candidate.source_type,
        id=candidate.source_id,
        url=candidate.url,
        provider=candidate.source_provider,
    )


def _has_explicit_protocol_evidence(candidate: CandidateReference) -> bool:
    text = " ".join((candidate.title, candidate.description, *candidate.evidence)).lower()
    if not text:
        return False
    markers = (
        "mcp server",
        "model context protocol",
        "tools/list",
        "tools/call",
        "mcp.server",
        "@mcp.tool",
        "agent card",
        "a2a agent",
        "agent2agent",
        "well-known",
    )
    return any(marker in text for marker in markers)


def _can_accept_best_effort_mcp(candidate: CandidateReference) -> bool:
    """Accept non-executable MCP discovery hits without running arbitrary code."""
    if candidate.item_type is not ItemType.TOOL:
        return False
    if candidate.source_type is SourceType.MCP_REGISTRY:
        raw = candidate.raw_metadata.get("source_candidate")
        return raw is not None and bool(getattr(raw, "version", None))
    if candidate.source_type is SourceType.GITHUB:
        return bool(candidate.repository_url and candidate.evidence)
    if candidate.source_type in {SourceType.WEB_SEARCH, SourceType.WEB_PAGE}:
        return _has_explicit_protocol_evidence(candidate)
    return False


def _candidate_has_explicit_protocol_evidence(item: Item) -> bool:
    text = " ".join(
        entry.statement.lower() for entry in item.evidence
    )
    if not text:
        return False
    markers = (
        "mcp server",
        "model context protocol",
        "mcp",
        "agent card",
        "a2a agent",
        "agent2agent",
        "a2a",
        "tools/list",
        "tools/call",
        "well-known",
    )
    return any(marker in text for marker in markers)


def _artifact_metadata(candidate: CandidateReference) -> ArtifactMetadata:
    source_url = candidate.repository_url or candidate.url
    return ArtifactMetadata(
        source_available=bool(source_url),
        source_url=source_url,
        source_code=_artifact_source_code(candidate),
        config_files=_artifact_config_files(candidate),
    )


def _artifact_source_code(candidate: CandidateReference) -> str | None:
    raw = candidate.raw_metadata.get("source_candidate")
    if raw is None:
        extracted = candidate.raw_metadata.get("web_extraction_candidate")
        return _extracted_text_content(extracted)

    readme = getattr(raw, "readme", None)
    if isinstance(readme, str) and readme.strip():
        return readme

    extracted = candidate.raw_metadata.get("web_extraction_candidate")
    content = _extracted_text_content(extracted)
    if content:
        return content

    return None


def _extracted_text_content(extracted: Any) -> str | None:
    if extracted is None:
        return None
    text = getattr(extracted, "text_content", None)
    if isinstance(text, str) and text.strip():
        return text
    excerpt = getattr(extracted, "text_excerpt", None)
    if isinstance(excerpt, str) and excerpt.strip():
        return excerpt
    return None


def _artifact_config_files(candidate: CandidateReference) -> list[dict[str, Any]]:
    raw = candidate.raw_metadata.get("source_candidate")
    config: list[dict[str, Any]] = []
    if raw is not None:
        raw_result = getattr(raw, "raw_result", None)
        if isinstance(raw_result, dict) and raw_result:
            config.append({"kind": "web_search_result", "result": raw_result})

    extracted = candidate.raw_metadata.get("web_extraction_candidate")
    if extracted is not None:
        config.append(
            {
                "kind": "web_extraction",
                "url": getattr(extracted, "url", None),
                "title": getattr(extracted, "title", None),
                "content_type": getattr(extracted, "content_type", None),
            }
        )

    extraction_error = candidate.raw_metadata.get("web_extraction_error")
    if isinstance(extraction_error, str) and extraction_error:
        config.append({"kind": "web_extraction_error", "error": extraction_error})

    if raw is None:
        return config

    repository = getattr(raw, "repository", None)
    clone_url = getattr(raw, "clone_url", None)
    default_branch = getattr(raw, "default_branch", None)
    if repository or clone_url or default_branch:
        config.append(
            {
                "kind": "github_repository",
                "repository": repository,
                "clone_url": clone_url,
                "default_branch": default_branch,
            }
        )

    root_entries = tuple(getattr(raw, "root_entries", ()) or ())
    if root_entries:
        config.append({"kind": "github_root_entries", "entries": list(root_entries)})

    packages = tuple(getattr(raw, "packages", ()) or ())
    remotes = tuple(getattr(raw, "remotes", ()) or ())
    if packages:
        config.append({"kind": "mcp_registry_packages", "packages": list(packages)})
    if remotes:
        config.append({"kind": "mcp_registry_remotes", "remotes": list(remotes)})

    raw_server = getattr(raw, "raw_server", None)
    if isinstance(raw_server, dict) and raw_server:
        config.append({"kind": "mcp_registry_server", "server": raw_server})

    return config


def _evidence(
    candidate: CandidateReference,
    source: DiscoverySource,
    now: datetime,
    kind: str,
    statements: list[str],
) -> list[DiscoveryEvidence]:
    return [
        _new_evidence(candidate, source, now, kind, statement, {})
        for statement in statements
    ]


def _new_evidence(
    candidate: CandidateReference,
    source: DiscoverySource,
    now: datetime,
    kind: str,
    statement: str,
    details: dict[str, Any],
) -> DiscoveryEvidence:
    evidence_id = uuid5(
        NAMESPACE_URL,
        f"{candidate.candidate_id}|{source.type.value}|{source.id}|{kind}|{statement}",
    )
    return DiscoveryEvidence(
        evidence_id=evidence_id,
        kind=kind,
        statement=statement,
        source=source,
        observed_at=now,
        details=details,
    )


def _merge_sources(
    left: list[DiscoverySource], right: list[DiscoverySource]
) -> list[DiscoverySource]:
    result: list[DiscoverySource] = []
    seen: set[str] = set()
    for source in [*left, *right]:
        key = f"{source.type.value}|{source.id}|{source.url}|{source.provider}"
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _merge_evidence(
    left: list[DiscoveryEvidence], right: list[DiscoveryEvidence]
) -> list[DiscoveryEvidence]:
    result: list[DiscoveryEvidence] = []
    seen: set[UUID] = set()
    for evidence in [*left, *right]:
        if evidence.evidence_id not in seen:
            seen.add(evidence.evidence_id)
            result.append(evidence)
    return result


def _candidate_version(candidate: CandidateReference) -> str | None:
    raw = candidate.raw_metadata.get("source_candidate")
    return getattr(raw, "version", None) if raw is not None else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
