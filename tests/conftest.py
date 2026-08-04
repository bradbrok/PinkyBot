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

Production-database guard (#355):

``_forbid_production_db_writes`` wraps ``sqlite3.connect`` for the whole
session and raises if a test opens a database under a checkout's
``data/`` directory. See that fixture's docstring for the why.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
from fastapi.testclient import TestClient

# Test session secret. Long-enough random-looking value; never used in
# production. Tests that need to override (e.g. test_auth.py) do so via
# ``monkeypatch.setenv`` for the duration of their test.
TEST_SESSION_SECRET = "test-session-secret-do-not-use-in-prod-32bytes-min"


def pytest_configure(config: pytest.Config) -> None:
    """Register the `real_auth` marker so it doesn't warn."""
    config.addinivalue_line(
        "markers",
        "real_auth: test exercises real auth flow — skip conftest auto-cookie patch",
    )


def _sqlite_target_path(database, uri: bool) -> Path | None:
    """Best-effort resolution of a ``sqlite3.connect`` target to a real file.

    Returns ``None`` for anything that is not an on-disk database
    (``:memory:``, empty/temporary databases, ``mode=memory`` URIs, and
    exotic argument types we don't want to guess about).
    """
    if isinstance(database, (bytes, bytearray)):
        database = os.fsdecode(database)
    if not isinstance(database, (str, os.PathLike)):
        return None

    target = os.fspath(database)
    if target in ("", ":memory:"):
        return None

    if uri or target.startswith("file:"):
        parsed = urlparse(target)
        if parsed.scheme != "file":
            return None
        if "mode=memory" in (parsed.query or ""):
            return None
        target = unquote(parsed.path)
        if target in ("", ":memory:"):
            return None

    try:
        return Path(target).resolve()
    except (OSError, ValueError):  # pragma: no cover - defensive
        return None


@pytest.fixture(autouse=True, scope="session")
def _forbid_production_db_writes():
    """Fail loudly if a test opens a SQLite file under a checkout's ``data/``.

    Roughly two dozen stores default to a *relative* ``db_path``
    (``data/tasks.db``, ``data/agents.db``, ...). A test that instantiates
    one without an explicit path therefore reads and writes whatever
    ``./data`` happens to be — which, when pytest is run from a live
    deployment checkout, is the production database.

    Rather than rewrite the defaults (invasive, production code) or chdir
    the whole session into a tmpdir (breaks tests that rely on
    repo-relative paths), this wraps ``sqlite3.connect`` for the duration
    of the test session and raises on any target under
    ``<cwd>/data`` or ``<repo root>/data``. The fix for a test that trips
    it is always the same: pass an explicit ``db_path`` under ``tmp_path``.

    Known limitation: this only covers connections opened in the pytest
    process. Code executed in a subprocess is unaffected.
    """
    repo_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    original_connect = sqlite3.connect

    def guarded_connect(database=None, *args, **kwargs):
        uri = kwargs.get("uri", False)
        target = _sqlite_target_path(database, uri)
        if target is not None:
            # cwd is recomputed per call and checked alongside the repo
            # root because the two can differ: running pytest from a live
            # deployment against another checkout's tests resolves the
            # stores' relative defaults against the *deployment's* data/.
            # A test that chdirs elsewhere first is refused too — the rule
            # is simply "pass an explicit path", with no exceptions to
            # reason about.
            for guarded in {repo_data, (Path.cwd() / "data").resolve()}:
                if target == guarded or target.is_relative_to(guarded):
                    raise RuntimeError(
                        f"Test tried to open a SQLite database inside a checkout's "
                        f"data/ directory: {target}\n"
                        f"That is the production database when pytest runs from a "
                        f"live deployment. Pass an explicit db_path under tmp_path "
                        f"instead of relying on the store's relative default."
                    )
        return original_connect(database, *args, **kwargs)

    sqlite3.connect = guarded_connect
    try:
        yield
    finally:
        sqlite3.connect = original_connect


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
def _auto_cookie_test_client(request, monkeypatch):
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
