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
