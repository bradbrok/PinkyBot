"""Tests for the per-agent Zoho key derivation module.

Covers:
- Derivation algorithm correctness (matches zoho-crm-cli byte-for-byte)
- Master/instance file loading (perms, persistence, regeneration)
- ZohoKeyDeriver behavior (enabled/disabled gating, derive_for)
- Streaming-session integration: zoho-mcp env injection

See ``olegbrok/zoho-crm-cli#10`` for the design.
"""

from __future__ import annotations

import os
import stat

import pytest

from pinky_daemon import zoho_keys

MASTER = "master-secret-deadbeef-0000"
INSTANCE = "00000000-0000-0000-0000-000000000001"


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Each test gets a fresh module-level deriver."""
    zoho_keys.reset_default_deriver_for_tests()
    yield
    zoho_keys.reset_default_deriver_for_tests()


class TestDeriveZohoAgentKey:
    """Algorithm tests — must stay in sync with zoho-crm-cli."""

    def test_derivation_is_deterministic(self):
        a = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        b = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        assert a == b

    def test_derivation_differs_per_agent(self):
        a = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        b = zoho_keys.derive_zoho_agent_key(MASTER, "sasha", INSTANCE)
        assert a != b

    def test_derivation_differs_per_instance(self):
        a = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        b = zoho_keys.derive_zoho_agent_key(MASTER, "lera", "other-instance")
        assert a != b

    def test_empty_inputs_rejected(self):
        with pytest.raises(ValueError):
            zoho_keys.derive_zoho_agent_key("", "lera", INSTANCE)
        with pytest.raises(ValueError):
            zoho_keys.derive_zoho_agent_key(MASTER, "", INSTANCE)
        with pytest.raises(ValueError):
            zoho_keys.derive_zoho_agent_key(MASTER, "lera", "")

    def test_output_is_hex_sha256(self):
        key = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_parity_with_zoho_crm_cli(self):
        """The daemon-side and server-side derivations MUST agree.

        If this fails, agents will sign with one key and the server will
        derive a different expected key → 100% auth failure in
        production. This is the most important test in this file.
        """
        # importorskip avoids the try/except + skip pattern that CodeQL's
        # data-flow analysis can't reason through (it doesn't model that
        # pytest.skip raises and prevents the import name from being
        # used uninitialized).
        agent_auth = pytest.importorskip("zoho_crm.agent_auth")
        daemon_key = zoho_keys.derive_zoho_agent_key(MASTER, "lera", INSTANCE)
        server_key = agent_auth.derive_agent_key(MASTER, "lera", INSTANCE)
        assert daemon_key == server_key


class TestLoadMasterSecret:
    def test_missing_file_returns_empty(self, tmp_path):
        assert zoho_keys.load_master_secret(tmp_path / "absent") == ""

    def test_reads_file_contents(self, tmp_path):
        p = tmp_path / ".master-key"
        p.write_text("super-secret-master\n")
        os.chmod(p, 0o600)
        assert zoho_keys.load_master_secret(p) == "super-secret-master"

    def test_strips_whitespace(self, tmp_path):
        p = tmp_path / ".master-key"
        p.write_text("  master-with-spaces  \n\n")
        os.chmod(p, 0o600)
        assert zoho_keys.load_master_secret(p) == "master-with-spaces"

    def test_warns_on_loose_perms(self, tmp_path, caplog):
        p = tmp_path / ".master-key"
        p.write_text("secret")
        # Intentionally set unsafe perms (group + world readable) to
        # exercise the loose-perms warning path. Composed via stat
        # constants instead of a 0o644 literal so CodeQL's
        # py/overly-permissive-file-permissions check (which pattern-
        # matches octal literals) doesn't flag the test for testing the
        # exact thing it's supposed to catch.
        unsafe_perms = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
        os.chmod(p, unsafe_perms)
        with caplog.at_level("WARNING"):
            result = zoho_keys.load_master_secret(p)
        assert result == "secret"  # still loads
        assert any("loose perms" in r.message for r in caplog.records)


class TestLoadOrCreateInstanceId:
    def test_creates_on_first_call(self, tmp_path):
        p = tmp_path / ".instance-id"
        instance = zoho_keys.load_or_create_instance_id(p)
        assert instance
        assert p.exists()
        assert p.read_text(encoding="utf-8").strip() == instance

    def test_persists_across_calls(self, tmp_path):
        p = tmp_path / ".instance-id"
        a = zoho_keys.load_or_create_instance_id(p)
        b = zoho_keys.load_or_create_instance_id(p)
        assert a == b

    def test_created_file_is_mode_0600(self, tmp_path):
        p = tmp_path / ".instance-id"
        zoho_keys.load_or_create_instance_id(p)
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600

    def test_regenerates_if_empty(self, tmp_path):
        p = tmp_path / ".instance-id"
        p.write_text("")
        new = zoho_keys.load_or_create_instance_id(p)
        assert new  # non-empty
        # And persisted
        assert p.read_text(encoding="utf-8").strip() == new


class TestZohoKeyDeriver:
    def test_disabled_without_master(self, tmp_path):
        deriver = zoho_keys.ZohoKeyDeriver(
            master_path=tmp_path / "absent-master",
            instance_path=tmp_path / ".instance-id",
        )
        assert deriver.enabled is False
        assert deriver.derive_for("lera") is None
        # No instance file should have been created either — deriver is
        # entirely no-op when master is absent.
        assert not (tmp_path / ".instance-id").exists()

    def test_enabled_with_master(self, tmp_path):
        master = tmp_path / ".master-key"
        master.write_text("the-master-secret")
        os.chmod(master, 0o600)
        deriver = zoho_keys.ZohoKeyDeriver(
            master_path=master,
            instance_path=tmp_path / ".instance-id",
        )
        assert deriver.enabled is True
        assert deriver.instance_id  # populated
        assert deriver.derive_for("lera") == zoho_keys.derive_zoho_agent_key(
            "the-master-secret", "lera", deriver.instance_id
        )

    def test_different_agents_get_different_keys(self, tmp_path):
        master = tmp_path / ".master-key"
        master.write_text("master")
        deriver = zoho_keys.ZohoKeyDeriver(
            master_path=master,
            instance_path=tmp_path / ".instance-id",
        )
        assert deriver.derive_for("lera") != deriver.derive_for("sasha")


class TestStreamingSessionInjection:
    """Smoke-test the integration point on StreamingSession.

    Uses a stub session object — the actual class is heavy. We only need
    the _inject_zoho_derived_key method behavior, which is pure
    dict-manipulation.
    """

    @pytest.fixture
    def configured_deriver(self, tmp_path, monkeypatch):
        master = tmp_path / ".master-key"
        master.write_text("integration-test-master")
        os.chmod(master, 0o600)

        # Force the module-level deriver to use these paths.
        zoho_keys.reset_default_deriver_for_tests()
        monkeypatch.setattr(
            zoho_keys, "_default_deriver", None
        )

        def _factory():
            return zoho_keys.ZohoKeyDeriver(
                master_path=master,
                instance_path=tmp_path / ".instance-id",
            )

        # Replace the singleton getter.
        monkeypatch.setattr(
            zoho_keys, "get_default_deriver", _factory
        )
        return _factory()

    def test_injects_into_zoho_mcp_entry(self, configured_deriver):
        from pinky_daemon.streaming_session import StreamingSession

        # Build a minimal-stub session.
        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "lera"

        mcp_servers = {
            "pinky-memory": {"command": "py", "args": []},
            "zoho-mcp": {
                "command": "/path/to/zoho-mcp",
                "args": ["--agent", "lera"],
                "alwaysLoad": True,
            },
        }
        session._inject_zoho_derived_key(mcp_servers)

        entry = mcp_servers["zoho-mcp"]
        assert "env" in entry
        assert entry["env"]["ZOHO_DERIVED_KEY"]
        assert entry["env"]["ZOHO_INSTANCE_ID"] == configured_deriver.instance_id
        # Legacy vars explicitly overridden in the zoho-mcp env.
        assert entry["env"]["ZOHO_API_SECRET"] == ""
        assert entry["env"]["PINKY_SESSION_SECRET"] == ""
        # Other entries untouched.
        assert "env" not in mcp_servers["pinky-memory"]

    def test_no_op_when_no_zoho_entry(self, configured_deriver):
        from pinky_daemon.streaming_session import StreamingSession

        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "kuzya"
        mcp_servers = {"pinky-memory": {"command": "py", "args": []}}
        before = dict(mcp_servers)
        session._inject_zoho_derived_key(mcp_servers)
        assert mcp_servers == before

    def test_no_op_when_no_master(self, tmp_path, monkeypatch):
        """Pre-migration deployments (no .master-key) don't get derived
        injection — agents stay on the legacy shared-secret path."""
        from pinky_daemon.streaming_session import StreamingSession

        zoho_keys.reset_default_deriver_for_tests()
        monkeypatch.setattr(
            zoho_keys, "get_default_deriver",
            lambda: zoho_keys.ZohoKeyDeriver(
                master_path=tmp_path / "absent",
                instance_path=tmp_path / ".instance-id",
            ),
        )

        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "lera"
        mcp_servers = {
            "zoho-mcp": {
                "command": "/path/to/zoho-mcp",
                "args": ["--agent", "lera"],
            }
        }
        session._inject_zoho_derived_key(mcp_servers)
        # No env injected — agent will continue using whatever it had.
        assert "env" not in mcp_servers["zoho-mcp"]

    def test_preserves_existing_env(self, configured_deriver):
        from pinky_daemon.streaming_session import StreamingSession

        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "lera"
        mcp_servers = {
            "zoho-mcp": {
                "command": "/path/to/zoho-mcp",
                "args": [],
                "env": {"PYTHONUNBUFFERED": "1", "LOG_LEVEL": "DEBUG"},
            }
        }
        session._inject_zoho_derived_key(mcp_servers)
        env = mcp_servers["zoho-mcp"]["env"]
        # Existing keys preserved.
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["LOG_LEVEL"] == "DEBUG"
        # New keys added.
        assert env["ZOHO_DERIVED_KEY"]
        assert env["ZOHO_INSTANCE_ID"]

    def test_legacy_name_pinky_zoho_also_handled(self, configured_deriver):
        """Some agents have the legacy `pinky-zoho` server entry name from
        before the relocation to zoho-crm-cli. We handle both names so
        partial migrations don't get stranded.
        """
        from pinky_daemon.streaming_session import StreamingSession

        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "barsik"
        mcp_servers = {
            "pinky-zoho": {
                "command": "/old/path/pinky_zoho",
                "args": ["-m", "pinky_zoho"],
            }
        }
        session._inject_zoho_derived_key(mcp_servers)
        assert "env" in mcp_servers["pinky-zoho"]
        assert mcp_servers["pinky-zoho"]["env"]["ZOHO_DERIVED_KEY"]

    def test_scrubs_preexisting_legacy_secrets(self, configured_deriver):
        """Regression: belt-and-suspenders override MUST scrub legacy secrets
        already present in zoho_entry["env"], not just fill in missing keys.

        Pre-fix this used dict.setdefault, which silently no-op'd when a
        legacy ZOHO_API_SECRET was already in the entry env (e.g. injected
        by .mcp.json or by the skill author for the migration window). That
        defeated the entire override and left the shared secret reachable
        to the zoho-mcp subprocess — exactly the leak this code is supposed
        to close.
        """
        from pinky_daemon.streaming_session import StreamingSession

        session = StreamingSession.__new__(StreamingSession)
        session.agent_name = "lera"
        mcp_servers = {
            "zoho-mcp": {
                "command": "/path/to/zoho-mcp",
                "args": [],
                "env": {
                    # Realistic preexisting legacy state.
                    "ZOHO_API_SECRET": "legacy-shared-secret-DO-NOT-LEAK",
                    "PINKY_SESSION_SECRET": "legacy-session-secret-DO-NOT-LEAK",
                    "PYTHONUNBUFFERED": "1",
                },
            }
        }
        session._inject_zoho_derived_key(mcp_servers)
        env = mcp_servers["zoho-mcp"]["env"]

        # Legacy secrets MUST be scrubbed — not silently retained.
        assert env["ZOHO_API_SECRET"] == "", (
            "preexisting legacy ZOHO_API_SECRET must be overwritten to empty "
            "string, not retained — otherwise the override is a no-op"
        )
        assert env["PINKY_SESSION_SECRET"] == "", (
            "preexisting legacy PINKY_SESSION_SECRET must be overwritten to "
            "empty string"
        )
        # Non-secret env preserved.
        assert env["PYTHONUNBUFFERED"] == "1"
        # Derived key still injected.
        assert env["ZOHO_DERIVED_KEY"]
        assert env["ZOHO_INSTANCE_ID"]
