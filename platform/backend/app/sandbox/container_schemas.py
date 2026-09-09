"""Pydantic schemas for local STDIO MCP tool testing.

These models describe the request/response contract for sandboxed local tool
execution via Docker containers.
"""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Environment Variable Schema (from package metadata)
# ---------------------------------------------------------------------------

class EnvironmentVariableSchema(BaseModel):
    """A single environment variable required by the package.

    Derived from the package's `environmentVariables` metadata.
    """

    name: str = Field(description="Environment variable name (e.g. GMAIL_OAUTH_CLIENT_ID)")
    description: str | None = Field(
        default=None,
        description="Human-readable description of what this variable is used for",
    )
    is_secret: bool = Field(
        default=False,
        description="Whether this variable contains sensitive data (password, token, etc.)",
    )
    is_required: bool = Field(
        default=True,
        description="Whether this variable is required for the tool to work",
    )


# ---------------------------------------------------------------------------
# /prepare response
# ---------------------------------------------------------------------------

class LocalPrepareResponse(BaseModel):
    """Response from /test/local/prepare.

    Returns the environment variable schema so the frontend can render
    a configuration form before attempting to connect.
    """

    item_id: UUID
    registry_type: str = Field(
        description="Registry type: 'npm' or 'pip'",
    )
    identifier: str = Field(
        description="Package identifier (e.g. 'com.pulsemcp/gmail' or 'mcp-gmail')",
    )
    install_command: str | None = Field(
        default=None,
        description="Suggested install command (e.g. 'npx -y com.pulsemcp/gmail')",
    )
    environment_variables: list[EnvironmentVariableSchema] = Field(
        default_factory=list,
        description="List of required/optional environment variables",
    )


# ---------------------------------------------------------------------------
# /connect request & response
# ---------------------------------------------------------------------------

class LocalConnectRequest(BaseModel):
    """Request body for /test/local/connect."""

    env_vars: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables with their values",
    )


class LocalConnectResponse(BaseModel):
    """Response from /test/local/connect.

    On success, returns the list of tools the MCP server exposes.
    On failure, returns error classification for the UI.
    """

    connected: bool = Field(
        default=False,
        description="True if connection was successful",
    )
    session_id: str | None = Field(
        default=None,
        description="Session ID for subsequent invoke/disconnect calls",
    )
    tools: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of available tools with their schemas",
    )
    error: str | None = Field(
        default=None,
        description="Error message if connection failed",
    )
    auth_reason: str | None = Field(
        default=None,
        description="Machine-readable error classification",
    )
    required_env_vars: list[str] = Field(
        default_factory=list,
        description="Credential environment variable names inferred from server diagnostics",
    )
    show_token_input: bool = Field(
        default=False,
        description="Whether the UI should offer an additional credential input",
    )
    user_message: str | None = Field(
        default=None,
        description="Human-readable error message safe for display",
    )
    show_retry: bool = Field(
        default=True,
        description="Whether to show a retry option in the UI",
    )


# ---------------------------------------------------------------------------
# /invoke request & response
# ---------------------------------------------------------------------------

class LocalInvokeRequest(BaseModel):
    """Request body for /test/local/invoke."""

    session_id: str = Field(
        description="Session ID from connect response",
    )
    tool_name: str = Field(
        description="Name of the tool to invoke",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Tool arguments conforming to the tool's inputSchema",
    )


class LocalInvokeResponse(BaseModel):
    """Response from /test/local/invoke."""

    status: Literal["success", "error"] = Field(
        description="Whether the invocation succeeded or failed",
    )
    result: Any = Field(
        default=None,
        description="Tool result payload (present on success)",
    )
    error: str | None = Field(
        default=None,
        description="Error message if invocation failed",
    )
    duration_ms: int = Field(
        default=0,
        description="Time taken to execute the tool in milliseconds",
    )
    user_message: str | None = Field(
        default=None,
        description="Human-readable error message (for display in UI)",
    )
    requires_auth: bool = Field(default=False)
    auth_reason: str | None = Field(default=None)
    show_token_input: bool = Field(default=False)


# ---------------------------------------------------------------------------
# /disconnect request & response
# ---------------------------------------------------------------------------

class LocalDisconnectRequest(BaseModel):
    """Request body for /test/local/disconnect."""

    session_id: str = Field(
        description="Session ID to terminate",
    )


class LocalDisconnectResponse(BaseModel):
    """Response from /test/local/disconnect."""

    status: Literal["ok", "error"] = Field(
        default="ok",
        description="Whether the disconnect was successful",
    )
    message: str | None = Field(
        default=None,
        description="Optional status message",
    )


# ---------------------------------------------------------------------------
# GitHub-Source Test Endpoints — /prepare and /connect
#
# Unlike the local_stdio flow (LocalPrepareResponse), run info here isn't
# known upfront from a registry package — it's derived by extract_local_run_config
# from the repo's manifest/README/heuristic (see app/sandbox/extract.py and
# app/sandbox/schemas.py::LocalRunConfig). So /prepare needs an explicit
# `status` to tell the frontend which of three states it's in, instead of
# always returning a ready-to-use install_command like LocalPrepareResponse does.
#
# No SourceInvokeRequest/Response or SourceDisconnectRequest/Response —
# sessions created via /test/source/connect live in the same ContainerSession
# store as local_stdio sessions, so LocalInvokeRequest/Response and
# LocalDisconnectRequest/Response are reused unmodified.
# ---------------------------------------------------------------------------

class SourcePrepareResponse(BaseModel):
    """Response from /test/source/prepare.

    Reports the outcome of extract_local_run_config(item) so the frontend
    knows what to render next:
    - status="not_runnable": no command could be derived (heuristic found
      only an installable runtime) — show an info panel, no Test button.
    - status="needs_auth": a runnable command was found but it requires
      env vars — render the credential form (environment_variables holds
      names only, values are always collected from the user, never stored).
    - status="ready": runnable command found, no env vars needed — frontend
      can call /test/source/connect directly.
    """

    item_id: UUID
    status: Literal["not_runnable", "needs_auth", "ready"] = Field(
        description="Which state extraction landed in; determines the next UI step",
    )
    source: Literal["manifest", "readme", "heuristic"] = Field(
        description="Where the run config was derived from, in priority order. "
        "Frontend can use this to show a confidence hint (e.g. 'inferred "
        "from README' vs 'declared in repo manifest')",
    )
    runtime: str | None = Field(
        default=None,
        description="Detected runtime, e.g. 'docker', 'python-uv', 'node' (heuristic tier)",
    )
    install_command: str | None = Field(
        default=None,
        description="Best-effort install command, when known",
    )
    environment_variables: list[str] = Field(
        default_factory=list,
        description="Required environment variable NAMES only (never values or "
        "placeholder secrets from the manifest/README)",
    )
    reason: str | None = Field(
        default=None,
        description="Human-readable explanation, populated when status='not_runnable'",
    )


# ---------------------------------------------------------------------------
# /connect request & response
# ---------------------------------------------------------------------------

class SourceConnectRequest(BaseModel):
    """Request body for /test/source/connect."""

    env_vars: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables with their values, matching the "
        "names returned by /test/source/prepare",
    )


class SourceConnectResponse(BaseModel):
    """Response from /test/source/connect.

    On success, returns the list of tools the MCP server exposes. The
    returned session_id is a normal ContainerSession id — subsequent calls
    use the existing /test/local/invoke and /test/local/disconnect endpoints
    with this session_id, there is no separate /test/source/invoke.

    Shape intentionally mirrors LocalConnectResponse so the frontend can
    reuse the same connect-result handling component for both flows.
    """

    connected: bool = Field(
        default=False,
        description="True if connection was successful",
    )
    session_id: str | None = Field(
        default=None,
        description="Session ID for subsequent invoke/disconnect calls "
        "(via /test/local/invoke and /test/local/disconnect)",
    )
    tools: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of available tools with their schemas",
    )
    error: str | None = Field(
        default=None,
        description="Error message if connection failed",
    )
    user_message: str | None = Field(
        default=None,
        description="Human-readable error message safe for display",
    )
    show_token_input: bool = Field(
        default=False,
        description="Whether the UI should offer an additional credential input",
    )
    show_retry: bool = Field(
        default=True,
        description="Whether to show a retry option in the UI",
    )