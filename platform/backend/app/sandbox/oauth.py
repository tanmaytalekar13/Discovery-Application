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


# Well-known OAuth scope grants for the major identity providers behind
# official MCP servers. The MCP spec's `scopes_supported` from server
# metadata is preferred when advertised; these defaults fill the gap for
# providers (notably Google and Microsoft Entra) whose MCP endpoints
# either omit scopes_supported or need their broad account scopes spelled
# out for the consent screen to show the resource the user expects.
# Keys are matched as exact host or domain suffix, because the MCP URL's
# host (e.g. calendarmcp.googleapis.com) differs from the authorization
# server's host (accounts.google.com).
DEFAULT_OAUTH_SCOPES: dict[str, str] = {
    "google.com": (
        "openid email profile "
        "https://www.googleapis.com/auth/drive "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.modify "
        "https://www.googleapis.com/auth/documents "
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/presentations "
        "https://www.googleapis.com/auth/chat.spaces "
        "https://www.googleapis.com/auth/tasks"
    ),
    "googleapis.com": (
        "openid email profile "
        "https://www.googleapis.com/auth/drive "
        "https://www.googleapis.com/auth/calendar "
        "https://www.googleapis.com/auth/gmail.modify "
        "https://www.googleapis.com/auth/documents "
        "https://www.googleapis.com/auth/spreadsheets "
        "https://www.googleapis.com/auth/presentations "
        "https://www.googleapis.com/auth/chat.spaces "
        "https://www.googleapis.com/auth/tasks"
    ),
    "microsoftonline.com": (
        "openid email profile offline_access "
        "https://graph.microsoft.com/.default"
    ),
    "microsoft.com": (
        "openid email profile offline_access "
        "https://graph.microsoft.com/.default"
    ),
    "cloud.microsoft": (
        "openid email profile offline_access "
        "https://graph.microsoft.com/.default"
    ),
    "azure.com": (
        "openid email profile offline_access "
        "https://graph.microsoft.com/.default"
    ),
    "github.com": "repo read:org read:user",
    "gitlab.com": "api read_user",
    "slack.com": "identity.basic identity.email",
}


def default_scopes_for(mcp_url: str, as_metadata: dict | None = None) -> str | None:
    """Pick a sensible scope grant for the provider behind `mcp_url`.

    Order: the authorization server's advertised scopes_supported (the
    server knows best), then the host-suffix defaults above, then None
    (send no scope parameter and let the provider apply its default).
    """
    if as_metadata:
        supported = as_metadata.get("scopes_supported")
        if isinstance(supported, list) and supported:
            return " ".join(str(s) for s in supported)
    host = (urlparse(mcp_url).hostname or "").lower()
    if not host:
        return None
    for suffix, scopes in DEFAULT_OAUTH_SCOPES.items():
        if host == suffix or host.endswith("." + suffix):
            return scopes
    return None


# ---------------------------------------------------------------------------
# Pre-registered OAuth clients (for providers without dynamic registration)
# ---------------------------------------------------------------------------

# Sentinel item id used in the redirect URI for pre-registered (no-DCR)
# providers. Their redirect URIs are registered once per provider in the
# provider's developer console, so the callback URL must be stable and can
# never contain a per-item UUID (the callback route parses item_id as a
# UUID, hence the nil UUID rather than a word). The real item context
# travels in the CSRF `state` (the OAuthFlowContext), so one redirect
# serves every item.
OAuthRedirectItem = "00000000-0000-0000-0000-000000000000"

# Several major providers (Slack, Google, GitHub Apps, Entra) do NOT offer
# RFC 7591 dynamic client registration — their MCP servers still speak the
# rest of the MCP auth spec, but the app must present a client_id from the
# provider's developer console. Those are configured once per provider via
# env vars and reused for every server behind that issuer.
_PRE_REGISTERED_CLIENT_ENV: dict[str, str] = {
    "slack.com": "SLACK",
    "google.com": "GOOGLE",
    "googleapis.com": "GOOGLE",
    "github.com": "GITHUB",
    "gitlab.com": "GITLAB",
    "microsoftonline.com": "ENTRA",
    "microsoft.com": "ENTRA",
    "cloud.microsoft": "ENTRA",
    "azure.com": "ENTRA",
    "linear.app": "LINEAR",
    "notion.com": "NOTION",
    "atlassian.com": "ATLASSIAN",
    "sentry.dev": "SENTRY",
    "vercel.com": "VERCEL",
    "supabase.com": "SUPABASE",
    "stripe.com": "STRIPE",
    "huggingface.co": "HUGGINGFACE",
}


def _issuer_host(mcp_url: str, as_metadata: dict | None = None) -> str:
    """The authorization server's host (falls back to the MCP origin)."""
    issuer = (as_metadata or {}).get("issuer") or mcp_url
    return (urlparse(str(issuer)).hostname or "").lower()


def preregistered_client_for(
    mcp_url: str, as_metadata: dict | None = None
) -> tuple[str, str | None] | None:
    """Look up a pre-registered OAuth client for the issuer behind `mcp_url`.

    Reads MCP_OAUTH_CLIENT_ID_<SLUG> (required) and
    MCP_OAUTH_CLIENT_SECRET_<SLUG> (optional; PKCE public clients omit it).
    Returns (client_id, client_secret) or None when unset.
    """
    host = _issuer_host(mcp_url, as_metadata)
    for suffix, slug in _PRE_REGISTERED_CLIENT_ENV.items():
        if host == suffix or host.endswith("." + suffix):
            client_id = os.environ.get(f"MCP_OAUTH_CLIENT_ID_{slug}", "").strip()
            if client_id:
                secret = os.environ.get(f"MCP_OAUTH_CLIENT_SECRET_{slug}", "").strip()
                return client_id, (secret or None)
    return None


def preregistered_env_hint(mcp_url: str, as_metadata: dict | None = None) -> str:
    """The env var name a user would set for this issuer (for error copy)."""
    host = _issuer_host(mcp_url, as_metadata)
    for suffix, slug in _PRE_REGISTERED_CLIENT_ENV.items():
        if host == suffix or host.endswith("." + suffix):
            return f"MCP_OAUTH_CLIENT_ID_{slug}"
    return "MCP_OAUTH_CLIENT_ID_<PROVIDER>"


def prereg_redirect_uri(mcp_url: str, as_metadata: dict | None = None) -> str | None:
    """Stable callback URL for pre-registered providers, or None.

    Providers without dynamic registration have their redirect URIs
    registered once per provider, so the callback must not vary per item.
    MCP_OAUTH_REDIRECT_BASE_URL (e.g. https://tunnel.example.com when the
    app runs behind a tunnel) builds that stable callback. Without it the
    caller falls back to the per-item redirect URI, which only works when
    the developer happened to register exactly that URI.
    """
    host = _issuer_host(mcp_url, as_metadata)
    for suffix, slug in _PRE_REGISTERED_CLIENT_ENV.items():
        if host == suffix or host.endswith("." + suffix):
            base = os.environ.get("MCP_OAUTH_REDIRECT_BASE_URL", "").strip().rstrip("/")
            if base:
                return f"{base}/api/items/{OAuthRedirectItem}/test/authorize/callback"
            return None
    return None


def redirect_base_hint(mcp_url: str, as_metadata: dict | None = None) -> str:
    """Human-readable callback path for error copy (base URL unknown here)."""
    host = _issuer_host(mcp_url, as_metadata)
    for suffix, slug in _PRE_REGISTERED_CLIENT_ENV.items():
        if host == suffix or host.endswith("." + suffix):
            return f"$MCP_OAUTH_REDIRECT_BASE_URL/api/items/{OAuthRedirectItem}/test/authorize/callback"
    return "$MCP_OAUTH_REDIRECT_BASE_URL/api/items/<item_id>/test/authorize/callback"


def preregistered_redirect_supported() -> bool:
    """Whether a stable, item-independent OAuth callback is configured.

    MCP_OAUTH_REDIRECT_BASE_URL names the externally reachable base URL
    the developer registered with the provider (the same host for every
    item), e.g. a tunnel hostname during local development. When set,
    pre-registered (no-DCR) providers redirect to
    {base}/api/items/<nil-uuid>/test/authorize/callback instead of a
    per-item callback the provider would reject as unregistered.
    """
    return bool(os.environ.get("MCP_OAUTH_REDIRECT_BASE_URL", "").strip())


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
    # Catalog item the eventual token lands on. Differs from item_id when the
    # redirect URI is the stable OAuthRedirectItem callback.
    session_item_id: str = ""

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
        # RFC 8414 fallback: if the issuer URL carries a path component, the
        # metadata may also live path-aware at the origin root. Slack is the
        # real-world case: its MCP server (https://mcp.slack.com) is its own
        # authorization server and serves its metadata at
        # /well-known/oauth-authorization-server (no leading dot), while
        # /slack.com returns the unrelated OpenID document (no PKCE, no MCP
        # scopes). Trying the MCP origin's path-aware candidate finds it.
        parsed = urlparse(str(issuer))
        if parsed.path:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            metadata = await discover_authorization_server_metadata(origin)
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

    # The MCP server's protected-resource metadata often advertises the
    # resource scopes the MCP tools actually need (Slack advertises 30 MCP
    # scopes there; its AS metadata and OpenID document advertise only
    # openid/profile/email). Merge those scopes into the metadata so scope
    # selection prefers the resource's own list before provider defaults.
    if prm:
        prm_scopes = prm.get("scopes_supported")
        if isinstance(prm_scopes, list) and prm_scopes:
            metadata = {**metadata, "scopes_supported": prm_scopes}
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
    session_item_id: str | None = None,
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
        session_item_id=session_item_id or item_id,
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


async def refresh_provider_token(
    token_endpoint: str,
    refresh_token: str,
    client_id: str,
    client_secret: str | None = None,
) -> dict:
    """Exchange a stored refresh token for fresh tokens (RFC 6749 Section 6).

    Used by the verification pipeline so an auth-gated server whose
    access token expired can be re-verified WITHOUT a new human consent
    round-trip: the provider-scoped refresh token (stored at consent
    time) is replayed against the same token_endpoint the original flow
    used. Returns the raw token document (access_token, optional new
    refresh_token, expires_in, scope).
    """
    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        form["client_secret"] = client_secret
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        try:
            resp = await client.post(
                token_endpoint, data=form, headers={"User-Agent": _USER_AGENT}
            )
        except httpx.HTTPError as exc:
            raise OAuthFlowError(f"Token refresh failed: {exc}") from exc
    if resp.status_code != 200:
        detail = ""
        try:
            doc = resp.json()
            detail = str(doc.get("error_description") or doc.get("error") or "")
        except ValueError:
            pass
        raise OAuthFlowError(
            f"Token refresh rejected (HTTP {resp.status_code})"
            f"{' ' + detail if detail else ''}."
        )
    doc = resp.json()
    if not doc.get("access_token"):
        raise OAuthFlowError("Token refresh response contained no access_token.")
    return doc
