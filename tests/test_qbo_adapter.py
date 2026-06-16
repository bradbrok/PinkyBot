"""Tests for the QuickBooks adapter — the refresh-rotation-under-lock path.

This is the single most important correctness path (spec §4): QBO refresh tokens
rotate on every use, so two concurrent tools in one onesie turn must NOT both
spend the same single-use token (that triggers ``invalid_grant`` fleet de-auth).

Covered:
  - concurrent ``_fresh_access_token`` calls perform exactly ONE coherent refresh
    (the other threads observe the freshly-rotated state and skip the network);
  - under the lock the adapter RE-READS the latest stored refresh token (no
    stale-token reuse);
  - the rotated refresh token is persisted (encrypted) BEFORE the new in-memory
    state is published;
  - ``_is_expired`` honours the 60s skew;
  - token material is never returned to the read-only client beyond the bearer.
"""

from __future__ import annotations

import threading
import time

import pytest
from cryptography.fernet import Fernet

from pinky_qbo import oauth
from pinky_qbo.adapters.quickbooks import QuickBooksAdapter
from pinky_qbo.store import TokenStore


@pytest.fixture(autouse=True)
def fernet_key(monkeypatch):
    monkeypatch.setenv("PINKY_QBO_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("PINKY_QBO_KEY_FILE", raising=False)


class _MemSettings:
    def __init__(self):
        self.store = {}
        self._lock = threading.Lock()

    def set(self, agent, key, value):
        with self._lock:
            self.store[(agent, key)] = value

    def get(self, agent, key, default=""):
        with self._lock:
            return self.store.get((agent, key), default)


def _make_store(refresh="REFRESH-0"):
    settings = _MemSettings()
    store = TokenStore(agent="onesie", set_fn=settings.set, get_fn=settings.get)
    store.save_client_credentials("client-id", "client-secret")
    store.save_refresh_token(refresh)
    store.save_realm("9130350000000000", "sandbox")
    return store, settings


def _adapter(store):
    return QuickBooksAdapter(
        store=store, realm_id="9130350000000000", agent="onesie", env="sandbox"
    )


# ── expiry / skew ──────────────────────────────────────────────────────────────


class TestExpiry:
    def test_expired_when_no_token(self):
        store, _ = _make_store()
        a = _adapter(store)
        assert a._is_expired() is True

    def test_skew_forces_early_refresh(self):
        store, _ = _make_store()
        a = _adapter(store)
        a._tokens.access_token = "tok"
        # 30s left — inside the 60s skew → treated as expired.
        a._tokens.expires_at = time.time() + 30
        assert a._is_expired() is True
        # 120s left — outside skew → still fresh.
        a._tokens.expires_at = time.time() + 120
        assert a._is_expired() is False


# ── refresh rotation under lock ────────────────────────────────────────────────


class TestRefreshRotation:
    def test_single_refresh_rotates_and_persists(self, monkeypatch):
        store, settings = _make_store(refresh="REFRESH-0")

        calls = []

        def fake_refresh(client_id, client_secret, refresh_token):
            calls.append(refresh_token)
            return {
                "access_token": "ACCESS-1",
                "refresh_token": "REFRESH-1",  # rotated
                "expires_in": 3600,
            }

        monkeypatch.setattr(oauth, "refresh", fake_refresh)

        a = _adapter(store)
        tok = a._fresh_access_token()
        assert tok == "ACCESS-1"
        # Used the latest stored token...
        assert calls == ["REFRESH-0"]
        # ...and persisted the rotated one (encrypted) to the store.
        assert store.get_refresh_token() == "REFRESH-1"

    def test_concurrent_refresh_spends_token_once(self, monkeypatch):
        """N threads hit an expired adapter at once → exactly ONE network refresh,
        the single-use token is spent once, no stale reuse, no invalid_grant."""
        store, settings = _make_store(refresh="REFRESH-0")

        refresh_calls = []
        barrier = threading.Barrier(8)
        call_lock = threading.Lock()

        def fake_refresh(client_id, client_secret, refresh_token):
            with call_lock:
                refresh_calls.append(refresh_token)
            # Simulate network latency so contenders pile up on the lock.
            time.sleep(0.02)
            n = len(refresh_calls)
            return {
                "access_token": f"ACCESS-{n}",
                "refresh_token": f"REFRESH-{n}",
                "expires_in": 3600,
            }

        monkeypatch.setattr(oauth, "refresh", fake_refresh)

        a = _adapter(store)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait()
                results.append(a._fresh_access_token())
            except Exception as e:  # pragma: no cover - surfaced via assert
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # CRITICAL: exactly one network refresh despite 8 concurrent callers.
        assert len(refresh_calls) == 1, refresh_calls
        # It spent the ORIGINAL single-use token exactly once (no stale reuse).
        assert refresh_calls == ["REFRESH-0"]
        # The rotated token is persisted.
        assert store.get_refresh_token() == "REFRESH-1"
        # Every caller got the SAME coherent access token.
        assert set(results) == {"ACCESS-1"}

    def test_lock_reraad_picks_up_peer_rotation(self, monkeypatch):
        """If a peer rotated while we waited on the lock, we must re-read the
        LATEST token and NOT refresh again with a stale token."""
        store, settings = _make_store(refresh="REFRESH-0")

        seen_tokens = []

        def fake_refresh(client_id, client_secret, refresh_token):
            seen_tokens.append(refresh_token)
            idx = len(seen_tokens)
            return {
                "access_token": f"ACCESS-{idx}",
                "refresh_token": f"REFRESH-{idx}",
                "expires_in": 3600,
            }

        monkeypatch.setattr(oauth, "refresh", fake_refresh)

        a = _adapter(store)
        # First refresh rotates REFRESH-0 -> REFRESH-1, sets a fresh access token.
        a._fresh_access_token()
        assert seen_tokens == ["REFRESH-0"]

        # Force expiry again; the NEXT refresh must use REFRESH-1 (re-read), not
        # the spent REFRESH-0.
        a._tokens.expires_at = time.time() - 1
        a._fresh_access_token()
        assert seen_tokens == ["REFRESH-0", "REFRESH-1"]
        assert store.get_refresh_token() == "REFRESH-2"

    def test_double_check_skips_network_when_peer_already_refreshed(self, monkeypatch):
        """The double-checked lock: if the token is already fresh when we acquire
        the lock, we must not make a redundant network call."""
        store, _ = _make_store()
        a = _adapter(store)
        # Pretend a peer already populated a fresh token.
        a._tokens.access_token = "ALREADY-FRESH"
        a._tokens.expires_at = time.time() + 3600

        def boom(*args, **kwargs):
            raise AssertionError("network refresh must not be called when fresh")

        monkeypatch.setattr(oauth, "refresh", boom)
        a._maybe_refresh()  # double-check returns early, no network
        assert a._tokens.access_token == "ALREADY-FRESH"

    def test_persist_before_publish(self, monkeypatch):
        """The rotated token must be PERSISTED before the new in-memory state is
        published — so a crash mid-refresh never reuses the spent token."""
        store, _ = _make_store(refresh="REFRESH-0")

        order = []
        real_save = store.save_refresh_token

        def tracking_save(tok):
            order.append(("persist", tok))
            real_save(tok)

        store.save_refresh_token = tracking_save  # type: ignore[assignment]

        def fake_refresh(cid, csec, rt):
            return {
                "access_token": "ACCESS-1",
                "refresh_token": "REFRESH-1",
                "expires_in": 3600,
            }

        monkeypatch.setattr(oauth, "refresh", fake_refresh)

        a = _adapter(store)
        a._maybe_refresh()
        # Persist happened, and the new in-memory bundle reflects it afterwards.
        assert order == [("persist", "REFRESH-1")]
        assert a._tokens.refresh_token == "REFRESH-1"
        assert a._tokens.access_token == "ACCESS-1"

    def test_missing_refresh_token_raises(self, monkeypatch):
        settings = _MemSettings()
        store = TokenStore(agent="onesie", set_fn=settings.set, get_fn=settings.get)
        store.save_client_credentials("id", "sec")
        store.save_realm("9130350000000000")
        a = _adapter(store)
        monkeypatch.setattr(oauth, "refresh", lambda *a, **k: {})
        with pytest.raises(RuntimeError):
            a._fresh_access_token()

    def test_refresh_with_no_access_token_raises(self, monkeypatch):
        store, _ = _make_store()
        monkeypatch.setattr(
            oauth, "refresh", lambda *a, **k: {"refresh_token": "R1", "expires_in": 3600}
        )
        a = _adapter(store)
        with pytest.raises(RuntimeError):
            a._fresh_access_token()

    def test_no_rotation_persists_only_when_changed(self, monkeypatch):
        """If the provider returns the SAME refresh token (no rotation), the store
        write is skipped (the code guards new != current)."""
        store, _ = _make_store(refresh="REFRESH-SAME")
        writes = []
        real_save = store.save_refresh_token

        def tracking_save(tok):
            writes.append(tok)
            real_save(tok)

        store.save_refresh_token = tracking_save  # type: ignore[assignment]
        monkeypatch.setattr(
            oauth,
            "refresh",
            lambda *a, **k: {
                "access_token": "ACCESS-1",
                "refresh_token": "REFRESH-SAME",
                "expires_in": 3600,
            },
        )
        a = _adapter(store)
        a._maybe_refresh()
        assert writes == []  # unchanged token → no redundant write
        assert a._tokens.access_token == "ACCESS-1"


# ── cross-process refresh lock (review P1) ──────────────────────────────────────


class TestCrossProcessLock:
    """The in-process threading.Lock can't stop a SECOND stdio process for the
    same agent (e.g. a cold-start overlap) from double-spending the single-use
    refresh token. The refresh section also takes a cross-process advisory flock.
    """

    def test_refresh_acquires_then_releases_exclusive_flock(self, monkeypatch, tmp_path):
        import pinky_qbo.adapters.quickbooks as qbo_mod

        events = []

        class FakeFcntl:
            LOCK_EX = 2
            LOCK_UN = 8

            @staticmethod
            def flock(fd, op):
                events.append(op)

        monkeypatch.setenv("PINKY_AGENTS_DB", str(tmp_path / "agents.db"))
        monkeypatch.setattr(qbo_mod, "fcntl", FakeFcntl)
        store, _ = _make_store(refresh="REFRESH-0")
        monkeypatch.setattr(
            oauth,
            "refresh",
            lambda *a, **k: {
                "access_token": "ACCESS-1",
                "refresh_token": "REFRESH-1",
                "expires_in": 3600,
            },
        )
        a = _adapter(store)
        a._maybe_refresh()
        # Exclusive lock taken first, released last — bracketing the section.
        assert events[0] == FakeFcntl.LOCK_EX
        assert events[-1] == FakeFcntl.LOCK_UN
        assert store.get_refresh_token() == "REFRESH-1"

    def test_lock_path_is_per_agent_and_co_located_with_db(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PINKY_AGENTS_DB", str(tmp_path / "agents.db"))
        store, _ = _make_store()
        a = _adapter(store)
        assert a._refresh_lock_path() == str(tmp_path / ".qbo_refresh_onesie.lock")

    def test_degrades_to_in_process_only_without_fcntl(self, monkeypatch):
        """Non-POSIX fallback: refresh still works when fcntl is unavailable."""
        import pinky_qbo.adapters.quickbooks as qbo_mod

        monkeypatch.setattr(qbo_mod, "fcntl", None)
        store, _ = _make_store(refresh="REFRESH-0")
        monkeypatch.setattr(
            oauth,
            "refresh",
            lambda *a, **k: {
                "access_token": "ACCESS-1",
                "refresh_token": "REFRESH-1",
                "expires_in": 3600,
            },
        )
        a = _adapter(store)
        assert a._fresh_access_token() == "ACCESS-1"
        assert store.get_refresh_token() == "REFRESH-1"
