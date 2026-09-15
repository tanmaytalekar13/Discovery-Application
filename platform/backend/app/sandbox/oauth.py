"""OAuth2 helpers for connecting to remote MCP servers (MCP auth spec).

Remote MCP servers frequently refuse static API keys and require an
OAuth authorization-code flow (Notion's hosted server is OAuth-only —
it answers static tokens with 403 restricted_resource). This module
implements the client side of that flow per the MCP authorization
spec (2025-06-18) and its underlying RFCs:

  - Protected-resource metadata discovery .......... RFC 9728
  - Authorization-server metadata discovery ........ RFC 8414
  - Dynamic client registration .................... RFC 7591
  - Authorization-code grant with PKCE ............. RFC 6749 + 7636
  - Resource indicators (audience binding) ......... RFC 8707

The flow is stateful but in-memory (same trade-off as SessionStore):
`OAuthFlowStore` keeps one entry per in-flight authorization, keyed by
the CSRF `state` value, with a short TTL.

No tokens are ever logged; only hostnames and statuses.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

# How long an in-flight authorization attempt stays valid. The user has
# to complete the provider's login within this window.
FLOW_TTL_SECONDS = 600
HTTP_TIMEOUT = 15.0

_USER_AGENT = "DiscoveryApplicationBot/1.0 (+mcp-oauth)"


class OAuthFlowError(Exception):
    """Raised when the OAuth flow cannot proceed (discovery, DCR, exchange)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _public_base_url() -> str:
    """Public base URL of THIS backend, used to build the OAuth redirect.

    The OAuth provider redirects the user's browser here, so it must be
    reachable from wherever the user browses. Configure
    MCP_OAUTH_PUBLIC_BASE_URL in production (e.g. https://tools.acme.dev).
    Defaults to localhost:8000 for local development.
    """
    return os.environ.get("MCP_OAUTH_PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")


def frontend_base_url() -> str:
    """Base URL of the frontend app (post-OAuth landing page)."""
    return os.environ.get("MCP_OAUTH_FRONTEND_URL", "http://localhost:4200").rstrip("/")


def redirect_uri_for_item(item_id: str) -> str:
    """The exact redirect URI to register with the authorization server."""
    return f"{_public_base_url()}/api/items/{item_id}/test/authorize/callback"


# ---------------------------------------------------------------------------
# In-memory flow store
# ---------------------------------------------------------------------------

@dataclass
class OAuthFlowContext:
    """One in-flight authorization-code flow."""

    item_id: str
    session_id: str
    state: str
    code_verifier: str
    redirect_uri: str
    client_id: str
    client_secret: str | None
    token_endpoint: str
    resource: str  # MCP server URL, sent as RFC 8707 `resource` when supported
    created_at: float
    expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at


class OAuthFlowStore:
    """Thread-safe in-memory store of in-flight OAuth flows, keyed by state."""

    def __init__(self) -> None:
        import threading
        self._lock = threading.RLock()
        self._flows: dict[str, OAuthFlowContext] = {}

    def put(self, flow: OAuthFlowContext) -> None:
        with self._lock:
            self._evict_expired()
            self._flows[flow.state] = flow

    def take(self, state: str) -> OAuthFlowContext | None:
        """Pop the flow for `state`, or None if missing/expired."""
        with self._lock:
            self._evict_expired()
            flow = self._flows.pop(state, None)
            if flow is None or flow.is_expired:
                return None
            return flow

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [s for s, f in self._flows.items() if now > f.expires_at]
        for s in expired:
            del self._flows[s]


_flow_store = OAuthFlowStore()


def get_flow_store() -> OAuthFlowStore:
    return _flow_store


# ---------------------------------------------------------------------------
# PKCE helpers (RFC 7636)
# ---------------------------------------------------------------------------

def _make_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    return verifier, challenge


# ---------------------------------------------------------------------------
# Metadata discovery
# ---------------------------------------------------------------------------

def _well_known_candidates(mcp_url: str, well_known: str) -> list[str]:
    """Candidate URLs for a metadata document, path-aware per RFC 9728/8414.

    For https://mcp.notion.com/mcp and well-known
    `oauth-protected-resource` the first candidate is
    https://mcp.notion.com/.well-known/oauth-protected-resource/mcp —
    exactly the URL Notion advertises in its WWW-Authenticate header.
    """
    parsed = urlparse(mcp_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = []
    if path:
        candidates.append(f"{origin}/.well-known/{well_known}{path}")
    candidates.append(f"{origin}/.well-known/{well_known}")
    return candidates


async def _fetch_json_document(candidates: list[str]) -> dict | None:
    """GET each candidate in order; return the first valid JSON object."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers={"User-Agent": _USER_AGENT})
            except httpx.HTTPError:
                continue
            if resp.status_code != 200:
                continue
            try:
                doc = resp.json()
            except ValueError:
                continue
            if isinstance(doc, dict):
                return doc
    return None


async def discover_protected_resource_metadata(mcp_url: str) -> dict | None:
    """RFC 9728 protected-resource metadata for an MCP server URL."""
    return await _fetch_json_document(
        _well_known_candidates(mcp_url, "oauth-protected-resource")
    )


async def discover_authorization_server_metadata(as_url: str) -> dict | None:
    """RFC 8414 authorization-server metadata for an issuer URL.

    `as_url` may carry a path (e.g. https://auth.example.com/oauth2);
    both path-aware and origin-only candidates are tried, for both the
    OAuth and OIDC well-known names.
    """
    parsed = urlparse(as_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    names = ["oauth-authorization-server", "openid-configuration"]
    candidates: list[str] = []
    for name in names:
        if path:
            candidates.append(f"{origin}/.well-known/{name}{path}")
        candidates.append(f"{origin}/.well-known/{name}")
    return await _fetch_json_document(candidates)


async def resolve_authorization_server(mcp_url: str) -> dict:
    """Find the authorization-server metadata that guards `mcp_url`.

    Order (per MCP spec):
      1. Protected-resource metadata -> authorization_servers[0]
      2. Fallback: treat the MCP origin itself as the authorization server
    """
    prm = await discover_protected_resource_metadata(mcp_url)
    issuer = None
    if prm:
        servers = prm.get("authorization_servers") or []
        if isinstance(servers, list) and servers:
            issuer = servers[0]
    if not issuer:
        parsed = urlparse(mcp_url)
        issuer = f"{parsed.scheme}://{parsed.netloc}"

    metadata = await discover_authorization_server_metadata(str(issuer))
    if not metadata:
        raise OAuthFlowError(
            "This server's OAuth authorization metadata could not be "
            f"discovered from {issuer}. The provider may not support "
            "dynamic client registration."
        )
    if not metadata.get("authorization_endpoint") or not metadata.get("token_endpoint"):
        raise OAuthFlowError(
            "The authorization server metadata is missing required "
            "endpoints (authorization_endpoint / token_endpoint)."
        )
    return metadata


# ---------------------------------------------------------------------------
# Dynamic client registration (RFC 7591)
# ---------------------------------------------------------------------------

async def register_dynamic_client(
    registration_endpoint: str, redirect_uri: str
) -> tuple[str, str | None]:
    """Register a throwaway OAuth client; returns (client_id, client_secret)."""
    payload = {
        "client_name": "MCP Discovery Platform (tool test)",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "web",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.post(
                registration_endpoint, json=payload, headers={"User-Agent": _USER_AGENT}
            )
        except httpx.HTTPError as exc:
            raise OAuthFlowError(f"Dynamic client registration failed: {exc}") from exc
    if resp.status_code not in (200, 201):
        raise OAuthFlowError(
            f"Dynamic client registration rejected (HTTP {resp.status_code}). "
            "This provider likely requires a pre-registered OAuth client."
        )
    doc = resp.json()
    client_id = doc.get("client_id")
    if not client_id:
        raise OAuthFlowError("Dynamic client registration returned no client_id.")
    return str(client_id), doc.get("client_secret")


# ---------------------------------------------------------------------------
# Authorization URL + token exchange
# ---------------------------------------------------------------------------

def build_authorization_url(
    *,
    authorization_endpoint: str,
    token_endpoint: str,
    client_id: str,
    client_secret: str | None,
    redirect_uri: str,
    resource: str,
    item_id: str,
    session_id: str,
    scope: str | None = None,
) -> tuple[str, str, OAuthFlowContext]:
    """Build the authorize URL (PKCE S256) and return (url, state, context)."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = _make_pkce_pair()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # RFC 8707: bind the token to the MCP server we're dialing.
        "resource": resource,
    }
    if scope:
        params["scope"] = scope
    url = f"{authorization_endpoint}?{urlencode(params)}"
    flow = OAuthFlowContext(
        item_id=item_id,
        session_id=session_id,
        state=state,
        code_verifier=verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint=token_endpoint,
        resource=resource,
        created_at=time.monotonic(),
        expires_at=time.monotonic() + FLOW_TTL_SECONDS,
    )
    return url, state, flow


async def exchange_code_for_token(flow: OAuthFlowContext, code: str) -> dict:
    """Exchange an authorization code for tokens using the stored PKCE pair."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": flow.client_id,
        "code_verifier": flow.code_verifier,
    }
    if flow.client_secret:
        form["client_secret"] = flow.client_secret
    if flow.resource:
        form["resource"] = flow.resource
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.post(
                flow.token_endpoint, data=form, headers={"User-Agent": _USER_AGENT}
            )
        except httpx.HTTPError as exc:
            raise OAuthFlowError(f"Token exchange failed: {exc}") from exc
    if resp.status_code != 200:
        detail = ""
        try:
            doc = resp.json()
            detail = str(doc.get("error_description") or doc.get("error") or "")
        except ValueError:
            pass
        raise OAuthFlowError(
            f"Token exchange rejected (HTTP {resp.status_code}){' ' + detail if detail else ''}."
        )
    doc = resp.json()
    if not doc.get("access_token"):
        raise OAuthFlowError("Token exchange response contained no access_token.")
    return doc
