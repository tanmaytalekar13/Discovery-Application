"""Tool-test API routes.

Implements the five endpoints needed for the Test Tool flow:

  POST /items/{item_id}/test/connect
  POST /items/{item_id}/test/invoke
  POST /items/{item_id}/test/authorize/start
  GET  /items/{item_id}/test/authorize/callback
  POST /items/{item_id}/test/disconnect

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
from app.sandbox.mcp_client import MCPTestClient
from app.sandbox.schemas import RemoteCandidate
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
