"""Tests for the encryption envelope (crypto.py) and agent-scoped TokenStore.

Covers spec §4/§6:
  - refresh_token + client_secret are encrypted AT REST (stored bytes differ
    from plaintext) and round-trip correctly;
  - the realm id / client id (non-secret) are stored as-is;
  - crypto fails LOUD (CryptoNotConfigured) when no key is configured, rather
    than silently persisting plaintext;
  - a tampered / wrong-key envelope fails to decrypt;
  - clearing tokens does not leave plaintext secrets behind.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from pinky_qbo import crypto
from pinky_qbo.crypto import CryptoNotConfigured
from pinky_qbo.store import TokenStore


@pytest.fixture()
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("PINKY_QBO_KEY", key)
    monkeypatch.delenv("PINKY_QBO_KEY_FILE", raising=False)
    return key


@pytest.fixture()
def no_key(monkeypatch):
    monkeypatch.delenv("PINKY_QBO_KEY", raising=False)
    monkeypatch.delenv("PINKY_QBO_KEY_FILE", raising=False)


class _MemSettings:
    """In-memory stand-in for the registry's agent-scoped settings store.

    Mirrors set_agent_setting(agent, key, value) / get_agent_setting(agent, key,
    default) and records the RAW persisted bytes so tests can assert what hits
    disk.
    """

    def __init__(self):
        self.store: dict[tuple[str, str], str] = {}

    def set(self, agent, key, value):
        self.store[(agent, key)] = value

    def get(self, agent, key, default=""):
        return self.store.get((agent, key), default)


def _store(settings):
    return TokenStore(agent="onesie", set_fn=settings.set, get_fn=settings.get)


# ── crypto envelope ────────────────────────────────────────────────────────────


class TestCrypto:
    def test_round_trip(self, fernet_key):
        env = crypto.encrypt("super-secret-refresh-token")
        assert crypto.decrypt(env) == "super-secret-refresh-token"

    def test_ciphertext_is_not_plaintext(self, fernet_key):
        env = crypto.encrypt("AB12refreshXYZ")
        assert "AB12refreshXYZ" not in env
        assert env.startswith("qbo-fernet:v1:")

    def test_is_envelope(self, fernet_key):
        assert crypto.is_envelope(crypto.encrypt("x"))
        assert not crypto.is_envelope("plaintext")

    def test_fails_loud_without_key(self, no_key):
        with pytest.raises(CryptoNotConfigured):
            crypto.encrypt("secret")

    def test_decrypt_fails_loud_without_key(self, monkeypatch):
        # Encrypt with a key, then remove it: decryption must fail loud.
        key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("PINKY_QBO_KEY", key)
        env = crypto.encrypt("secret")
        monkeypatch.delenv("PINKY_QBO_KEY", raising=False)
        with pytest.raises(CryptoNotConfigured):
            crypto.decrypt(env)

    def test_tampered_token_rejected(self, fernet_key):
        env = crypto.encrypt("secret")
        tampered = env[:-2] + ("AA" if not env.endswith("AA") else "BB")
        with pytest.raises(ValueError):
            crypto.decrypt(tampered)

    def test_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setenv("PINKY_QBO_KEY", Fernet.generate_key().decode("ascii"))
        env = crypto.encrypt("secret")
        monkeypatch.setenv("PINKY_QBO_KEY", Fernet.generate_key().decode("ascii"))
        with pytest.raises(ValueError):
            crypto.decrypt(env)

    def test_non_envelope_rejected(self, fernet_key):
        with pytest.raises(ValueError):
            crypto.decrypt("not-an-envelope")

    def test_is_configured_true_with_valid_key(self, fernet_key):
        assert crypto.is_configured() is True

    def test_is_configured_false_without_key(self, no_key):
        assert crypto.is_configured() is False

    def test_is_configured_false_with_malformed_key(self, monkeypatch):
        monkeypatch.setenv("PINKY_QBO_KEY", "not-a-valid-fernet-key")
        assert crypto.is_configured() is False

    def test_key_from_file(self, monkeypatch, tmp_path):
        key = Fernet.generate_key().decode("ascii")
        kf = tmp_path / "qbo.key"
        kf.write_text(key)
        monkeypatch.delenv("PINKY_QBO_KEY", raising=False)
        monkeypatch.setenv("PINKY_QBO_KEY_FILE", str(kf))
        env = crypto.encrypt("secret")
        assert crypto.decrypt(env) == "secret"

    def test_key_material_never_in_error(self, monkeypatch):
        monkeypatch.setenv("PINKY_QBO_KEY", "short-bad-key-but-distinctive-XYZ")
        try:
            crypto.encrypt("secret")
        except CryptoNotConfigured as e:
            assert "short-bad-key-but-distinctive-XYZ" not in str(e)


# ── TokenStore (encryption at rest for secrets) ────────────────────────────────


class TestTokenStore:
    def test_refresh_token_encrypted_at_rest(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_refresh_token("ROTATING-REFRESH-001")
        # The RAW persisted value must be an envelope, not the plaintext.
        raw = settings.store[("onesie", "qbo_refresh_token")]
        assert "ROTATING-REFRESH-001" not in raw
        assert crypto.is_envelope(raw)
        # And it round-trips through the store reader.
        assert store.get_refresh_token() == "ROTATING-REFRESH-001"

    def test_client_secret_encrypted_at_rest(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_client_credentials("client-id-123", "CLIENT-SECRET-XYZ")
        raw_secret = settings.store[("onesie", "qbo_client_secret")]
        assert "CLIENT-SECRET-XYZ" not in raw_secret
        assert crypto.is_envelope(raw_secret)
        cid, csec = store.get_client_credentials()
        assert (cid, csec) == ("client-id-123", "CLIENT-SECRET-XYZ")

    def test_client_id_stored_plaintext(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_client_credentials("client-id-123", "secret")
        # client_id is non-secret — stored as-is (not wrapped).
        assert settings.store[("onesie", "qbo_client_id")] == "client-id-123"

    def test_realm_stored_plaintext(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_realm("9130350000000000", "sandbox")
        assert settings.store[("onesie", "qbo_realm_id")] == "9130350000000000"
        assert settings.store[("onesie", "qbo_env")] == "sandbox"
        assert store.get_realm_id() == "9130350000000000"
        assert store.get_env() == "sandbox"

    def test_keys_are_agent_scoped(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_refresh_token("tok")
        # Every persisted key is bound to the agent tuple — a global key cannot
        # leak in (the store always passes its agent through).
        for (agent, _key) in settings.store:
            assert agent == "onesie"

    def test_is_configured_and_connected(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        assert store.is_configured() is False
        assert store.is_connected() is False
        store.save_client_credentials("id", "sec")
        assert store.is_configured() is True
        assert store.is_connected() is False
        store.save_refresh_token("rt")
        store.save_realm("123")
        assert store.is_connected() is True

    def test_clear_tokens_removes_refresh(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_client_credentials("id", "sec")
        store.save_refresh_token("rt")
        store.save_realm("123")
        store.clear_tokens()
        assert store.get_refresh_token() is None
        # Client creds survive.
        assert store.get_client_credentials() == ("id", "sec")

    def test_clear_all_leaves_no_plaintext_secret(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_client_credentials("id", "SECRET-PLAINTEXT")
        store.save_refresh_token("REFRESH-PLAINTEXT")
        store.save_realm("123")
        store.clear_all()
        for v in settings.store.values():
            assert "SECRET-PLAINTEXT" not in v
            assert "REFRESH-PLAINTEXT" not in v
        assert store.is_configured() is False
        assert store.is_connected() is False

    def test_save_refresh_requires_crypto(self, no_key):
        settings = _MemSettings()
        store = _store(settings)
        # Without a key, saving a SECRET must fail loud (no silent plaintext).
        with pytest.raises(CryptoNotConfigured):
            store.save_refresh_token("would-be-plaintext")
        # Nothing got persisted as plaintext.
        assert ("onesie", "qbo_refresh_token") not in settings.store

    def test_rotation_overwrites_old_envelope(self, fernet_key):
        settings = _MemSettings()
        store = _store(settings)
        store.save_refresh_token("OLD-REFRESH")
        old_raw = settings.store[("onesie", "qbo_refresh_token")]
        store.save_refresh_token("NEW-REFRESH")
        new_raw = settings.store[("onesie", "qbo_refresh_token")]
        assert old_raw != new_raw
        assert store.get_refresh_token() == "NEW-REFRESH"
        assert "OLD-REFRESH" not in new_raw
