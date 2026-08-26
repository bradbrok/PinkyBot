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

import atexit
import os
import shutil
import tempfile

import pytest
from fastapi.testclient import TestClient

# Test session secret. Long-enough random-looking value; never used in
# production. Tests that need to override (e.g. test_auth.py) do so via
# ``monkeypatch.setenv`` for the duration of their test.
TEST_SESSION_SECRET = "test-session-secret-do-not-use-in-prod-32bytes-min"
TEST_HOME = tempfile.mkdtemp(prefix="pinkybot-test-home-")
TEST_CLAUDE_CONFIG_DIR = tempfile.mkdtemp(prefix="pinkybot-test-claude-config-")
TEST_CLAUDE_SECURESTORAGE_DIR = tempfile.mkdtemp(
    prefix="pinkybot-test-claude-securestorage-"
)
TEST_ANTHROPIC_CONFIG_DIR = tempfile.mkdtemp(
    prefix="pinkybot-test-anthropic-config-"
)
TEST_CODEX_HOME = tempfile.mkdtemp(prefix="pinkybot-test-codex-home-")
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)
atexit.register(shutil.rmtree, TEST_CLAUDE_CONFIG_DIR, ignore_errors=True)
atexit.register(shutil.rmtree, TEST_CLAUDE_SECURESTORAGE_DIR, ignore_errors=True)
atexit.register(shutil.rmtree, TEST_ANTHROPIC_CONFIG_DIR, ignore_errors=True)
atexit.register(shutil.rmtree, TEST_CODEX_HOME, ignore_errors=True)

# Repository tests must not inherit live daemon behavior from their invoking
# shell.  The source tree contains many PINKY_* switches that can select real
# transports, containers, shared services, auth policy, or process launchers.
# Scrub the whole namespace so newly-added switches are isolated by default,
# then pin only the deterministic values the suite relies on.
# Credential vars are pinned to EMPTY (tokens) or FRESH EMPTY DIRS (config/
# storage roots). Empty-string shadowing, not deletion: a bare unset lets a
# parent environment re-supply the value, and pointing a *_CONFIG_DIR at a
# populated inherited path re-authenticates the bundled CLI. Enumerating every
# credential var is a losing game (the CLI reads dozens and gains more per
# release), so this list is defense-in-depth beneath two structural backstops:
# the transport guard below (no test may spawn the real CLI) and the
# credential tripwire in test_test_env_guard.py (asserts loggedIn=false, so any
# credential link this list misses fails CI loudly rather than leaking).
_PINNED_TEST_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_AUTH_TOKEN": "",
    "ANTHROPIC_CONFIG_DIR": TEST_ANTHROPIC_CONFIG_DIR,
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "CLAUDE_CONFIG_DIR": TEST_CLAUDE_CONFIG_DIR,
    "CLAUDE_SECURESTORAGE_CONFIG_DIR": TEST_CLAUDE_SECURESTORAGE_DIR,
    "CODEX_HOME": TEST_CODEX_HOME,
    "HOME": TEST_HOME,
    "PINKY_AUTH_DENY_DEFAULT": "shadow",
    "PINKY_DREAM_TRANSPORT": "sdk",
    "PINKY_SESSION_SECRET": TEST_SESSION_SECRET,
    "PINKY_TEST_TRANSPORT_GUARD": "1",
}
_REAL_TRANSPORT_OPT_IN_ENV = "PINKY_TEST_REAL_TRANSPORT"
_REAL_TRANSPORT_OPTED_IN = os.environ.get(
    _REAL_TRANSPORT_OPT_IN_ENV, ""
).strip().lower() in {"1", "true", "yes", "on"}


def _scrub_test_env() -> None:
    """Replace ambient runtime configuration with deterministic test values."""
    for key in tuple(os.environ):
        if key.startswith("PINKY_"):
            os.environ.pop(key, None)
    os.environ.update(_PINNED_TEST_ENV)


# Run during conftest import, before pytest imports test modules that may import
# source modules with environment-derived module constants.
_scrub_test_env()


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
    config.addinivalue_line(
        "markers",
        "store_security: test exercises the store ownership/mode security boundary — "
        "opts out of the global _verified_daemon_owned_path stub",
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


@pytest.fixture(autouse=True)
def _stub_daemon_wal_path_check(request, monkeypatch):
    """Bypass the DaemonStoreCatalog WAL path ownership/mode check in tests.

    The check rejects any store path whose ancestor directories are writable by
    non-daemon UIDs — /tmp (mode 1777) is one such ancestor.  All test helpers
    that call create_api() use tempfile paths under /tmp, so without this stub
    the preflight fails whenever a prior test left a store file behind.

    Tests in this suite are not exercising the store-security boundary;
    returning the realpath directly preserves the path-resolution contract
    while removing the environment-dependent permission check.

    Tests marked ``store_security`` test the security boundary itself and need
    the real implementation — they opt out of this stub via the marker.
    """
    if request.node.get_closest_marker("store_security"):
        yield
        return

    from pinky_daemon.store_catalog import DaemonStoreCatalog

    monkeypatch.setattr(
        DaemonStoreCatalog,
        "_verified_daemon_owned_path",
        lambda self, path: os.path.realpath(path),
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
def _isolate_test_env(request, monkeypatch):
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
    if (
        request.node.get_closest_marker("real_transport")
        and _REAL_TRANSPORT_OPTED_IN
    ):
        # The process-level flag is captured before the import-time PINKY_*
        # scrub. Marked tests still begin credential-empty and must inject the
        # exact live transport configuration they intend to exercise.
        monkeypatch.setenv("PINKY_TEST_TRANSPORT_GUARD", "0")
    try:
        yield
    finally:
        # Also contain direct os.environ writes that bypassed monkeypatch.
        _scrub_test_env()


@pytest.fixture(autouse=True)
def _guard_real_sdk_transport(monkeypatch, _isolate_test_env):
    """Fail before any test can spawn the real bundled Claude CLI.

    Two layers, because a single high-level patch is bypassable:

    1. ``claude_agent_sdk.ClaudeSDKClient`` — the clearest early failure for
       the common ``streaming_session`` path.
    2. ``SubprocessCLITransport.connect`` — the actual subprocess spawn, the
       single choke point BOTH ``ClaudeSDKClient.connect()`` and the module-
       level ``query()`` funnel through. Patching the transport method (not
       just the package attribute) also defeats a ``ClaudeSDKClient`` alias
       captured at import/collection time before the attribute patch applies:
       the alias still constructs the real transport at runtime. Without this
       layer a test reaching ``query()`` (e.g. ``SDKRunner.run``) or holding
       such an alias could spawn the real credential-bearing CLI.
    """
    if os.environ.get("PINKY_TEST_TRANSPORT_GUARD") != "1":
        yield
        return

    import claude_agent_sdk
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    def blocked_sdk_client(*args, **kwargs):
        del args, kwargs
        pytest.fail(
            "PINKY_TEST_TRANSPORT_GUARD blocked real ClaudeSDKClient "
            "construction through an unpatched _ensure_streaming_session "
            "boundary; patch that boundary (or ClaudeSDKClient) in this test",
            pytrace=False,
        )

    async def blocked_transport_connect(self, *args, **kwargs):
        del self, args, kwargs
        pytest.fail(
            "PINKY_TEST_TRANSPORT_GUARD blocked a real Claude CLI subprocess "
            "spawn (SubprocessCLITransport.connect) — reached via query(), a "
            "pre-import ClaudeSDKClient alias, or another unpatched boundary; "
            "patch the SDK entry point this test exercises",
            pytrace=False,
        )

    monkeypatch.setattr(claude_agent_sdk, "ClaudeSDKClient", blocked_sdk_client)
    monkeypatch.setattr(
        SubprocessCLITransport, "connect", blocked_transport_connect
    )
    yield


@pytest.fixture(autouse=True)
def _auto_cookie_test_client(request, monkeypatch, _isolate_test_env):
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


@pytest.fixture
def stub_sdk_transport(monkeypatch):
    """Provide a connecting in-process fake SDK client for incidental boots.

    For tests whose subject is elsewhere (auth scoping, buzz startup) but whose
    API calls boot a streaming session as a side effect. The fake replaces
    ``ClaudeSDKClient`` entirely — so no real ``SubprocessCLITransport`` is
    constructed and the transport guard is satisfied — and connects
    successfully with an idle receive loop, reaching the same CONNECTED state
    the real credential-backed client reached at these tests' baseline. It is
    NOT a credential-less client (which fails connect); it is a deterministic
    in-process transport, so a boot side effect never spawns a subprocess and
    never diverges the session lifecycle from the reviewed baseline.
    """
    import claude_agent_sdk

    class ConnectingFakeSDKClient:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def get_server_info(self):
            return None

        async def get_context_usage(self):
            return {"totalTokens": 0, "maxTokens": 200_000}

        async def query(self, prompt):
            del prompt
            return None

        async def receive_messages(self):
            # Idle transport: connected, no turn output to deliver. The reader
            # loop completes cleanly rather than blocking or spawning anything.
            return
            yield  # pragma: no cover — makes this an async generator

    monkeypatch.setattr(
        claude_agent_sdk, "ClaudeSDKClient", ConnectingFakeSDKClient
    )
    yield
