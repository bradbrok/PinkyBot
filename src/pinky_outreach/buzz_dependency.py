"""Lightweight Buzz dependency probe safe to import in a broken venv."""

from __future__ import annotations

import importlib

BUZZ_REQUIRED_IMPORTS = ("coincurve", "httpx", "nacl", "websockets")


def missing_buzz_dependencies() -> tuple[str, ...]:
    """Return dependencies that cannot actually be imported at boot.

    ``find_spec`` alone is insufficient for native wheels: a package can be
    present while its extension or shared library still fails to load.  Probe
    the required modules themselves, while keeping the Buzz adapter unimported
    so a broken dependency becomes a loud refusal instead of a daemon crash.
    """
    missing: list[str] = []
    for name in BUZZ_REQUIRED_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 — broken native wheels count as missing
            missing.append(name)
    return tuple(missing)


__all__ = ["BUZZ_REQUIRED_IMPORTS", "missing_buzz_dependencies"]
