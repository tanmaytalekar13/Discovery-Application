"""Pydantic schemas for tool classification.

These models describe how a discovered MCP tool item can be tested
live from this app. The frontend reads `classification` from the
search/item response to decide which action button (or no button)
to render.
"""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


ClassificationMode = Literal[
    "remote",              # mcp_registry_remotes entry with non-localhost URL
    "remote_via_package",  # mcp_registry_packages entry with http/sse + non-localhost URL
    "local_stdio",         # stdio package, or http package pointing at localhost
    "not_testable",        # no remotes or packages - blog article, web search result, etc.
]


class LocalPackageHint(BaseModel):
    """A single package entry for the 'Run Locally' info panel.

    Only the fields the frontend actually needs to render install
    instructions and env-var hints are surfaced. We deliberately
    keep this minimal and stable so registry shape changes don't
    leak into the UI.
    """

    registry_type: str | None = Field(default=None, alias="registryType")
    identifier: str | None = None
    runtime_hint: str | None = Field(default=None, alias="runtimeHint")
    transport_type: str | None = Field(default=None, alias="transportType")
    install_command: str | None = Field(default=None, alias="installCommand")

    environment_variables: list[dict[str, Any]] = Field(
        default_factory=list, alias="environmentVariables"
    )

    model_config = {"populate_by_name": True}


class RemoteCandidate(BaseModel):
    """A non-localhost remote candidate the backend can attempt to dial."""

    type: Literal["streamable-http", "sse"]
    url: str
    auth_header: str | None = Field(
        default=None,
        description=(
            "The HTTP header name to use for the auth token (e.g. 'x-api-key'). "
            "If absent, the client falls back to 'Authorization: Bearer'."
        ),
    )


class ClassificationResult(BaseModel):
    """Result of classifying a single tool item for live testing.

    `testable` is the single boolean the frontend should branch on.
    `mode` is informational and helps the UI pick a label / icon.
    `detail` carries the supporting data the UI needs to render the
    appropriate affordance (remote URL list for connect, package list
    for the 'Run Locally' panel).
    """

    testable: bool
    mode: ClassificationMode
    detail: list[RemoteCandidate] | list[LocalPackageHint] | None = None
    reason: str | None = Field(
        default=None,
        description="Short human-readable explanation shown in the UI when a tool can't be tested.",
    )
