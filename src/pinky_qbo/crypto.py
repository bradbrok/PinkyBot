"""Encryption envelope for QBO secrets at rest (spec §4/§6).

`system_settings` is plaintext on disk, so the refresh token and client secret
are wrapped with Fernet (AES-128-CBC + HMAC-SHA256, authenticated) *before*
being written, and unwrapped on read. If we don't ship this, we don't claim
"encrypted at rest."

Key source (explicitly defined, never in the repo):
  - env ``PINKY_QBO_KEY`` — a urlsafe-base64-encoded 32-byte Fernet key, OR
  - a 0600 file whose path is in ``PINKY_QBO_KEY_FILE`` containing that key.

The env var wins if both are set. ``encrypt``/``decrypt`` raise a clear
``CryptoNotConfigured`` when neither is available so callers fail loud rather
than silently persisting plaintext.

Generate a key with::

    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from __future__ import annotations

import os
import sys

from cryptography.fernet import Fernet, InvalidToken

_ENV_KEY = "PINKY_QBO_KEY"
_ENV_KEY_FILE = "PINKY_QBO_KEY_FILE"

# Marker prefix on ciphertext so we can tell wrapped values apart from any
# legacy plaintext that may have slipped into the store. Fernet tokens are
# urlsafe-base64 and never contain ':', so this is unambiguous.
_ENVELOPE_PREFIX = "qbo-fernet:v1:"


class CryptoNotConfiguredError(RuntimeError):
    """Raised when encryption is required but no key is configured."""


# Backward-compatible alias (kept short for callers / readability).
CryptoNotConfigured = CryptoNotConfiguredError


def _log(msg: str) -> None:
    print(f"[pinky-qbo.crypto] {msg}", file=sys.stderr)


def _load_key() -> bytes | None:
    """Load the raw Fernet key bytes from env or 0600 file, or None.

    Never logs the key material.
    """
    env_key = os.environ.get(_ENV_KEY, "").strip()
    if env_key:
        return env_key.encode("ascii")

    key_file = os.environ.get(_ENV_KEY_FILE, "").strip()
    if key_file:
        try:
            with open(key_file, encoding="ascii") as fh:
                contents = fh.read().strip()
        except OSError as e:
            raise CryptoNotConfigured(
                f"PINKY_QBO_KEY_FILE={key_file!r} could not be read: {e}"
            ) from e
        if not contents:
            raise CryptoNotConfigured(f"PINKY_QBO_KEY_FILE={key_file!r} is empty")
        return contents.encode("ascii")

    return None


def is_configured() -> bool:
    """True when a usable Fernet key is available (env or file)."""
    try:
        raw = _load_key()
    except CryptoNotConfigured:
        return False
    if not raw:
        return False
    try:
        Fernet(raw)
    except (ValueError, TypeError):
        return False
    return True


def _fernet() -> Fernet:
    """Build a Fernet instance from the configured key, or raise."""
    raw = _load_key()
    if not raw:
        raise CryptoNotConfigured(
            f"QBO encryption key not configured. Set {_ENV_KEY} (a urlsafe-base64 "
            f"32-byte Fernet key) or {_ENV_KEY_FILE} (path to a 0600 file containing it)."
        )
    try:
        return Fernet(raw)
    except (ValueError, TypeError) as e:
        # Don't echo the key material in the error.
        raise CryptoNotConfigured(
            f"QBO encryption key is malformed (expected a urlsafe-base64 32-byte "
            f"Fernet key): {e}"
        ) from e


def encrypt(plaintext: str) -> str:
    """Wrap a secret for storage. Returns a prefixed envelope string.

    Raises CryptoNotConfigured if no key is available.
    """
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _ENVELOPE_PREFIX + token


def decrypt(envelope: str) -> str:
    """Unwrap a stored secret.

    Accepts values produced by ``encrypt`` (prefixed). Raises
    CryptoNotConfigured if no key is available, or ValueError on a malformed /
    tampered / wrong-key token.
    """
    if not envelope.startswith(_ENVELOPE_PREFIX):
        raise ValueError("not a pinky-qbo crypto envelope")
    token = envelope[len(_ENVELOPE_PREFIX) :].encode("ascii")
    try:
        return _fernet().decrypt(token).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("QBO secret decryption failed (bad key or tampered token)") from e


def is_envelope(value: str) -> bool:
    """True if a stored value looks like a pinky-qbo crypto envelope."""
    return value.startswith(_ENVELOPE_PREFIX)
