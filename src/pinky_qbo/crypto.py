"""Fernet encryption envelope for QBO secrets.

Key is loaded from a FILE whose path is given by the env var PINKY_QBO_KEY_FILE.
Key material is NEVER accepted from the environment directly — only the file path.

Usage:
    from pinky_qbo.crypto import encrypt, decrypt
    token = encrypt("my-secret")
    plain = decrypt(token)
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet


@lru_cache(maxsize=1)
def _load_key() -> bytes:
    """Load the Fernet key from the file referenced by PINKY_QBO_KEY_FILE.

    Raises RuntimeError if the env var or file is missing/unreadable.
    The result is cached for the lifetime of the process.
    """
    key_file = os.environ.get("PINKY_QBO_KEY_FILE", "")
    if not key_file:
        raise RuntimeError(
            "PINKY_QBO_KEY_FILE env var is not set. "
            "Set it to the path of a root-only 0600 file containing a Fernet key."
        )
    try:
        with open(key_file, "rb") as fh:
            raw = fh.read().strip()
    except OSError as exc:
        raise RuntimeError(
            f"Cannot read Fernet key from '{key_file}': {exc}"
        ) from exc
    if not raw:
        raise RuntimeError(f"Fernet key file '{key_file}' is empty.")
    return raw


def _fernet() -> Fernet:
    return Fernet(_load_key())


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a URL-safe base64 Fernet token (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token. Returns the original plaintext string."""
    return _fernet().decrypt(token.encode()).decode()
