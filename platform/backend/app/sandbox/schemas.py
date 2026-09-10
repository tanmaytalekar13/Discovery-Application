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
    "local_source",        # github_repository entry - runnable, but run config
                            # (runtime/install/entrypoint/env) is unresolved until
                            # the tree is fetched and extracted on 'Test Tool' click
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
    runtime_arguments: list[str] = Field(default_factory=list, alias="runtimeArguments")
    allowed_domains: list[str] = Field(default_factory=list, alias="allowedDomains")
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

    auth_value_prefix: str | None = Field(
        default=None,
        description="Literal prefix from a registry header template, e.g. Bearer.",
    )


class GithubSourceHint(BaseModel):
    """Repo coordinates for a github_repository-sourced item.

    This is deliberately thin - just enough for the backend to know
    what to clone. It carries no runtime/install/entrypoint/env info,
    because that data doesn't exist in the GitHub discovery config
    entry at all (unlike mcp_registry_packages, which declares it
    up front). That information is derived later, lazily, by
    sandbox.extract.extract_local_run_config from the repo's file
    tree - not here, since classification must stay fast and
    synchronous (no network calls).
    """

    repository: str
    clone_url: str
    default_branch: str | None = None


class ClassificationResult(BaseModel):
    """Result of classifying a single tool item for live testing.

    `testable` is the single boolean the frontend should branch on.
    `mode` is informational and helps the UI pick a label / icon.
    `detail` carries the supporting data the UI needs to render the
    appropriate affordance (remote URL list for connect, package list
    for the 'Run Locally' panel, or repo coordinates for a GitHub
    source pending extraction).
    """

    testable: bool
    mode: ClassificationMode
    detail: list[RemoteCandidate] | list[LocalPackageHint] | GithubSourceHint | None = None
    reason: str | None = Field(
        default=None,
        description="Short human-readable explanation shown in the UI when a tool can't be tested.",
    )


RunSource = Literal["manifest", "readme", "package", "python", "docker", "heuristic"]
ExecutionType = Literal["remote", "local", "both", "ambiguous"]


class EnvironmentRequirement(BaseModel):
    """A configuration value discovered from repository evidence.

    Values are deliberately never part of an execution plan.  The UI can use
    this metadata to collect them for one test session only.
    """
    name: str
    description: str | None = None
    is_secret: bool = False
    is_required: bool = True
    evidence: str | None = None


class LocalRunConfig(BaseModel):
    """Resolved run config for a github_repository-sourced item.

    Produced lazily by sandbox.extract.extract_local_run_config once
    the user clicks 'Test Tool' on a `local_source` item - this is
    the thing GithubSourceHint deliberately doesn't carry.

    `source` tells the frontend how much to trust `command`/`args`/
    `env_vars`: a real repo manifest is authoritative, a README's
    `mcpServers` JSON block is the de-facto convention authors use
    and is nearly as reliable, and `heuristic` means we only found
    enough to guess a runtime + install step - command/args/env are
    empty and the UI should say so rather than attempt a run.
    """

    source: RunSource
    runtime: str | None = Field(
        default=None,
        description="One of: docker, python-uv, python, node, go, rust.",
    )
    install_command: str | None = Field(default=None, alias="installCommand")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env_vars: list[str] = Field(
        default_factory=list,
        alias="envVars",
        description="Names only - values are collected from the user at test time, never stored here.",
    )
    # A GitHub repository can publish a hosted MCP endpoint.  Keeping this
    # alongside the local command lets the lazy source inspection decide the
    # transport before anything is started in a container.
    remote: RemoteCandidate | None = None
    candidates: list[str] = Field(default_factory=list)
    execution_type: ExecutionType = "ambiguous"
    transport: str | None = None
    working_directory: str = "."
    required_env: list[EnvironmentRequirement] = Field(default_factory=list)
    external_dependencies: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    rationale: str | None = None

    model_config = {"populate_by_name": True}
