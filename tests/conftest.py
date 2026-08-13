"""Pytest fixtures for the PinkyBot test suite.

Auth context (as of #504, addressing Murzik's #497 review):

The production daemon defaults to fail-closed when `PINKY_SESSION_SECRET`
is unset or no valid session/HMAC is present (see
``auth_middleware`` in ``pinky_daemon.api``). The vast majority of tests
in this suite, however, are not exercising authentication — they hit
protected endpoints like ``/tasks``, ``/agents``, ``/system/*`` to test
business logic and would now all 401 under default-deny.

To avoid migrating ~340 call sites to wire up auth, this conftest:
  1. Sets ``PINKY_SESSION_SECRET`` to a known test value for the whole
     test session.
  2. Auto-injects a valid signed session cookie into every
     ``TestClient`` instance on construction.

Tests that need to exercise real auth flows (``tests/test_auth.py``)
opt out via the ``real_auth`` pytest marker (set as a module-level
``pytestmark`` in that file). For those, the conftest leaves
``TestClient`` alone and the test sets up its own auth state via
``monkeypatch.setenv`` + ``/auth/setup``.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Test session secret. Long-enough random-looking value; never used in
# production. Tests that need to override (e.g. test_auth.py) do so via
# ``monkeypatch.setenv`` for the duration of their test.
TEST_SESSION_SECRET = "test-session-secret-do-not-use-in-prod-32bytes-min"

# Repository tests must not inherit live daemon behavior from their invoking
# shell.  The source tree contains many PINKY_* switches that can select real
# transports, containers, shared services, auth policy, or process launchers.
# Scrub the whole namespace so newly-added switches are isolated by default,
# then pin only the deterministic values the suite relies on.
_PINNED_TEST_ENV = {
    "PINKY_AUTH_DENY_DEFAULT": "shadow",
    "PINKY_DREAM_TRANSPORT": "sdk",
    "PINKY_SESSION_SECRET": TEST_SESSION_SECRET,
}
_REAL_TRANSPORT_OPT_IN_ENV = "PINKY_TEST_REAL_TRANSPORT"
_REAL_TRANSPORT_OPTED_IN = os.environ.get(
    _REAL_TRANSPORT_OPT_IN_ENV, ""
).strip().lower() in {"1", "true", "yes", "on"}


def _scrub_pinky_env() -> None:
    """Replace ambient PINKY_* configuration with deterministic test values."""
    for key in tuple(os.environ):
        if key.startswith("PINKY_"):
            os.environ.pop(key, None)
    os.environ.update(_PINNED_TEST_ENV)


# Run during conftest import, before pytest imports test modules that may import
# source modules with environment-derived module constants.
_scrub_pinky_env()


def pytest_configure(config: pytest.Config) -> None:
    """Register suite-specific opt-in markers."""
    config.addinivalue_line(
        "markers",
        "real_auth: test exercises real auth flow — skip conftest auto-cookie patch",
    )
    config.addinivalue_line(
        "markers",
        "real_transport: test intentionally uses a real external transport",
    )


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Require both a marker and a process-level flag for real transports.

    Even an opted-in run starts from the scrubbed environment.  A marked test
    must still set the exact transport configuration it intends to exercise.
    """
    if item.get_closest_marker("real_transport") and not _REAL_TRANSPORT_OPTED_IN:
        pytest.skip(
            f"real transport disabled; set {_REAL_TRANSPORT_OPT_IN_ENV}=1 "
            "for this explicitly marked test"
        )


@pytest.fixture(autouse=True, scope="session")
def _ensure_test_session_secret():
    """Make sure PINKY_SESSION_SECRET is set for the whole test run.

    Without this, ``create_api`` runs in unconfigured-secret state and
    every protected endpoint 401s (default-deny). Tests that want to
    exercise the unconfigured-secret behavior monkeypatch.delenv it
    locally.
    """
    prev = os.environ.get("PINKY_SESSION_SECRET")
    os.environ["PINKY_SESSION_SECRET"] = TEST_SESSION_SECRET
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("PINKY_SESSION_SECRET", None)
        else:
            os.environ["PINKY_SESSION_SECRET"] = prev


@pytest.fixture(autouse=True)
def _isolate_pinky_env(monkeypatch):
    """Re-apply the ambient guard before every test.

    Tests remain free to override a value explicitly with ``monkeypatch`` in
    their own setup or body.  Re-applying the guard also contains accidental
    environment leakage from a prior test that mutated ``os.environ`` directly.
    """
    for key in tuple(os.environ):
        if key.startswith("PINKY_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in _PINNED_TEST_ENV.items():
        monkeypatch.setenv(key, value)
    try:
        yield
    finally:
        # Also contain direct os.environ writes that bypassed monkeypatch.
        _scrub_pinky_env()


@pytest.fixture(autouse=True)
def _auto_cookie_test_client(request, monkeypatch, _isolate_pinky_env):
    """Auto-inject a valid session cookie into every TestClient.

    Skipped for tests marked ``real_auth`` — those construct their own
    TestClient and walk the actual login/setup flow.

    The cookie is signed with the conftest-level ``TEST_SESSION_SECRET``.
    Tests that override ``PINKY_SESSION_SECRET`` to a different value
    (e.g. test_onboarding.py) will have the auto-injected cookie fail
    validation, but those tests already authenticate via
    ``client.post("/auth/setup", ...)``, which is a public endpoint and
    overwrites the bad cookie with a freshly-signed one.
    """
    if request.node.get_closest_marker("real_auth"):
        yield
        return

    # Import inside the fixture so test collection doesn't depend on
    # pinky_daemon being importable yet (it always is, but defensive).
    from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie

    cookie_value = create_session_cookie(TEST_SESSION_SECRET)
    original_init = TestClient.__init__

    def patched_init(self, app, *args, **kwargs):
        original_init(self, app, *args, **kwargs)
        try:
            self.cookies.set(SESSION_COOKIE_NAME, cookie_value)
        except Exception:
            # If the underlying httpx cookie jar shape ever changes,
            # don't break the test run — auth-checking tests opt out
            # anyway, and tests that need the cookie will fail loudly.
            pass

    monkeypatch.setattr(TestClient, "__init__", patched_init)
    yield
