"""Exceptions raised by the federation crypto layer.

Rules:
- Never include private key material in exception messages.
- Keep error types narrow so higher layers can handle them distinctly
  (e.g. TOFU mismatch vs tampered ciphertext vs unknown version).
"""

from __future__ import annotations


class CryptoError(Exception):
    """Base class for all federation crypto failures."""


class DecryptionError(CryptoError):
    """Ciphertext could not be decrypted (tamper, wrong key, or corruption)."""


class SignatureError(CryptoError):
    """Ed25519 sender signature did not verify."""


class EnvelopeError(CryptoError):
    """Envelope bytes failed structural parsing or version check."""
