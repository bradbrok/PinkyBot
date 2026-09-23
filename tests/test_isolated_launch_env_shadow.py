"""Shadow diagnostics observe names without changing launch environments."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pinky_daemon import codex_session, codex_tmux_session, isolated_launch_env, tmux_session
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from tests.tmux_isolated_env_support import DAEMON_NAMES, SYNTHETIC_VALUES, Registry, launch_probe


def reports(logs):
    prefix = "isolated_launch_env_shadow "
    return [json.loads(line[len(prefix):]) for line in logs if line.startswith(prefix)]


def builder(kind, registry, tmp_path, monkeypatch):
    logs = []
    for mod in (tmux_session, codex_tmux_session, codex_session):
        monkeypatch.setattr(mod, "_log", logs.append)
    config = StreamingSessionConfig(
        agent_name="test-tenant", working_dir=str(tmp_path), provider_key="synthetic-provider",
    )
    if kind == "app_server":
        monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
        monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
        session = CodexSession(config, registry=registry)
        target = session._app_supervisor
        assert target is not None
        target._log = logs.append
        return target._build_env, logs
    cls = TmuxSession if kind == "claude" else CodexTmuxSession
    session = cls(config, registry=registry)
    return session._build_repl_env, logs


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("flag", [None, "", "0", "true", "yes"])
def test_shadow_is_explicitly_opt_in(tmp_path, monkeypatch, kind, flag):
    if flag is None:
        monkeypatch.delenv(isolated_launch_env.SHADOW_ENV, raising=False)
    else:
        monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, flag)
    build, logs = builder(kind, Registry(), tmp_path, monkeypatch)
    build()
    assert not reports(logs)


@pytest.mark.parametrize("kind", ["codex", "app_server"])
def test_flag_off_adds_no_registry_lookups(tmp_path, monkeypatch, kind):
    registry = Mock()
    build, logs = builder(kind, registry, tmp_path, monkeypatch)
    registry.reset_mock()
    build()
    assert registry.mock_calls == []
    assert not reports(logs)


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("status,key,expected", [
    ("isolated", "scoped", True), ("isolated", "", True),
    ("unknown", "scoped", True), ("unknown", "", False),
    ("not_isolated", "scoped", False), ("not_isolated", "", False),
])
def test_tri_state_predicate_and_name_only_reports(
    tmp_path, monkeypatch, kind, status, key, expected,
):
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "1")
    for name, value in SYNTHETIC_VALUES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ZOHO_API_SECRET", "synthetic-future-grant")
    build, logs = builder(kind, Registry(status, key=key), tmp_path, monkeypatch)
    before = dict(os.environ)
    env = build()
    assert dict(os.environ) == before
    emitted = reports(logs)
    assert bool(emitted) is expected
    if expected:
        assert len(emitted) == 1
        report = emitted[0]
        assert report["agent"] == "test-tenant" and report["source"] == "daemon"
        assert report["enforced"] is False
        dropped = report["would_drop_names"]
        assert report["would_drop_count"] == len(dropped) == len(set(dropped))
        assert dropped == sorted(dropped)
        assert DAEMON_NAMES | {"ZOHO_API_SECRET"} <= set(dropped)
        assert "OPENAI_API_KEY" not in dropped if kind != "claude" else True
    assert all(value not in "\n".join(logs) for value in SYNTHETIC_VALUES.values())
    assert "synthetic-future-grant" not in "\n".join(logs)
    if kind != "claude":
        assert DAEMON_NAMES <= env.keys(), "shadow accidentally enforces payload filtering"


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("mode", ["container", "unix_user"])
def test_nonlocal_mode_reports_despite_false_flag(tmp_path, monkeypatch, kind, mode):
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "1")
    build, logs = builder(kind, Registry("not_isolated", mode=mode), tmp_path, monkeypatch)
    build()
    assert reports(logs)[0]["isolation"] == "isolated"


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_shadow_preserves_payload_bytes(tmp_path, monkeypatch, kind):
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "0")
    for name, value in SYNTHETIC_VALUES.items():
        monkeypatch.setenv(name, value)
    build, logs = builder(kind, Registry(), tmp_path, monkeypatch)
    off = build()
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "1")
    on = build()
    # Codex forwards the operator's flag as part of existing full-env parity.
    # That input difference is the only allowed difference in output bytes.
    if kind != "claude":
        assert off.pop(isolated_launch_env.SHADOW_ENV) == "0"
        assert on.pop(isolated_launch_env.SHADOW_ENV) == "1"
    assert json.dumps(off, sort_keys=True).encode() == json.dumps(on, sort_keys=True).encode()
    assert len(reports(logs)) == 1


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
async def test_real_tmux_shadow_preserves_child_names(tmp_path, monkeypatch, kind):
    observed = []
    for flag in ("0", "1"):
        root = tmp_path / flag
        root.mkdir()
        with monkeypatch.context() as launch_patch:
            async with launch_probe(root, launch_patch) as probe:
                launch_patch.setenv(isolated_launch_env.SHADOW_ENV, flag)
                names = await probe.launch(kind)
                assert DAEMON_NAMES <= names, "shadow must leave the existing inheritance intact"
                assert "CLAUDE_CODE_OAUTH_TOKEN" in json.loads(probe.empty_names_path.read_text())
                assert bool(reports(probe.logs)) is (flag == "1")
                if kind == "claude":
                    assert "PINKY_AGENT_KEY" in names
                observed.append(names)
    assert observed[0] == observed[1]


def test_base_allowlist_and_explicit_names_do_not_hide_daemon_only(monkeypatch):
    keep = {
        "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE",
        "TZ", "TMPDIR", "XDG_CONFIG_HOME", "XDG_NEW_SETTING", "HTTP_PROXY", "HTTPS_PROXY",
        "NO_PROXY", "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "EXPLICIT_ALLOWED_NAME", "ZOHO_API_SECRET",
    }
    drop = {"HRPOS_PASSWORD", "XDGISH", "LCISH", "arbitrary\nname"} | DAEMON_NAMES - {"HRPOS_PASSWORD"}
    # This mapping fails if the reporter tries to obtain a value (except flag).
    class NamesOnly(dict):
        def __getitem__(self, name):
            raise AssertionError("shadow read an environment value")

        def get(self, name, default=None):
            assert name == isolated_launch_env.SHADOW_ENV
            return "1"

        def items(self):
            raise AssertionError("shadow iterated environment values")

    monkeypatch.setattr(isolated_launch_env, "os", SimpleNamespace(environ=NamesOnly.fromkeys(keep | drop)))
    logs = []
    isolated_launch_env.report_shadow(
        agent_name="tenant\nidentity", status="isolated", has_agent_key=True,
        explicit_names={"EXPLICIT_ALLOWED_NAME", "ZOHO_API_SECRET"} | DAEMON_NAMES - {"HRPOS_PASSWORD"},
        log=logs.append,
    )
    report = reports(logs)[0]
    assert set(report["would_drop_names"]) == drop
    assert "\n" not in logs[0]
    assert isolated_launch_env.DAEMON_ONLY == {"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"}


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
def test_status_is_rechecked_for_each_build(tmp_path, monkeypatch, kind):
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "1")
    registry = Registry("not_isolated")
    build, logs = builder(kind, registry, tmp_path, monkeypatch)
    build()
    assert not reports(logs)
    registry.status = "isolated"
    build()
    assert len(reports(logs)) == 1


def test_missing_registry_and_lookup_failure_are_unknown():
    assert isolated_launch_env.isolation_status(None, "tenant") == "unknown"
    assert isolated_launch_env.isolation_status(Registry(), "") == "unknown"
    assert isolated_launch_env.isolation_status(Registry("unknown"), "tenant") == "unknown"
    assert isolated_launch_env.isolation_status(SimpleNamespace(get=lambda _: None), "tenant") == "unknown"


@pytest.mark.parametrize("kind", ["codex", "app_server"])
def test_key_lookup_failure_still_reports_proven_isolated(tmp_path, monkeypatch, kind):
    monkeypatch.setenv(isolated_launch_env.SHADOW_ENV, "1")
    registry = Registry()
    monkeypatch.setattr(registry, "get_signing_key", Mock(side_effect=RuntimeError("synthetic")))
    build, logs = builder(kind, registry, tmp_path, monkeypatch)
    build()
    assert reports(logs)[0]["isolation"] == "isolated"
