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

import json
import logging
import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response as FastAPIResponse

from app.api.dependencies import get_item_repository
from app.artifacts.source_resolver import resolve_source
from app.api.schemas import (
    ManualTokenRequest,
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
from app.sandbox.mcp_client import MCPTestClient, classify_connection_error
from app.sandbox.oauth import (
    OAuthFlowError,
    OAuthRedirectItem,
    build_authorization_url,
    default_scopes_for,
    exchange_code_for_token,
    frontend_base_url,
    get_flow_store,
    prereg_redirect_uri,
    preregistered_client_for,
    preregistered_env_hint,
    preregistered_redirect_supported,
    redirect_base_hint,
    redirect_uri_for_item,
    register_dynamic_client,
    resolve_authorization_server,
)
from app.sandbox.schemas import GithubSourceHint, LocalPackageHint, LocalRunConfig, RemoteCandidate
from app.sandbox.session import get_session_store, SessionStore

router = APIRouter(prefix="/api/items", tags=["tool-test"])
logger = logging.getLogger(__name__)


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
    # GitHub repositories are inspected lazily. A hosted endpoint discovered
    # there uses the exact same remote client/auth flow as registry remotes;
    # it must never be put in a sandbox.
    if classification.mode == "local_source":
        item = await _require_item(item_id, repository)
        config = await _get_or_extract_run_config(item_id, item)
        if config.remote:
            return config.remote, config.remote.url

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

    # A single generic Exception handler is used here on purpose (rather than
    # a narrower `except MCPClientError` plus a hand-rolled fallback for
    # everything else). MCPTestClient.connect() already turns most failures
    # into a graceful ConnectResult internally, but URL validation
    # (UnsafeURLError) happens synchronously in the constructor, and some
    # exception types (e.g. mcp.shared.exceptions.McpError, or any future
    # exception type we haven't special-cased) can still escape uncaught.
    # Always routing every exception through classify_connection_error()
    # means the frontend consistently gets show_token_input/show_oauth_button
    # set from the same fallback logic used everywhere else, instead of a
    # dead-end error with no way for the user to retry with credentials.
    try:
        client = MCPTestClient(
            url=url,
            auth_token=token,
            preferred_transport=candidate.type,
            auth_header=candidate.auth_header,
            auth_value_prefix=candidate.auth_value_prefix,
        )
        result = await client.connect()
    except Exception as exc:
        logger.exception(
            "Remote MCP connection failed (item_id=%s, transport=%s, url=%s)",
            item_id, candidate.type, url,
        )
        info = classify_connection_error(exc, url=url)
        return ToolConnectResponse(
            connected=False, auth_required=info.auth_reason == "unauthorized",
            transport=candidate.type, error=info.user_message,
            requires_auth=info.auth_reason == "unauthorized",
            auth_reason=info.auth_reason, user_message=info.user_message,
            show_token_input=info.show_token_input, show_oauth_button=info.show_oauth_button,
        )

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
        auth_value_prefix=candidate.auth_value_prefix,
    )

    # MCPTestClient.invoke() already catches its own known error types and
    # returns a graceful InvokeResult, but - just like connect() above - an
    # exception type it doesn't recognize (e.g. McpError, or anything new)
    # can still escape uncaught. Without this guard the endpoint would
    # surface a raw 500 to the frontend instead of a structured
    # ToolInvokeResponse the UI knows how to render (including an
    # auth-retry affordance when relevant).
    try:
        result = await client.invoke(body.tool_name, body.arguments)
    except Exception as exc:
        logger.exception(
            "Remote MCP invoke failed (item_id=%s, tool=%s, url=%s)",
            item_id, body.tool_name, url,
        )
        info = classify_connection_error(exc, url=url)
        return ToolInvokeResponse(
            status="error",
            error=info.user_message,
            requires_auth=info.auth_reason == "unauthorized",
            auth_reason=info.auth_reason,
            user_message=info.user_message,
            show_token_input=info.show_token_input,
            show_oauth_button=info.show_oauth_button,
        )

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
    """Discover the server's OAuth metadata and build an authorize URL.

    Implements the client side of the MCP authorization spec:
      1. RFC 9728 protected-resource metadata (fallback: MCP origin)
      2. RFC 8414 authorization-server metadata
      3. RFC 7591 dynamic client registration (when offered)
      4. Authorization-code + PKCE (S256) authorize URL with RFC 8707
         `resource` binding
    The user completes the flow in a new tab; the provider redirects to
    /test/authorize/callback, which stores the token on the session.
    """
    candidate, url = await _require_testable(item_id, repository)

    # Ensure we have a session to attach the eventual token to.
    if session_id:
        existing = session_store.get_session(session_id)
        if existing is None:
            session_id = None  # treat as missing; create new
    if session_id is None:
        session = session_store.create_session(item_id)
        session_id = session.session_id

    try:
        as_metadata = await resolve_authorization_server(url)
    except OAuthFlowError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    client_id: str | None = None
    client_secret: str | None = None
    registration_endpoint = as_metadata.get("registration_endpoint")
    prereg = (
        preregistered_client_for(url, as_metadata)
        if not registration_endpoint
        else None
    )
    if prereg and not preregistered_redirect_supported():
        # A pre-registered client without a stable redirect can only produce
        # a per-item callback the provider has never seen registered — fail
        # fast with the fix instead of bouncing the user to a provider error
        # page halfway through consent.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "A pre-registered OAuth client is configured for this provider "
                f"({preregistered_env_hint(url, as_metadata)}), but the app has "
                "no stable OAuth redirect URI: set MCP_OAUTH_REDIRECT_BASE_URL "
                "to the externally reachable base URL whose "
                f"/api/items/{OAuthRedirectItem}/test/authorize/callback you "
                "registered with the provider (e.g. your tunnel host), then "
                "retry. Alternatively provide an API token if the server "
                "accepts one."
            ),
        )
    if prereg and preregistered_redirect_supported():
        # Providers without dynamic registration have their redirect URIs
        # registered once per provider, so the callback must be the stable,
        # item-independent one (the real item context rides in the CSRF
        # `state`). Redirecting back into the app from there resumes the flow.
        redirect_uri = prereg_redirect_uri(url, as_metadata) or redirect_uri_for_item(str(item_id))
    else:
        redirect_uri = redirect_uri_for_item(str(item_id))
    if registration_endpoint:
        try:
            client_id, client_secret = await register_dynamic_client(
                str(registration_endpoint), redirect_uri
            )
        except OAuthFlowError as exc:
            logger.warning("Dynamic client registration failed for %s: %s", url, exc)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{exc} If you control this server, pre-register an OAuth "
                    "client or enable dynamic registration."
                ),
            ) from exc
    elif prereg:
        client_id, client_secret = prereg
        logger.info(
            "Using pre-registered OAuth client for %s (no DCR, redirect=%s)",
            url,
            redirect_uri,
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This MCP server's authorization server does not offer "
                "dynamic client registration, so the app needs a "
                "pre-registered OAuth client for it: set "
                f"{preregistered_env_hint(url, as_metadata)} (and optionally "
                "MCP_OAUTH_CLIENT_SECRET_<PROVIDER>) plus "
                "MCP_OAUTH_REDIRECT_BASE_URL — register the callback "
                f"{redirect_base_hint(url, as_metadata)} as the OAuth redirect "
                "URI in the provider's developer console — in the backend "
                "environment, then retry. Alternatively provide an API token "
                "if the server accepts one."
            ),
        )

    authorization_url, oauth_state, flow = build_authorization_url(
        authorization_endpoint=str(as_metadata["authorization_endpoint"]),
        token_endpoint=str(as_metadata["token_endpoint"]),
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        resource=url,
        item_id=str(item_id),
        session_id=session_id,
        # Prefer the server's advertised scopes; fall back to provider
        # defaults (Google / Entra consent screens need explicit resource
        # scopes or they grant only an identity token).
        scope=default_scopes_for(url, as_metadata),
        # When the redirect is the stable oauth-redirect callback, the token
        # must land on this item's session, not on the sentinel item.
        session_item_id=str(item_id),
    )
    get_flow_store().put(flow)
    # Keep the legacy session-state binding in sync for compatibility.
    session_store.set_oauth_state(session_id, oauth_state)

    logger.info(
        "OAuth start (item=%s url=%s issuer=%s dcr=ok state=%s)",
        item_id, url.split("?", 1)[0],
        str(as_metadata.get("issuer") or "")[:100], oauth_state[:8],
    )
    return OAuthStartResponse(
        authorization_url=authorization_url,
        state=oauth_state,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# GET /items/{item_id}/test/authorize/callback
# ---------------------------------------------------------------------------

_CALLBACK_SUCCESS_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="3;url={frontend_url}/">
<title>Authorization complete</title></head>
<body style="font-family: system-ui; text-align:center; padding-top:4rem">
<h2>&#10003; Authorization complete</h2>
<p>The MCP server token was stored. You can close this tab and
retry the connection in the app.</p>
</body></html>"""

_CALLBACK_FAILURE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Authorization failed</title></head>
<body style="font-family: system-ui; text-align:center; padding-top:4rem">
<h2>&#10007; Authorization failed</h2>
<p>{message}</p>
<p>Close this tab and try connecting again.</p>
</body></html>"""


@router.get(
    "/{item_id}/test/authorize/callback",
    summary="OAuth callback",
    response_class=FastAPIResponse,
)
async def authorize_callback(
    item_id: UUID,
    request: Request,
    code: Annotated[str | None, Query(description="Authorization code from OAuth provider")] = None,
    state: Annotated[str, Query(description="CSRF state from authorize/start")] = "",
    error: Annotated[str | None, Query(description="OAuth error code, if the user denied access")] = None,
):
    """Receive the provider redirect, exchange the code, store the token.

    The flow context (PKCE verifier, client, token endpoint) was stored at
    /authorize/start under the CSRF `state`; it is consumed exactly once
    here. On success the access token lands on the test session and the
    browser gets a small confirmation page (close the tab and retry
    connect in the app).
    """
    frontend_url = frontend_base_url()
    if error:
        detail = request.query_params.get("error_description") or error
        return HTMLResponse(
            _CALLBACK_FAILURE_HTML.format(message=f"Provider said: {detail}"),
            status_code=400,
        )
    if not code or not state:
        return HTMLResponse(
            _CALLBACK_FAILURE_HTML.format(message="Missing code or state parameter."),
            status_code=400,
        )

    flow = get_flow_store().take(state)
    if flow is None or flow.item_id not in {str(item_id), OAuthRedirectItem}:
        return HTMLResponse(
            _CALLBACK_FAILURE_HTML.format(
                message="Unknown or expired OAuth state (possible CSRF attack). Please start the flow again."
            ),
            status_code=400,
        )
    # The catalog item the token belongs to. With the stable oauth-redirect
    # callback the provider redirects to the sentinel item id; the real item
    # context was stored in the flow at /authorize/start.
    target_item_id = flow.session_item_id or flow.item_id

    try:
        token_doc = await exchange_code_for_token(flow, code)
    except OAuthFlowError as exc:
        logger.warning("OAuth token exchange failed (item=%s): %s", item_id, exc)
        return HTMLResponse(
            _CALLBACK_FAILURE_HTML.format(message=str(exc)),
            status_code=400,
        )

    # Store the token on the session created at /authorize/start.
    store = get_session_store()
    session = store.get_session(flow.session_id)
    if session is None:
        session = store.create_session(UUID(target_item_id))
    store.store_token(
        session.session_id,
        access_token=str(token_doc["access_token"]),
        token_type=str(token_doc.get("token_type") or "Bearer"),
        refresh_token=(str(token_doc["refresh_token"]) if token_doc.get("refresh_token") else None),
        auth_method="oauth2",
    )
    logger.info(
        "OAuth callback success (item=%s session=%s token_len=%d)",
        target_item_id, session.session_id[:8], len(str(token_doc["access_token"])),
    )
    # With the stable oauth-redirect callback, the provider lands on the
    # sentinel item id; bounce the browser into the app so the test dialog
    # (already polling /authorize/poll for its session) resumes by itself.
    if str(item_id) != target_item_id:
        return RedirectResponse(url=f"{frontend_url}/", status_code=302)
    return HTMLResponse(
        _CALLBACK_SUCCESS_HTML.format(frontend_url=frontend_url),
        status_code=200,
    )


# ---------------------------------------------------------------------------
# GET /items/{item_id}/test/authorize/poll  (Claude-style auto-resume)
# ---------------------------------------------------------------------------

@router.get(
    "/{item_id}/test/authorize/poll",
    summary="Poll whether an OAuth flow finished and auto-connect",
)
async def authorize_poll(
    item_id: UUID,
    session_id: Annotated[str, Query(description="Test-session ID from authorize/start")],
    repository: ItemRepository = Depends(get_item_repository),
    session_store: SessionStore = Depends(get_session_store),
):
    """Check whether the OAuth token for this session has landed.

    The user completes consent in a separate tab; the provider redirects
    to /test/authorize/callback which stores the token on this session.
    The test dialog polls this endpoint every ~2s (like Claude's connector
    flow) so the tools list appears the moment authorization completes —
    no manual 'I'm done, retry' click needed.

    Returns `{connected: true, tools: [...]}` once the token exists and a
    real initialize+tools/list succeeds against the MCP server; a plain
    `{connected: false}` heartbeat otherwise. Never raises for 'not yet'.
    """
    session = session_store.get_session(session_id)
    if session is None or session.access_token is None:
        return {"connected": False, "tools": []}

    # Token landed: immediately attempt a real connect with it.
    try:
        candidate, url = await _require_testable(item_id, repository)
        client = MCPTestClient(
            url=url,
            auth_token=session.access_token,
            preferred_transport=candidate.type,
            auth_header=candidate.auth_header,
            auth_value_prefix=candidate.auth_value_prefix,
        )
        result = await client.connect()
    except Exception as exc:  # noqa: BLE001 - poll must stay a heartbeat
        logger.info(
            "OAuth poll connect attempt failed (item=%s): %s", item_id, exc
        )
        return {"connected": False, "tools": [], "token_received": True}

    if not result.connected:
        return {
            "connected": False,
            "tools": [],
            "token_received": True,
            "error": result.user_message or result.error,
        }

    return {
        "connected": True,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
            }
            for t in result.tools
        ],
    }


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
    await _require_item(item_id, repository)
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
    body: ManualTokenRequest | None = None,
    token: Annotated[str | None, Query(description="Bearer token or API key (deprecated; send in body)")] = None,
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

    # Preferred: token in JSON body. Kept as a deprecated query-param
    # fallback for older clients — query params end up in access logs.
    raw_token = (body.token if body is not None else None) or token or ""
    body_session_id = body.session_id if body is not None else None
    effective_session_id = session_id or body_session_id
    if not raw_token.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A non-empty 'token' is required.",
        )

    if effective_session_id is None:
        session = session_store.create_session(item_id)
    else:
        existing = session_store.get_session(effective_session_id)
        if existing is None:
            session = session_store.create_session(item_id)
        else:
            session = existing

    # Trim whitespace from user input before storage
    trimmed_token = raw_token.strip() if isinstance(raw_token, str) else raw_token
    session_store.store_token(
        session.session_id,
        access_token=trimmed_token,
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
# Credential-name extraction from README-documented env configuration
# ---------------------------------------------------------------------------

_SECRET_NAME_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH")


def _env_name_is_secret(name: str) -> bool:
    return any(marker in name.upper() for marker in _SECRET_NAME_MARKERS)


async def _collect_credential_env_names(item) -> list[str]:
    """Credential env var names from the item's README/source evidence.

    Uses the same resolution chain as the Source preview tab: inline
    artifacts.source_code first, then the disk cache, then a GitHub
    README fetch (cached on first download). Mirrors the mcpServers JSON
    convention ("env": {"BEARER_TOKEN": ...}) used by Claude Desktop /
    Cursor configs: registry packages that declare no
    environment_variables metadata usually DO document credentials this
    way.
    """
    source_code = None
    if item is not None and item.artifacts is not None:
        source_code = item.artifacts.source_code
    if not source_code:
        try:
            preview, _ = await resolve_source(item)
        except Exception:  # noqa: BLE001 - README fetch is best-effort
            return []
        source_code = preview.content if preview and preview.available else None
    if not source_code:
        return []
    return _extract_env_names_from_text(source_code)


def _extract_env_names_from_artifacts(item) -> list[str]:
    """Synchronous inline-only variant kept for direct reuse/tests."""
    try:
        source_code = (item.artifacts.source_code if item and item.artifacts else "") or ""
    except AttributeError:
        return []
    if not source_code:
        return []
    return _extract_env_names_from_text(source_code)


_ENV_NAME_RE = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")


def _extract_env_names_from_text(source_code: str) -> list[str]:
    """Extract credential env var names from README/config text.

    Two evidence tiers, mirroring sandbox/extract.py's approach:
    1. names inside an `env` block of an mcpServers-style JSON config
       (the strongest signal — Claude Desktop / Cursor convention);
    2. ALL_CAPS credential-looking names anywhere in the text (READMEs
       commonly write `SLACK_BOT_TOKEN=xoxb-...` in a shell example even
       when no JSON config block exists).
    """
    names: list[str] = []
    for block in re.findall(r"```(?:json)?\s*\n(.*?)```", source_code, re.DOTALL):
        try:
            config = json.loads(block)
        except ValueError:
            continue
        servers = config.get("mcpServers") if isinstance(config, dict) else None
        candidates = servers.values() if isinstance(servers, dict) else [config]
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            env = entry.get("env")
            if isinstance(env, dict):
                names.extend(str(key) for key in env if isinstance(key, str))

    if names:
        return list(dict.fromkeys(names))[:8]

    # Prose/shell-example fallback: only names that read as credentials.
    for name in _ENV_NAME_RE.findall(source_code):
        if _env_name_is_secret(name) and name not in (
            "API_KEY",
            "API_TOKEN",
            "ACCESS_TOKEN",
            "AUTH_TOKEN",
            "BEARER_TOKEN",
        ):
            names.append(name)
    return list(dict.fromkeys(names))[:8]


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

    # Many registry packages declare no environment_variables metadata at
    # all, yet their README documents exactly how credentials must be
    # supplied (an `env` block in the mcpServers JSON, e.g.
    # "BEARER_TOKEN": "ghp_..."). Extract those names so the credential
    # form matches what the server actually expects instead of a generic
    # API_KEY placeholder.
    if not env_vars:
        item = await repository.get(item_id)
        readme_env = await _collect_credential_env_names(item) if item else []
        env_vars = [
            EnvironmentVariableSchema(
                name=name,
                description="Documented in the server's setup instructions.",
                is_secret=_env_name_is_secret(name),
                is_required=True,
            )
            for name in readme_env
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

    # Registry metadata is authoritative for package-level configuration.
    # Do not attempt a stdio handshake until required values are present.
    required_env = [
        value.get("name") for value in hint.environment_variables
        if isinstance(value, dict) and value.get("isRequired", value.get("is_required", True))
        and isinstance(value.get("name"), str)
    ]
    missing = [name for name in required_env if not body.env_vars.get(name)]
    if missing:
        return LocalConnectResponse(
            connected=False,
            error="Missing required environment variables: " + ", ".join(missing),
            user_message="This MCP server requires configuration before it can start.",
            required_env_vars=missing,
            show_token_input=True,
            show_retry=False,
        )

    # Trim env vars before injection (BUG 2 whitespace corruption)
    trimmed_env = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.env_vars.items()}
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
            env_vars=trimmed_env,
            allowed_domains=hint.allowed_domains,
        )

        # Connect to MCP server inside container
        command = config.build_command()
        mcp_client = LocalMCPClient()

        # Run connect (this runs the command inside the Docker container)
        connect_result = await mcp_client.connect(
            command=command,
            env_vars=trimmed_env,
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

    response_fields = dict(
        item_id=item_id,
        source=run_config.source,
        runtime=run_config.runtime,
        install_command=run_config.install_command,
        environment_variables=run_config.env_vars,
        execution_type=run_config.execution_type,
        transport=run_config.remote.type if run_config.remote else run_config.transport,
        endpoint=run_config.remote.url if run_config.remote else None,
        required_configuration=[
            EnvironmentVariableSchema(name=value.name, description=value.description,
                is_secret=value.is_secret, is_required=value.is_required)
            for value in run_config.required_env
        ],
        external_dependencies=run_config.external_dependencies,
        evidence=run_config.evidence,
        confidence=run_config.confidence,
    )
    # For a repository supporting both modes, remote is preferred for a
    # discovery test: it is the least invasive, already-running option.
    if run_config.remote:
        return SourcePrepareResponse(status="remote", **response_fields)

    if not run_config.command:
        # Heuristic tier with install-only info, or nothing structured
        # found at all — same "can install, can't run" case flagged in
        # extract.py's heuristic tier docstring.
        return SourcePrepareResponse(
            status="not_runnable",
            reason="No safe server command was found from repository evidence. "
                   "Review the repository's setup instructions before testing it.",
            candidates=run_config.candidates,
            **response_fields,
        )

    return SourcePrepareResponse(
        status="needs_auth" if run_config.env_vars else "ready",
        **response_fields,
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

    if run_config.remote:
        raise HTTPException(status_code=400, detail="Remote endpoint detected; use /test/connect so MCP is initialized over HTTP/SSE.")

    if not run_config.command:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No runnable command was found for this repo. Call "
                   "/test/source/prepare first to see why.",
        )

    # Trim env vars before injection (BUG 2 whitespace corruption)
    trimmed_env = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.env_vars.items()}
    missing = [v for v in run_config.env_vars if v not in trimmed_env]
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
            env_vars=trimmed_env,
        )

        mcp_client = LocalMCPClient()
        connect_result = await mcp_client.connect(
            command=[run_config.command, *run_config.args],
            env_vars=trimmed_env,
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
            user_message=f"Sandbox startup failed: {exc}",
        )