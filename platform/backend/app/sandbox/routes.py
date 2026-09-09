"""Tool-test API routes.

Implements the endpoints needed for the Test Tool flow:

  POST /items/{item_id}/test/connect
  POST /items/{item_id}/test/invoke
  POST /items/{item_id}/test/authorize/start
  GET  /items/{item_id}/test/authorize/callback
  POST /items/{item_id}/test/disconnect
  POST /items/{item_id}/test/manual-token

  POST /items/{item_id}/test/local/prepare
  POST /items/{item_id}/test/local/connect
  POST /items/{item_id}/test/local/invoke
  POST /items/{item_id}/test/local/disconnect

  POST /items/{item_id}/test/source/prepare
  POST /items/{item_id}/test/source/connect
  (source sessions reuse /test/local/invoke and /test/local/disconnect —
   see note above those two routes)

These endpoints live in a separate router under /api/items/{item_id}/test
to keep them co-located with the tool-test logic and cleanly separated
from the existing discovery routes.
"""
from __future__ import annotations

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import get_item_repository
from app.api.schemas import (
    OAuthCallbackResponse,
    OAuthStartResponse,
    ToolConnectResponse,
    ToolInvokeRequest,
    ToolInvokeResponse,
)
from app.db.repositories import ItemRepository
from app.sandbox.classifier import classify_tool
from app.sandbox.container_manager import get_container_manager, LocalToolConfig
from app.sandbox.container_schemas import (
    EnvironmentVariableSchema,
    LocalConnectRequest,
    LocalConnectResponse,
    LocalDisconnectRequest,
    LocalDisconnectResponse,
    LocalInvokeRequest,
    LocalInvokeResponse,
    LocalPrepareResponse,
    SourceConnectRequest,
    SourceConnectResponse,
    SourcePrepareResponse,
)
from app.sandbox.extract import extract_local_run_config
from app.sandbox.local_mcp_client import LocalMCPClient
from app.sandbox.mcp_client import MCPTestClient
from app.sandbox.schemas import GithubSourceHint, LocalPackageHint, LocalRunConfig, RemoteCandidate
from app.sandbox.session import get_session_store, SessionStore

router = APIRouter(prefix="/api/items", tags=["tool-test"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _require_testable(
    item_id: UUID,
    repository: ItemRepository,
) -> tuple[RemoteCandidate, str]:
    """Classify an item and return the first remote candidate + the URL.

    Raises HTTPException(400) if the item is not testable.
    """
    item = await _require_item(item_id, repository)
    classification = classify_tool(item)
    if not classification.testable:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                classification.reason
                or "This tool cannot be tested live from this application."
            ),
        )

    # Pick the first remote candidate in priority order:
    # streamable-http first, then sse. This is already the order the
    # classifier preserves from the registry.
    detail = classification.detail
    if not detail:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Testable item has no remote candidates",
        )

    candidate: RemoteCandidate
    if isinstance(detail[0], RemoteCandidate):
        candidate = detail[0]
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Testable item returned a non-remote detail",
        )

    return candidate, candidate.url


async def _require_item(item_id: UUID, repository: ItemRepository):
    item = await repository.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


def _extract_bearer_token(session: SessionStore | None, session_id: str) -> str | None:
    """Return the stored bearer token for a session, if any."""
    if session is None:
        return None
    s = session.get_session(session_id)
    if s is None or s.access_token is None:
        return None
    return s.access_token


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/connect
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/connect",
    response_model=ToolConnectResponse,
    summary="Connect to a remote MCP server",
)
async def connect(
    item_id: UUID,
    session_id: Annotated[str | None, Query(description="Optional test-session ID to attach auth token")] = None,
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """Open an MCP session and run initialize + tools/list.

    If the server returns 401/403 (or raises WWW-Authenticate) the
    response carries `auth_required=True` so the frontend can show a
    "Connect Account" button. A successful connect returns the list
    of tools the server exposes.

    An optional `session_id` query parameter will attach any previously
    stored bearer token to the request (e.g. after an OAuth flow).
    """
    candidate, url = await _require_testable(item_id, repository)
    token = _extract_bearer_token(session_store, session_id) if session_id else None

    client = MCPTestClient(
        url=url,
        auth_token=token,
        preferred_transport=candidate.type,
        auth_header=candidate.auth_header,
    )
    result = await client.connect()

    return ToolConnectResponse(
        connected=result.connected,
        auth_required=result.auth_required,
        transport=result.transport,
        tools=[
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in result.tools
        ],
        error=result.error,
        requires_auth=result.auth_required,
        auth_reason=result.auth_reason,
        user_message=result.user_message,
        show_token_input=result.show_token_input,
        show_oauth_button=result.show_oauth_button,
    )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/invoke
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/invoke",
    response_model=ToolInvokeResponse,
    summary="Invoke a tool on a remote MCP server",
)
async def invoke(
    item_id: UUID,
    body: ToolInvokeRequest,
    session_id: Annotated[str | None, Query(description="Test-session ID")] = None,
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """Call a named tool with the given arguments.

    The `tool_name` is the name from the tools list returned by the
    connect endpoint. The `arguments` must conform to the tool's
    inputSchema.

    If the server returns 401/403 the response carries
    `requires_auth=True` so the frontend can offer a re-auth flow.
    """
    candidate, url = await _require_testable(item_id, repository)
    token = _extract_bearer_token(session_store, session_id) if session_id else None

    client = MCPTestClient(
        url=url,
        auth_token=token,
        preferred_transport=candidate.type,
        auth_header=candidate.auth_header,
    )
    result = await client.invoke(body.tool_name, body.arguments)

    return ToolInvokeResponse(
        status=result.status,
        result=result.result,
        error=result.error,
        requires_auth=result.requires_auth,
        duration_ms=result.duration_ms,
        auth_reason=result.auth_reason,
        user_message=result.user_message,
        show_token_input=result.show_token_input,
        show_oauth_button=result.show_oauth_button,
    )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/authorize/start
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/authorize/start",
    response_model=OAuthStartResponse,
    summary="Start OAuth flow",
)
async def authorize_start(
    item_id: UUID,
    session_id: Annotated[str | None, Query(description="Test-session ID to associate with this flow")] = None,
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """Generate an OAuth authorization URL and return it for redirect.

    If the connect endpoint returned `auth_required=True` and
    `WWW-Authenticate` metadata, this endpoint uses that metadata to
    build the appropriate authorization URL.

    If the item has no OAuth metadata, a 400 is returned with a
    message suggesting the user provide a manual API key instead.

    The returned `authorization_url` should be opened in a new tab
    for the user to complete the OAuth flow. After authorization,
    the OAuth provider redirects to our `/callback` endpoint with
    the `state` and `code` parameters.

    The `session_id` is used to associate the OAuth callback with
    the correct in-memory session (and to verify the CSRF `state`).
    """
    # First ensure we have a session to attach this flow to
    if session_id:
        store = get_session_store()
        existing = store.get_session(session_id)
        if existing is None:
            session_id = None  # treat as missing; create new

    if session_id is None:
        store = get_session_store()
        session = store.create_session(item_id)
        session_id = session.session_id

    # Build the authorization URL.
    # For now we accept a pre-configured URL from the tool's classification
    # detail (WWW-Authenticate header). If no pre-configured URL exists,
    # we can't auto-discover the OAuth endpoint from the MCP spec, so we
    # return a 400 with a hint.
    #
    # TODO: When MCP spec adds OAuth discovery metadata, wire it here.
    item = await _require_item(item_id, repository)
    classification = classify_tool(item)

    # Look for an authorization URL in the candidate data.
    # The MCP spec uses WWW-Authenticate: Bearer authorization_url="..."
    # but that typically appears on a 401, not in the registry entry.
    # For now we return a 400 asking the user to provide an API key.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "OAuth authorization URL discovery from WWW-Authenticate metadata "
            "will be implemented when the MCP spec adds this to the registry "
            "schema. For now, use the /test/manual-token endpoint to "
            "provide a bearer token directly."
        ),
    )

    # This line is unreachable but satisfies the type checker
    state = secrets.token_urlsafe(32)
    return OAuthStartResponse(authorization_url="", state=state)  # pragma: no cover


# ---------------------------------------------------------------------------
# GET /items/{item_id}/test/authorize/callback
# ---------------------------------------------------------------------------

@router.get(
    "/{item_id}/test/authorize/callback",
    response_model=OAuthCallbackResponse,
    summary="OAuth callback",
)
async def authorize_callback(
    item_id: UUID,
    code: Annotated[str, Query(description="Authorization code from OAuth provider")],
    state: Annotated[str, Query(description="CSRF state from authorize/start")],
    session_id: Annotated[str | None, Query(description="Test-session ID")] = None,
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """Handle the OAuth callback from the authorization server.

    The OAuth provider redirects here with `code` and `state` parameters.
    We verify the `state` against our stored CSRF token, exchange the
    `code` for tokens, store the access token in the session, and
    redirect the user back to the app.

    In practice this endpoint is rarely called directly by the frontend —
    the user completes OAuth in a new tab and the redirect URL brings
    them back to the app. The app then re-calls /connect with the
    `session_id` to pick up the stored token.
    """
    if session_id is None:
        raise HTTPException(status_code=400, detail="session_id is required")

    # Verify CSRF state
    stored_state = session_store.get_oauth_state(session_id)
    if stored_state is None or stored_state != state:
        return OAuthCallbackResponse(
            success=False,
            message="Invalid or expired OAuth state (possible CSRF attack). Please try again.",
        )

    # Exchange code for token.
    # The token exchange endpoint depends on the OAuth provider's metadata.
    # For now this is a stub; the full implementation is gated on MCP spec
    # adding authorization_url to the registry schema.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=(
            "Token exchange requires the authorization server's token endpoint "
            "URL, which will be available when the MCP spec adds OAuth "
            "discovery metadata. Use /test/manual-token to provide a "
            "bearer token directly."
        ),
    )

    return OAuthCallbackResponse(success=True, message="Authorized successfully")  # pragma: no cover


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/disconnect
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/disconnect",
    summary="Disconnect and revoke test session",
)
async def disconnect(
    item_id: UUID,
    session_id: Annotated[str, Query(description="Test-session ID to revoke")],
    session_store: SessionStore = Depends(get_session_store),
    repository: ItemRepository = Depends(get_item_repository),
):
    """Immediately discard the stored token for a test session.

    After this call the session_id is invalid and any stored tokens
    are gone. The user will need to re-authorize if they want to test
    again.
    """
    item = await _require_item(item_id, repository)
    revoked = session_store.revoke_session(session_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "ok", "message": "Session revoked"}


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/manual-token  (MVP fallback)
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/manual-token",
    summary="Submit a manual bearer token",
)
async def submit_manual_token(
    item_id: UUID,
    token: Annotated[str, Query(description="Bearer token or API key")],
    session_id: Annotated[str | None, Query(description="Test-session ID")] = None,
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """MVP fallback for tools that don't support OAuth.

    Accept a bearer token (or API key) from the user and store it
    in the test session. The frontend shows an input field for this
    when the connect endpoint returns `auth_required=True` without
    a resolvable OAuth URL.

    The token is stored encrypted in the session store and attached
    to subsequent connect/invoke calls automatically.
    """
    candidate, url = await _require_testable(item_id, repository)

    if session_id is None:
        session = session_store.create_session(item_id)
    else:
        existing = session_store.get_session(session_id)
        if existing is None:
            session = session_store.create_session(item_id)
        else:
            session = existing

    session_store.store_token(
        session.session_id,
        access_token=token,
        token_type="Bearer",
        auth_method="bearer",
    )

    return {
        "status": "ok",
        "session_id": session.session_id,
        "message": "Token stored. You can now call /test/connect with this session_id.",
    }


# ---------------------------------------------------------------------------
# Local STDIO MCP Tool Testing Endpoints
# ---------------------------------------------------------------------------

async def _require_local_testable(
    item_id: UUID,
    repository: ItemRepository,
) -> LocalPackageHint:
    """Classify an item and return the local package hint.

    Raises HTTPException(400) if the item is not a local STDIO tool.
    """
    item = await _require_item(item_id, repository)
    classification = classify_tool(item)

    if classification.mode != "local_stdio":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                classification.reason
                or "This tool cannot be tested as a local STDIO server."
            ),
        )

    if not classification.detail or not isinstance(classification.detail[0], LocalPackageHint):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Local STDIO item has no package hint",
        )

    return classification.detail[0]


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/local/prepare
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/local/prepare",
    response_model=LocalPrepareResponse,
    summary="Prepare local tool test - get environment variable schema",
)
async def prepare_local_test(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Return the environment variable schema for a local MCP tool.

    This endpoint is called before the user fills in credentials.
    It returns:
    - registry_type: "npm" or "pip"
    - identifier: the package name
    - install_command: suggested command to install/run
    - environment_variables: schema for required/optional env vars

    The frontend uses this to render the credential form.
    """
    hint = await _require_local_testable(item_id, repository)

    # Build install command
    registry_type = "pip" if hint.registry_type == "pypi" else hint.registry_type
    if registry_type == "npm":
        install_command = f"npx -y {hint.identifier}"
    else:
        install_command = f"pip install {hint.identifier}"
        if hint.runtime_hint and "uvx" in hint.runtime_hint.lower():
            install_command = f"uvx {hint.identifier}"

    # Convert environment variables to schema format
    env_vars = [
        EnvironmentVariableSchema(
            name=var["name"],
            description=var.get("description"),
            is_secret=var.get("is_secret", var.get("isSecret", False)),
            is_required=var.get("is_required", var.get("isRequired", True)),
        )
        for var in hint.environment_variables
    ]

    return LocalPrepareResponse(
        item_id=item_id,
        registry_type=registry_type,
        identifier=hint.identifier,
        install_command=install_command,
        environment_variables=env_vars,
    )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/local/connect
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/local/connect",
    response_model=LocalConnectResponse,
    summary="Connect to a local STDIO MCP server",
)
async def connect_local(
    item_id: UUID,
    body: LocalConnectRequest,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Create a container, install the package, and connect to the MCP server.

    This endpoint:
    1. Creates an ephemeral Docker container
    2. Installs the npm/pip package inside it
    3. Spawns the MCP server as a STDIO process
    4. Runs initialize + tools/list

    Returns the session_id for subsequent invoke/disconnect calls,
    plus the list of available tools.
    """
    hint = await _require_local_testable(item_id, repository)

    # Build tool config
    config = LocalToolConfig(
        registry_type="pip" if hint.registry_type == "pypi" else hint.registry_type,
        identifier=hint.identifier,
        runtime_hint=hint.runtime_hint,
        runtime_arguments=hint.runtime_arguments,
    )

    container_manager = get_container_manager()

    try:
        # Create container
        session = await container_manager.create_container(
            item_id=item_id,
            config=config,
            env_vars=body.env_vars,
            allowed_domains=hint.allowed_domains,
        )

        # Connect to MCP server inside container
        command = config.build_command()
        mcp_client = LocalMCPClient()

        # Run connect (this runs the command inside the Docker container)
        connect_result = await mcp_client.connect(
            command=command,
            env_vars=body.env_vars,
            timeout=60.0,
            container_id=session.container_id,
        )

        if not connect_result.connected:
            # Clean up container on failure
            await container_manager.destroy_container(session.session_id)
            return LocalConnectResponse(
                connected=False,
                error=connect_result.error,
                auth_reason=connect_result.auth_reason,
                user_message=connect_result.user_message,
                required_env_vars=connect_result.required_env_vars,
                show_token_input=connect_result.auth_reason == "unauthorized",
            )

        # Update session status
        session.status = "running"
        session.mcp_client = mcp_client

        return LocalConnectResponse(
            connected=True,
            session_id=session.session_id,
            tools=[tool.to_dict() for tool in connect_result.tools],
        )

    except ValueError as exc:
        # Session limit exceeded
        return LocalConnectResponse(
            connected=False,
            error=str(exc),
            user_message=str(exc),
            show_retry=False,
        )
    except Exception as exc:
        return LocalConnectResponse(
            connected=False,
            error=str(exc),
            user_message=f"Failed to start local MCP server: {exc}",
        )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/local/invoke
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/local/invoke",
    response_model=LocalInvokeResponse,
    summary="Invoke a tool on a local STDIO MCP server",
)
async def invoke_local(
    item_id: UUID,
    body: LocalInvokeRequest,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Invoke a tool on an existing local MCP session.

    The session_id must be from a previous /test/local/connect OR
    /test/source/connect call — both create the exact same
    ContainerSession shape in the same session store, so this endpoint
    works unmodified for GitHub-source-tested sessions too. There is
    no separate /test/source/invoke endpoint.

    This endpoint calls the MCP server's tools/call method.
    """
    # Validate item exists
    await _require_item(item_id, repository)

    container_manager = get_container_manager()
    session = container_manager.get_session(body.session_id)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found or expired. Please connect again.",
        )

    if str(session.item_id) != str(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session does not belong to this item.",
        )

    mcp_client = session.mcp_client
    if mcp_client is None:
        # This should only occur for sessions created before the server was
        # initialized, or after an unexpected worker restart.
        return LocalInvokeResponse(
            status="error",
            error="Sandbox MCP connection is no longer available",
            user_message="The sandbox server was restarted or disconnected. Please connect again.",
        )

    try:
        result = await mcp_client.invoke(
            tool_name=body.tool_name,
            arguments=body.arguments,
            timeout=30.0,
        )

        return LocalInvokeResponse(
            status=result.status,
            result=result.result,
            error=result.error,
            duration_ms=result.duration_ms,
            user_message=result.user_message,
            requires_auth=result.requires_auth,
            auth_reason=result.auth_reason,
            show_token_input=result.requires_auth,
        )

    except Exception as exc:
        return LocalInvokeResponse(
            status="error",
            error=str(exc),
            user_message=f"Tool invocation failed: {exc}",
        )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/local/disconnect
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/local/disconnect",
    response_model=LocalDisconnectResponse,
    summary="Disconnect and destroy a local STDIO MCP session",
)
async def disconnect_local(
    item_id: UUID,
    body: LocalDisconnectRequest,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Destroy the container and clean up the session.

    This endpoint:
    1. Stops and removes the Docker container
    2. Cleans up any temporary files
    3. Invalidates the session_id

    Works unmodified for sessions from /test/source/connect too, for the
    same reason as invoke_local above — same ContainerSession store.

    The user must connect again if they want to test more tools.
    """
    # Validate item exists
    await _require_item(item_id, repository)

    container_manager = get_container_manager()
    session = container_manager.get_session(body.session_id)

    if session is None:
        # Already disconnected or expired - that's fine
        return LocalDisconnectResponse(
            status="ok",
            message="Session already cleaned up.",
        )

    if str(session.item_id) != str(item_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session does not belong to this item.",
        )

    # Destroy the container
    await container_manager.destroy_container(body.session_id)

    return LocalDisconnectResponse(
        status="ok",
        message="Session disconnected and container destroyed.",
    )


# ---------------------------------------------------------------------------
# GitHub-Source MCP Tool Testing Endpoints (no registry package/remote info —
# run config is derived lazily from the repo itself via extract_local_run_config)
# ---------------------------------------------------------------------------

# NEW-ASSUMPTION: naive per-worker cache. Registry hints (local_stdio /
# remote) are cheap/sync so they don't need this, but extraction here hits
# the GitHub API, so it's worth caching across prepare -> connect calls for
# the same item. If the app runs multiple workers/processes this will be
# inconsistent per-worker; acceptable for now (worst case = re-extraction,
# not wrong behavior). Swap for a shared cache (redis) if one already exists.
_run_config_cache: dict[str, LocalRunConfig] = {}


async def _require_source_testable(
    item_id: UUID,
    repository: ItemRepository,
) -> GithubSourceHint:
    """Classify an item and return the GithubSourceHint.

    Raises HTTPException(400) if the item is not a bare GitHub-source tool.
    """
    item = await _require_item(item_id, repository)
    classification = classify_tool(item)

    if classification.mode != "local_source":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                classification.reason
                or "This tool cannot be tested as a GitHub-source server."
            ),
        )

    if not classification.detail or not isinstance(classification.detail, GithubSourceHint):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub-source item has no source hint",
        )

    return classification.detail


async def _get_or_extract_run_config(item_id: UUID, item) -> LocalRunConfig:
    run_config = _run_config_cache.get(str(item_id))
    if run_config is None:
        run_config = await extract_local_run_config(item)
        _run_config_cache[str(item_id)] = run_config
    return run_config


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/source/prepare
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/source/prepare",
    response_model=SourcePrepareResponse,
    summary="Prepare GitHub-source tool test - extract run config, get env var names",
)
async def prepare_source_test(
    item_id: UUID,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Classify + extract the run config for a bare GitHub-source item.

    Unlike /test/local/prepare (where the registry already declares
    install_command/env vars), here nothing is known upfront — this
    endpoint runs extract_local_run_config(item), which reads the repo's
    manifest / README / file tree (in that priority order, see
    app/sandbox/extract.py) to derive it.

    Returns one of three states for the frontend:
    - "not_runnable": heuristic fallback only found an installable
      runtime, no runnable command — show info panel, no Test button.
    - "needs_auth": env var *names* were found (in manifest or README
      config) — render the credential form, same as the local_stdio flow.
    - "ready": no env vars required, frontend can call /test/source/connect
      directly.
    """
    item = await _require_item(item_id, repository)
    await _require_source_testable(item_id, repository)

    run_config = await _get_or_extract_run_config(item_id, item)

    if not run_config.command:
        # Heuristic tier with install-only info, or nothing structured
        # found at all — same "can install, can't run" case flagged in
        # extract.py's heuristic tier docstring.
        return SourcePrepareResponse(
            item_id=item_id,
            status="not_runnable",
            source=run_config.source,
            runtime=run_config.runtime,
            install_command=run_config.install_command,
            reason="Could not determine a run command for this repo from "
                   "its manifest, README, or file tree.",
        )

    return SourcePrepareResponse(
        item_id=item_id,
        status="needs_auth" if run_config.env_vars else "ready",
        source=run_config.source,
        runtime=run_config.runtime,
        install_command=run_config.install_command,
        environment_variables=run_config.env_vars,  # names only, per schema
    )


# ---------------------------------------------------------------------------
# POST /items/{item_id}/test/source/connect
# ---------------------------------------------------------------------------

@router.post(
    "/{item_id}/test/source/connect",
    response_model=SourceConnectResponse,
    summary="Connect to a GitHub-source MCP server (build/pull image, sandbox, MCP handshake)",
)
async def connect_source(
    item_id: UUID,
    body: SourceConnectRequest,
    repository: ItemRepository = Depends(get_item_repository),
):
    """Build/select a sandbox image, start the container, and connect.

    Mirrors connect_local() above almost exactly — same error handling,
    same LocalMCPClient — the only difference is create_container_from_source
    is used instead of create_container, since the run config here comes
    from repo extraction (Dockerfile / manifest / README / heuristic)
    rather than a registry package hint.

    create_container_from_source internally:
    - prefers the repo's own Dockerfile if present, else a generic
      per-runtime base image
    - runs the conditional install step (docker exec ... install_command)
      when a manifest/heuristic command needs deps installed and no
      Dockerfile build already handled it
    - applies the same network/resource limits as the registry-package path

    On success the returned session_id is used with the existing
    /test/local/invoke and /test/local/disconnect endpoints — there is no
    separate /test/source/invoke or /test/source/disconnect.
    """
    item = await _require_item(item_id, repository)
    hint = await _require_source_testable(item_id, repository)

    run_config = await _get_or_extract_run_config(item_id, item)

    if not run_config.command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No runnable command was found for this repo. Call "
                   "/test/source/prepare first to see why.",
        )

    missing = [v for v in run_config.env_vars if v not in body.env_vars]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing required env vars: {missing}",
        )

    container_manager = get_container_manager()

    try:
        session = await container_manager.create_container_from_source(
            item=item,
            github_hint=hint,
            run_config=run_config,
            env_vars=body.env_vars,
        )

        mcp_client = LocalMCPClient()
        connect_result = await mcp_client.connect(
            command=[run_config.command, *run_config.args],
            env_vars=body.env_vars,
            timeout=60.0,
            container_id=session.container_id,
        )

        if not connect_result.connected:
            await container_manager.destroy_container(session.session_id)
            return SourceConnectResponse(
                connected=False,
                error=connect_result.error,
                user_message=connect_result.user_message,
                show_token_input=connect_result.auth_reason == "unauthorized",
            )

        session.status = "running"
        session.mcp_client = mcp_client

        return SourceConnectResponse(
            connected=True,
            session_id=session.session_id,
            tools=[tool.to_dict() for tool in connect_result.tools],
        )

    except ValueError as exc:
        # Session limit exceeded — same per-user concurrent-limit check
        # create_container already enforces.
        return SourceConnectResponse(
            connected=False,
            error=str(exc),
            user_message=str(exc),
            show_retry=False,
        )
    except Exception as exc:
        return SourceConnectResponse(
            connected=False,
            error=str(exc),
            user_message=f"Failed to start GitHub-source MCP server: {exc}",
        )