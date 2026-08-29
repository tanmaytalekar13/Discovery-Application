"""
Exceptions raised while resolving/validating an MCP candidate.

Per CODEX_EXECUTION_PLAN.md (Section 9 - MCP Resolution, Section 39 -
Phase 02 Definition of Done): every stage of talking to a candidate MCP
server can fail independently, and a failure at any stage must be
reported explicitly rather than silently downgraded into a "success".
Never fabricate a schema or a result when a stage fails.
"""

from __future__ import annotations


class MCPResolutionError(Exception):
    """Base class for all MCP protocol resolution failures."""


class MCPConnectionError(MCPResolutionError):
    """
    Raised when a transport-level connection to the candidate MCP
    server could not be established (process failed to start,
    endpoint unreachable, transport handshake failed, etc).
    """


class MCPInitializeError(MCPResolutionError):
    """
    Raised when the MCP `initialize` handshake fails or the server
    returns a response that cannot be validated.
    """


class MCPToolsListError(MCPResolutionError):
    """
    Raised when `tools/list` fails, times out, or returns a payload
    that fails schema validation.
    """


class MCPInvalidSchemaError(MCPResolutionError):
    """
    Raised when a specific tool's `inputSchema` is missing or is not
    a well-formed JSON Schema object. Per rule #11 in the plan, a
    trusted tool schema must never be fabricated from README text or
    guessed defaults - if the server-provided schema is invalid, the
    tool must be rejected rather than "fixed up".
    """


class MCPToolNotFoundError(MCPResolutionError):
    """
    Raised when the caller asks to resolve/call a tool name that was
    not present in the server's `tools/list` response.
    """


class MCPToolCallError(MCPResolutionError):
    """
    Raised when `tools/call` fails at the protocol/transport level
    (as opposed to the tool itself reporting a normal application
    error inside a successful CallToolResult).
    """