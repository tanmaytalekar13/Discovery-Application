"""Per-stage timeout budgets and invocation safety helpers (spec v2 Sections 5-6).

The critical rule: a single timeout is *inconclusive*, never a verdict.
Timeouts differ per stage and per tool; verification is async and
nobody user-facing waits on it, so budgets are generous.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Stage budgets (Section 6 table)
# ---------------------------------------------------------------------------

CONNECTION_TIMEOUT_S = 5.0        # TCP/TLS - 1 attempt
HANDSHAKE_TIMEOUT_S = 15.0        # initialize - 2 attempts
TOOL_INVOCATION_TIMEOUT_S = 75.0  # 60-90s band - 3 attempts spread over hours
HARD_KILL_TIMEOUT_S = 180.0       # per-attempt resource safety cap

HANDSHAKE_ATTEMPTS = 2
TOOL_INVOCATION_ATTEMPTS = 3

# Remote retry schedule (Section 5.2: now / +1h / +6h)
REMOTE_RETRY_DELAYS_S = (0, 3600, 6 * 3600)

# Malformed-input probe timeout is short: a crash/hang is the signal.
MALFORMED_INPUT_TIMEOUT_S = 10.0

# Progress-notification extension (Section 6): each MCP progress/streaming
# notification observed while a call is in flight extends the budget -
# the server is alive and working, just slow.
PROGRESS_NOTIFICATION_EXTENSION_S = 30.0
MAX_TOOL_BUDGET_S = HARD_KILL_TIMEOUT_S


@dataclass(frozen=True)
class ToolTimeoutPolicy:
    """Per-tool invocation budget (not per-server, Section 6)."""

    tool_name: str
    base_timeout_s: float = TOOL_INVOCATION_TIMEOUT_S
    attempts: int = TOOL_INVOCATION_ATTEMPTS
    hard_kill_s: float = HARD_KILL_TIMEOUT_S

    @property
    def effective_timeout_s(self) -> float:
        """Budget after natural-latency heuristics for this tool name."""
        name = self.tool_name.lower()
        timeout = self.base_timeout_s
        # Tools whose names suggest heavy work get the upper end of the band.
        if re.search(r"report|export|generate|scrape|crawl|render|analyze|bulk|sync", name):
            timeout = min(90.0, timeout + 15.0)
        # Trivial lookups stay nimble but still generous.
        if re.search(r"^get_|^is_|^ping|^health|^list", name):
            timeout = max(60.0, timeout - 15.0)
        return min(timeout, self.hard_kill_s)


def tool_policy(tool_name: str) -> ToolTimeoutPolicy:
    return ToolTimeoutPolicy(tool_name=tool_name)


# ---------------------------------------------------------------------------
# Read-only detection (Section 7.1 rule 4)
# ---------------------------------------------------------------------------

_READONLY_PREFIXES = ("list", "get", "search", "find", "fetch", "read", "query", "show")
_WRITE_MARKERS = (
    "create", "delete", "remove", "update", "write", "send", "post", "put",
    "patch", "submit", "deploy", "run", "exec", "invite", "move", "archive",
    "restore", "cancel", "pay", "purchase",
)


def is_readonly_tool(tool_name: str, description: str | None = None) -> bool:
    """Conservative read-only detection for auto-invocation with real credentials."""
    name = (tool_name or "").strip().lower()
    if not name:
        return False
    if any(name.startswith(prefix) for prefix in _READONLY_PREFIXES):
        return True
    if any(marker in name for marker in _WRITE_MARKERS):
        return False
    text = (description or "").lower()
    if not text:
        return False
    # Fall back to the description: destructive verbs disqualify.
    if any(marker in text for marker in ("deletes", "creates", "sends", "modifies", "writes", "posts")):
        return False
    return any(word in text for word in ("returns", "lists", "searches", "retrieves", "reads"))


# ---------------------------------------------------------------------------
# Synthetic argument generation (Section 5.1 step 5)
# ---------------------------------------------------------------------------

_EMPTY_SCHEMA = object()


def synthetic_arguments(tool: dict[str, Any]) -> dict[str, Any]:
    """Build minimal schema-derived arguments for one tool invocation.

    Follows JSON-Schema structure conservatively: fills required
    properties with type-appropriate placeholder values, honours enums
    and defaults, recurses into nested required objects. Properties with
    no required value are omitted rather than guessed.
    """
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    if not isinstance(schema, dict):
        return {}
    return _args_from_schema(schema, depth=0) or {}


def _args_from_schema(schema: dict[str, Any], depth: int) -> dict[str, Any]:
    if depth > 4:
        return {}
    props = schema.get("properties")
    required = schema.get("required") or []
    if not isinstance(props, dict):
        return {}
    out: dict[str, Any] = {}
    for name in required:
        if name not in props:
            continue
        prop = props[name] if isinstance(props[name], dict) else {}
        out[name] = _value_for_property(prop, depth + 1)
    return out


def _value_for_property(prop: dict[str, Any], depth: int) -> Any:
    if "const" in prop:
        return prop["const"]
    if "default" in prop:
        return prop["default"]
    if "enum" in prop and isinstance(prop["enum"], list) and prop["enum"]:
        return prop["enum"][0]

    declared = prop.get("type")
    if isinstance(declared, list) and declared:
        declared = next((t for t in declared if t != "null"), declared[0])

    if declared == "object":
        return _args_from_schema(prop, depth)
    if declared == "array":
        items = prop.get("items")
        if isinstance(items, dict):
            inner = _value_for_property(items, depth + 1)
            return [inner] if inner is not None else []
        return []
    if declared == "integer":
        return int(prop.get("minimum", 1) or 1)
    if declared == "number":
        return float(prop.get("minimum", 1) or 1)
    if declared == "boolean":
        return False
    # String default: prefer format-appropriate placeholders.
    fmt = prop.get("format")
    if fmt == "uri":
        return "https://example.com"
    if fmt in ("date-time", "date"):
        return "2026-01-01T00:00:00Z" if fmt == "date-time" else "2026-01-01"
    if fmt == "email":
        return "user@example.com"
    if fmt == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    return prop.get("minLength") and "x" * int(prop["minLength"]) or "test"


def malformed_arguments(tool: dict[str, Any]) -> dict[str, Any]:
    """Deliberately invalid arguments for the graceful-error probe
    (Section 5.1 step 6): wrong types on required fields, garbage
    values for unknown-required schemas."""
    schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    if not isinstance(schema, dict):
        return {"__malformed__": {"unexpected": True}}
    props = schema.get("properties")
    required = schema.get("required") or []
    if isinstance(props, dict) and required:
        first = required[0]
        prop = props.get(first) if isinstance(props.get(first), dict) else {}
        declared = prop.get("type")
        if declared == "string":
            return {first: 12345}
        if declared in ("integer", "number"):
            return {first: "not-a-number"}
        if declared == "boolean":
            return {first: "yes-please"}
        if declared == "object":
            return {first: "not-an-object"}
        if declared == "array":
            return {first: {"not": "an-array"}}
    return {"__malformed_unexpected_field__": {"deeply": {"nested": [1, 2, 3]}}}
