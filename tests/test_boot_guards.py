"""Tests for refuse-to-boot guards (#441).

Covers per-guard logic, the override env-var mechanism, the warning guard
runner, the overall ``assert_web_mode_safe`` aggregator, and the message
formatter.

Guards that depend on unshipped pieces of the #433 track (admin-user
existence, legacy UI password, multi-worker rate-limit backend) land
alongside their owning PRs (#435, #438) and are not exercised here.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from pinky_daemon.boot_guards import (
    BootGuardError,
    assert_web_mode_safe,
    check_db_permissions,
    check_public_bind_requires_tls_marker,
    check_session_secret,
    check_trusted_proxies_when_behind_tls,
    format_guard_error,
    run_warning_guards,
)
from pinky_daemon.config import DeploymentMode

# ── check_session_secret ───────────────────────────────────────


_GOOD_SECRET = "x7ZfqL9aE2bN4mPvR8sUw1tYjK5dGcHo3iVnQbAxZsUyTpWmRkLeJfH4dN6cVbXz"


class TestCheckSessionSecret:
    def test_unset_fails(self):
        r = check_session_secret(env={})
        assert not r.ok
        assert "unset" in r.message.lower()

    def test_too_short_fails(self):
        r = check_session_secret(env={"PINKY_SESSION_SECRET": "abc123"})
        assert not r.ok
        assert "too short" in r.message.lower()

    @pytest.mark.parametrize(
        "value", ["changeme", "secret", "pinky", "password", "default"]
    )
    def test_placeholder_fails(self, value):
        # Pad to >32 chars so we exercise the placeholder check, not length.
        padded = value + "x" * 30
        # But the lowered() check reads the *raw* secret, not the padded
        # one — so test a long literal-placeholder by setting only the
        # placeholder.
        r = check_session_secret(env={"PINKY_SESSION_SECRET": value})
        assert not r.ok
        # padded variant should pass length but might still fail on
        # entropy if the padding character is repeated. Check intent:
        _ = padded

    def test_long_random_passes(self):
        r = check_session_secret(env={"PINKY_SESSION_SECRET": _GOOD_SECRET})
        assert r.ok

    def test_low_entropy_fails(self):
        # 32 chars but only 1 distinct char.
        r = check_session_secret(env={"PINKY_SESSION_SECRET": "x" * 64})
        assert not r.ok
        assert "entropy" in r.message.lower()

    def test_override_marks_passing(self):
        env = {
            "PINKY_SESSION_SECRET": "",
            "PINKY_ALLOW_INSECURE_SESSION_SECRET": "true",
        }
        r = check_session_secret(env=env)
        assert r.ok
        assert r.override_used


# ── check_public_bind_requires_tls_marker ──────────────────────


class TestPublicBindRequiresTlsMarker:
    @pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
    def test_loopback_passes(self, host):
        r = check_public_bind_requires_tls_marker(env={}, host=host)
        assert r.ok

    def test_public_bind_without_marker_fails(self):
        r = check_public_bind_requires_tls_marker(env={}, host="0.0.0.0")
        assert not r.ok
        assert "PINKY_BEHIND_TLS_PROXY" in r.message

    def test_public_bind_with_marker_passes(self):
        r = check_public_bind_requires_tls_marker(
            env={"PINKY_BEHIND_TLS_PROXY": "true"}, host="0.0.0.0"
        )
        assert r.ok

    def test_specific_public_address_fails(self):
        r = check_public_bind_requires_tls_marker(
            env={}, host="192.168.1.10"
        )
        assert not r.ok

    def test_override_marks_passing(self):
        r = check_public_bind_requires_tls_marker(
            env={"PINKY_ALLOW_INSECURE_PUBLIC_BIND_NO_TLS": "true"},
            host="0.0.0.0",
        )
        assert r.ok
        assert r.override_used


# ── check_trusted_proxies_when_behind_tls ──────────────────────


class TestTrustedProxiesWhenBehindTls:
    def test_no_tls_marker_passes(self):
        r = check_trusted_proxies_when_behind_tls(env={})
        assert r.ok

    def test_behind_tls_with_proxies_passes(self):
        r = check_trusted_proxies_when_behind_tls(
            env={
                "PINKY_BEHIND_TLS_PROXY": "true",
                "PINKY_TRUSTED_PROXIES": "127.0.0.1/32",
            }
        )
        assert r.ok

    def test_behind_tls_without_proxies_fails(self):
        r = check_trusted_proxies_when_behind_tls(
            env={"PINKY_BEHIND_TLS_PROXY": "true"}
        )
        assert not r.ok
        assert "PINKY_TRUSTED_PROXIES" in r.message
        assert "flatten" in r.message.lower() or "audit" in r.message.lower()


# ── check_db_permissions ───────────────────────────────────────


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestCheckDbPermissions:
    def test_nonexistent_path_no_parent_passes(self, tmp_dir):
        # Nonexistent inside an existing parent should still check parent.
        # Use a fully nonexistent path so we hit the early-return.
        nowhere = tmp_dir / "nope" / "nope.db"
        r = check_db_permissions(env={}, db_path=str(nowhere))
        # tmp_dir/nope doesn't exist either; guard skips.
        assert r.ok

    def test_secure_dir_passes(self, tmp_dir):
        os.chmod(tmp_dir, 0o700)
        r = check_db_permissions(env={}, db_path=str(tmp_dir / "x.db"))
        assert r.ok

    def test_world_readable_dir_fails(self, tmp_dir):
        # Skip the test on Windows (chmod is a no-op there). Skip is
        # actually inside the helper — sanity-check we return ok on win.
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX permissions only")
        os.chmod(tmp_dir, 0o755)
        r = check_db_permissions(env={}, db_path=str(tmp_dir / "x.db"))
        assert not r.ok
        assert "loose permissions" in r.message

    def test_secure_file_passes(self, tmp_dir):
        os.chmod(tmp_dir, 0o700)
        f = tmp_dir / "x.db"
        f.write_text("")
        os.chmod(f, 0o600)
        r = check_db_permissions(env={}, db_path=str(f))
        assert r.ok

    def test_world_readable_file_fails(self, tmp_dir):
        import sys

        if sys.platform == "win32":
            pytest.skip("POSIX permissions only")
        f = tmp_dir / "x.db"
        f.write_text("")
        os.chmod(f, 0o644)
        r = check_db_permissions(env={}, db_path=str(f))
        assert not r.ok


# ── Warning guards ─────────────────────────────────────────────


class TestRunWarningGuards:
    def test_returns_list(self):
        out = run_warning_guards(env={})
        assert isinstance(out, list)

    def test_running_as_root_warning_when_uid_zero(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
        out = run_warning_guards(env={})
        assert any("running as root" in m.lower() for m in out)

    def test_running_as_root_silenced_by_override(self, monkeypatch):
        monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
        out = run_warning_guards(
            env={"PINKY_ALLOW_INSECURE_RUNNING_AS_ROOT": "true"}
        )
        assert all("running as root" not in m.lower() for m in out)


# ── assert_web_mode_safe ───────────────────────────────────────


class TestAssertWebModeSafe:
    def test_trusted_mode_skips_all_guards(self, tmp_dir):
        # Even with garbage env, trusted mode is exempt.
        assert_web_mode_safe(
            DeploymentMode.TRUSTED,
            host="0.0.0.0",
            db_path=str(tmp_dir),
            env={},
        )

    def test_lan_mode_skips_all_guards(self, tmp_dir):
        assert_web_mode_safe(
            DeploymentMode.LAN,
            host="0.0.0.0",
            db_path=str(tmp_dir),
            env={},
        )

    def test_web_mode_all_guards_pass(self, tmp_dir):
        os.chmod(tmp_dir, 0o700)
        assert_web_mode_safe(
            DeploymentMode.WEB,
            host="127.0.0.1",
            db_path=str(tmp_dir / "conv.db"),
            env={
                "PINKY_SESSION_SECRET": _GOOD_SECRET,
            },
        )

    def test_web_mode_aggregates_failures(self, tmp_dir):
        # Empty env → secret unset; bound to 0.0.0.0 without TLS marker;
        # should produce two failures.
        with pytest.raises(BootGuardError) as exc:
            assert_web_mode_safe(
                DeploymentMode.WEB,
                host="0.0.0.0",
                db_path=str(tmp_dir / "conv.db"),
                env={},
            )
        names = {r.name for r in exc.value.results}
        assert "SESSION_SECRET" in names
        assert "PUBLIC_BIND_NO_TLS" in names

    def test_web_mode_overrides_let_boot_proceed(self, tmp_dir):
        os.chmod(tmp_dir, 0o700)
        env = {
            "PINKY_ALLOW_INSECURE_SESSION_SECRET": "true",
            "PINKY_ALLOW_INSECURE_PUBLIC_BIND_NO_TLS": "true",
        }
        # No exception even though the underlying conditions fail.
        assert_web_mode_safe(
            DeploymentMode.WEB,
            host="0.0.0.0",
            db_path=str(tmp_dir / "conv.db"),
            env=env,
        )


# ── format_guard_error ─────────────────────────────────────────


class TestFormatGuardError:
    def test_block_format(self):
        from pinky_daemon.boot_guards import GuardResult

        results = [
            GuardResult(name="SESSION_SECRET", ok=False, message="unset"),
            GuardResult(
                name="PUBLIC_BIND_NO_TLS",
                ok=False,
                message="bound to 0.0.0.0 without marker",
            ),
        ]
        out = format_guard_error(results)
        assert "ERROR: refusing to start" in out
        assert "[SESSION_SECRET] unset" in out
        assert "[PUBLIC_BIND_NO_TLS]" in out
        assert "PINKY_DEPLOYMENT_MODE=trusted" in out
        assert "PINKY_ALLOW_INSECURE_" in out
