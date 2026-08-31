from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.discovery.common.candidate import CandidateReference


def _norm_url(value: str | None) -> str:
    if not value:
        return ""
    parsed = urlsplit(value.strip())
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), host, path, "", ""))


def canonical_identity(candidate: CandidateReference, normalized: dict[str, Any] | None = None) -> str:
    """Return a deterministic protocol-aware identity; never uses display name alone."""
    data = normalized or {}
    if candidate.protocol == "mcp":
        server = str(data.get("server_id") or candidate.source_id).strip().lower()
        version = str(data.get("version") or "").strip().lower()
        tool = str(data.get("tool_name") or "").strip().lower()
        endpoint = _norm_url(str(data.get("endpoint") or candidate.url or ""))
        raw = "|".join(("mcp", server, version, tool, endpoint))
    else:
        identity = str(data.get("agent_identity") or candidate.source_id).strip().lower()
        endpoint = _norm_url(str(data.get("endpoint") or candidate.url or ""))
        raw = "|".join(("a2a", identity, endpoint))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
