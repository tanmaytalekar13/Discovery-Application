"""Tests for the session store (in-memory encrypted token storage)."""
from __future__ import annotations

import time
from uuid import uuid4

import pytest

from app.sandbox.session import (
    SessionStore,
    TestSession,
    get_session_store,
)


@pytest.fixture
def store():
    """Each test gets a fresh SessionStore (don't use the singleton)."""
    return SessionStore()


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------

class TestCreateSession:
    def test_returns_session(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        assert isinstance(session, TestSession)
        assert session.item_id == item_id
        assert len(session.session_id) > 16  # securely random

    def test_session_ids_are_unique(self, store):
        item_id = uuid4()
        s1 = store.create_session(item_id)
        s2 = store.create_session(item_id)
        assert s1.session_id != s2.session_id

    def test_session_has_ttl(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        assert session.expires_at > session.created_at


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

class TestGetSession:
    def test_get_existing(self, store):
        item_id = uuid4()
        created = store.create_session(item_id)
        fetched = store.get_session(created.session_id)
        assert fetched is not None
        assert fetched.session_id == created.session_id
        assert fetched.item_id == item_id

    def test_get_missing_returns_none(self, store):
        assert store.get_session("nonexistent") is None

    def test_token_initially_empty(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        assert session.access_token is None
        assert session.refresh_token is None
        assert session.auth_method is None


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

class TestStoreToken:
    def test_store_oauth_token(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        result = store.store_token(
            session.session_id,
            access_token="oauth-access-secret",
            refresh_token="oauth-refresh-secret",
            auth_method="oauth2",
        )
        assert result is not None
        assert result.access_token == "oauth-access-secret"
        assert result.refresh_token == "oauth-refresh-secret"
        assert result.auth_method == "oauth2"

    def test_store_bearer_token(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        result = store.store_token(
            session.session_id,
            access_token="manual-bearer-token",
            auth_method="bearer",
        )
        assert result is not None
        assert result.auth_method == "bearer"
        assert result.refresh_token is None

    def test_store_token_for_missing_session(self, store):
        result = store.store_token(
            "nonexistent",
            access_token="some-token",
        )
        assert result is None

    def test_persists_after_storage(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        store.store_token(
            session.session_id,
            access_token="persist-test",
            auth_method="bearer",
        )
        fetched = store.get_session(session.session_id)
        assert fetched is not None
        assert fetched.access_token == "persist-test"


# ---------------------------------------------------------------------------
# OAuth state
# ---------------------------------------------------------------------------

class TestOAuthState:
    def test_set_and_get_state(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        ok = store.set_oauth_state(session.session_id, "csrf-state-xyz")
        assert ok is True
        assert store.get_oauth_state(session.session_id) == "csrf-state-xyz"

    def test_get_state_for_missing_session(self, store):
        assert store.get_oauth_state("nonexistent") is None

    def test_set_state_for_missing_session(self, store):
        assert store.set_oauth_state("nonexistent", "state") is False


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------

class TestRevocation:
    def test_revoke_existing(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        assert store.revoke_session(session.session_id) is True
        assert store.get_session(session.session_id) is None

    def test_revoke_missing(self, store):
        assert store.revoke_session("nonexistent") is False

    def test_revocation_is_immediate(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        store.store_token(
            session.session_id,
            access_token="revoke-me",
        )
        store.revoke_session(session.session_id)
        # Subsequent get should return None
        assert store.get_session(session.session_id) is None


# ---------------------------------------------------------------------------
# Encryption at rest
# ---------------------------------------------------------------------------

class TestEncryptionAtRest:
    def test_token_not_stored_in_plaintext(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        store.store_token(
            session.session_id,
            access_token="super-secret-token",
            auth_method="bearer",
        )
        # Inspect the raw storage - token must not appear in plaintext
        with store._lock:
            raw = store._entries.get(session.session_id)
        assert raw is not None
        assert "super-secret-token" not in raw

    def test_token_not_stored_as_uuid(self, store):
        item_id = uuid4()
        session = store.create_session(item_id)
        with store._lock:
            raw = store._entries.get(session.session_id)
        assert str(item_id) not in raw


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_empty_store(self, store):
        assert store.stats() == {"total_sessions": 0}

    def test_counts_sessions(self, store):
        store.create_session(uuid4())
        store.create_session(uuid4())
        store.create_session(uuid4())
        assert store.stats()["total_sessions"] == 3

    def test_revoke_decrements_count(self, store):
        session = store.create_session(uuid4())
        assert store.stats()["total_sessions"] == 1
        store.revoke_session(session.session_id)
        assert store.stats()["total_sessions"] == 0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_singleton_returns_same_instance(self):
        s1 = get_session_store()
        s2 = get_session_store()
        assert s1 is s2
