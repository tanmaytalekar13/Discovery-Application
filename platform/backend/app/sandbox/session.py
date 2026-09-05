"""In-memory session store for test-session tokens.

OAuth tokens are stored here while the user completes the OAuth flow.
Each token is keyed by `(item_id, session_id)` so multiple users can
test the same item concurrently without collision.

Tokens are encrypted at rest (Fernet symmetric encryption) and
auto-expire after `TTL_SECONDS`. When a test session ends (user
closes the panel or hits /disconnect) the token is discarded
immediately.

For production at scale this should be replaced by Redis with
the same TTL and encryption guarantees.
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# How long a token lives after the user completes OAuth (or after a
# test session is established). Short enough that leaked tokens have
# limited blast radius; long enough that the user can complete a test
# flow without re-authorizing mid-session.
TOKEN_TTL_SECONDS = 60 * 30  # 30 minutes
CLEANUP_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

# Fernet key must be 32 url-safe base64-encoded bytes.
# In production this should come from an environment variable or a
# secrets manager. For local dev we generate a random key on startup
# and log it (so operators can decode tokens if needed for debugging).
_FERNET_KEY: bytes | None = None


def _get_fernet_key() -> bytes:
    global _FERNET_KEY
    if _FERNET_KEY is None:
        import os

        raw = os.environ.get("MCP_SESSION_KEY")
        if raw:
            _FERNET_KEY = raw.encode("utf-8")
        else:
            # Generate a new random key and encode it so it can be
            # set via env in production: export MCP_SESSION_KEY="$(cat key.txt)"
            _FERNET_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32))
            logger.warning(
                "MCP_SESSION_KEY not set; using ephemeral in-process key. "
                "Tokens will not survive app restart."
            )
    return _FERNET_KEY


def _encrypt(data: str) -> str:
    import cryptography.fernet
    key = _get_fernet_key()
    # Ensure key is exactly 32 url-safe base64-encoded bytes
    f = cryptography.fernet.Fernet(base64.urlsafe_b64encode(key[:32]))
    return f.encrypt(data.encode()).decode()


def _decrypt(token: str) -> str:
    import cryptography.fernet
    key = _get_fernet_key()
    f = cryptography.fernet.Fernet(base64.urlsafe_b64encode(key[:32]))
    return f.decrypt(token.encode()).decode()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TestSession:
    """A single test session bound to one user + one item."""

    item_id: UUID
    session_id: str
    created_at: float
    expires_at: float
    oauth_state: str | None = None  # OAuth state param for CSRF protection
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str | None = None
    auth_method: str | None = None  # "oauth2", "bearer", "api_key"

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def to_token_payload(self) -> dict[str, Any]:
        return {
            "item_id": str(self.item_id),
            "session_id": self.session_id,
            "oauth_state": self.oauth_state,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "auth_method": self.auth_method,
        }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SessionStore:
    """Thread-safe in-memory store with TTL-based eviction.

    Stores encrypted token data. Each entry is keyed by session_id.
    Expired entries are lazily cleaned up on access and by a background
    thread.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # session_id -> encrypted JSON string
        self._entries: dict[str, str] = {}
        self._last_cleanup = time.monotonic()

    # ---- public API --------------------------------------------------------

    def create_session(self, item_id: UUID) -> TestSession:
        """Create a new test session and return it (not yet authenticated)."""
        session_id = secrets.token_urlsafe(32)
        now = time.monotonic()
        session = TestSession(
            item_id=item_id,
            session_id=session_id,
            created_at=now,
            expires_at=now + TOKEN_TTL_SECONDS,
        )
        with self._lock:
            self._store(session)
        logger.info("Created test session %s for item %s", session_id, item_id)
        return session

    def get_session(self, session_id: str) -> TestSession | None:
        """Fetch a session, checking expiry. Returns None if missing or expired."""
        with self._lock:
            raw = self._entries.get(session_id)
            if raw is None:
                return None
            session = self._load(session_id, raw)
            if session is None:
                return None
            if session.is_expired:
                self._evict(session_id)
                return None
            return session

    def store_token(
        self,
        session_id: str,
        *,
        access_token: str,
        token_type: str = "Bearer",
        refresh_token: str | None = None,
        auth_method: str = "oauth2",
        ttl_seconds: int | None = None,
    ) -> TestSession | None:
        """Store the OAuth token pair and return the updated session."""
        with self._lock:
            raw = self._entries.get(session_id)
            if raw is None:
                return None
            session = self._load(session_id, raw)
            if session is None:
                return None
            now = time.monotonic()
            session.access_token = access_token
            session.token_type = token_type
            session.refresh_token = refresh_token
            session.auth_method = auth_method
            session.expires_at = now + (ttl_seconds or TOKEN_TTL_SECONDS)
            self._store(session)
            logger.info(
                "Stored %s token for session %s (expires in %ds)",
                auth_method, session_id, ttl_seconds or TOKEN_TTL_SECONDS,
            )
            return session

    def set_oauth_state(self, session_id: str, state: str) -> bool:
        """Associate an OAuth state param with a session (for CSRF check on callback)."""
        with self._lock:
            raw = self._entries.get(session_id)
            if raw is None:
                return False
            session = self._load(session_id, raw)
            if session is None:
                return False
            session.oauth_state = state
            self._store(session)
            return True

    def get_oauth_state(self, session_id: str) -> str | None:
        """Return the stored OAuth state (or None)."""
        session = self.get_session(session_id)
        return session.oauth_state if session else None

    def revoke_session(self, session_id: str) -> bool:
        """Immediately discard a session and its tokens."""
        with self._lock:
            if session_id in self._entries:
                self._evict(session_id)
                logger.info("Revoked test session %s", session_id)
                return True
            return False

    def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns the count of evicted entries."""
        with self._lock:
            now = time.monotonic()
            if now - self._last_cleanup < CLEANUP_INTERVAL_SECONDS:
                return 0
            self._last_cleanup = now
            before = len(self._entries)
            expired = [
                sid for sid, raw in list(self._entries.items())
                if self._load(sid, raw) is None
                or self._load(sid, raw).is_expired  # type: ignore[union-attr]
            ]
            for sid in expired:
                self._evict(sid)
            evicted = before - len(self._entries)
            if evicted:
                logger.info("Session store: evicted %d expired entries", evicted)
            return evicted

    def stats(self) -> dict[str, Any]:
        """Return store statistics for monitoring / health checks."""
        with self._lock:
            return {
                "total_sessions": len(self._entries),
            }

    # ---- internals ---------------------------------------------------------

    def _store(self, session: TestSession) -> None:
        payload = json.dumps(session.to_token_payload())
        encrypted = _encrypt(payload)
        self._entries[session.session_id] = encrypted

    def _load(self, session_id: str, raw: str) -> TestSession | None:
        try:
            decrypted = _decrypt(raw)
            data = json.loads(decrypted)
            session = TestSession(
                item_id=UUID(data["item_id"]),
                session_id=data["session_id"],
                created_at=0,  # not stored in encrypted payload
                expires_at=time.monotonic() + TOKEN_TTL_SECONDS,  # refresh TTL on load
                oauth_state=data.get("oauth_state"),
                access_token=data.get("access_token"),
                refresh_token=data.get("refresh_token"),
                token_type=data.get("token_type"),
                auth_method=data.get("auth_method"),
            )
            return session
        except Exception as exc:
            logger.warning(
                "Failed to decrypt session %s: %s; evicting", session_id, exc
            )
            self._evict(session_id)
            return None

    def _evict(self, session_id: str) -> None:
        self._entries.pop(session_id, None)


# ---------------------------------------------------------------------------
# Global singleton (per-process)
# ---------------------------------------------------------------------------

_session_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    global _session_store
    if _session_store is None:
        with _store_lock:
            if _session_store is None:
                _session_store = SessionStore()
    return _session_store
