"""Tests for the pinky CLI (serve, connect, run delegation)."""

from __future__ import annotations

import json
import sys

import pytest

from pinky_cli.connect import run_connect
from pinky_cli.serve import run_serve

# ---------------------------------------------------------------------------
# pinky serve
# ---------------------------------------------------------------------------


def test_serve_memory_resets_argv(monkeypatch):
    """The delegated server main must not see pinky's own CLI args."""
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr(sys, "argv", ["pinky", "serve", "--server", "memory"])
    monkeypatch.setattr("pinky_memory.__main__.main", fake_main)

    run_serve(server="memory")

    assert seen["argv"] == ["pinky-memory"]


def test_serve_outreach_resets_argv(monkeypatch):
    seen = {}

    def fake_main():
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr(sys, "argv", ["pinky", "serve", "--server", "outreach"])
    monkeypatch.setattr("pinky_outreach.__main__.main", fake_main)

    run_serve(server="outreach")

    assert seen["argv"] == ["pinky-outreach"]


def test_serve_all_starts_both_servers(monkeypatch):
    """server=all spawns memory and outreach as subprocesses and waits on both."""
    spawned = []

    class FakeProc:
        def __init__(self, cmd):
            self.cmd = cmd
            self.waited = False

        def wait(self):
            self.waited = True

        def terminate(self):
            pass

    def fake_popen(cmd, *args, **kwargs):
        proc = FakeProc(cmd)
        spawned.append(proc)
        return proc

    monkeypatch.setattr("pinky_cli.serve.subprocess.Popen", fake_popen)

    run_serve(server="all")

    modules = [proc.cmd[2] for proc in spawned]
    assert modules == ["pinky_memory", "pinky_outreach"]
    assert all(proc.cmd[0] == sys.executable for proc in spawned)
    assert all(proc.cmd[1] == "-m" for proc in spawned)
    assert all(proc.waited for proc in spawned)


# ---------------------------------------------------------------------------
# pinky connect
# ---------------------------------------------------------------------------


def test_connect_writes_project_mcp_json(tmp_path, monkeypatch):
    """connect writes .mcp.json in cwd with sys.executable, not ~/.claude/settings.json."""
    monkeypatch.chdir(tmp_path)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test")

    run_connect()

    mcp_file = tmp_path / ".mcp.json"
    assert mcp_file.exists()
    assert not (fake_home / ".claude" / "settings.json").exists()

    config = json.loads(mcp_file.read_text())
    servers = config["mcpServers"]
    assert servers["pinky-memory"]["command"] == sys.executable
    assert servers["pinky-outreach"]["command"] == sys.executable
    assert servers["pinky-memory"]["env"]["OPENAI_API_KEY"] == "sk-test"
    assert servers["pinky-outreach"]["env"]["TELEGRAM_BOT_TOKEN"] == "tg-test"


def test_connect_preserves_existing_credentials(tmp_path, monkeypatch):
    """Re-running without env vars must not clobber stored credentials."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    existing = {
        "mcpServers": {
            "pinky-memory": {
                "command": "python",
                "args": ["-m", "pinky_memory"],
                "env": {"OPENAI_API_KEY": "sk-existing"},
            },
            "pinky-outreach": {
                "command": "python",
                "args": ["-m", "pinky_outreach"],
                "env": {"TELEGRAM_BOT_TOKEN": "tg-existing"},
            },
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(existing))

    run_connect()

    config = json.loads((tmp_path / ".mcp.json").read_text())
    servers = config["mcpServers"]
    assert servers["pinky-memory"]["env"]["OPENAI_API_KEY"] == "sk-existing"
    assert servers["pinky-outreach"]["env"]["TELEGRAM_BOT_TOKEN"] == "tg-existing"


def test_connect_omits_unset_credentials_with_warning(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    run_connect()

    config = json.loads((tmp_path / ".mcp.json").read_text())
    servers = config["mcpServers"]
    assert "OPENAI_API_KEY" not in servers["pinky-memory"]["env"]
    assert "TELEGRAM_BOT_TOKEN" not in servers["pinky-outreach"]["env"]
    out = capsys.readouterr().out
    assert "Warning: OPENAI_API_KEY is not set" in out
    assert "Warning: TELEGRAM_BOT_TOKEN is not set" in out


def test_connect_aborts_cleanly_on_invalid_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    mcp_file = tmp_path / ".mcp.json"
    mcp_file.write_text("{not json")

    with pytest.raises(SystemExit) as excinfo:
        run_connect()

    assert excinfo.value.code == 1
    assert "not valid JSON" in capsys.readouterr().out
    assert mcp_file.read_text() == "{not json"


def test_connect_preserves_unrelated_keys(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test")
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other-server": {"command": "foo"}}})
    )

    run_connect()

    config = json.loads((tmp_path / ".mcp.json").read_text())
    assert config["mcpServers"]["other-server"] == {"command": "foo"}


# ---------------------------------------------------------------------------
# pinky run
# ---------------------------------------------------------------------------


def test_run_forwards_mode_to_daemon(monkeypatch):
    from pinky_cli.__main__ import main as cli_main

    seen = {}

    def fake_daemon_main():
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr("pinky_daemon.__main__.main", fake_daemon_main)
    monkeypatch.setattr(
        sys, "argv", ["pinky", "run", "--mode", "poll", "--config", "my.yaml"]
    )

    cli_main()

    assert seen["argv"] == [
        "pinky-daemon",
        "--mode", "poll",
        "--config", "my.yaml",
        "--working-dir", ".",
    ]


def test_run_defaults_to_api_mode(monkeypatch):
    from pinky_cli.__main__ import main as cli_main

    seen = {}

    def fake_daemon_main():
        seen["argv"] = list(sys.argv)

    monkeypatch.setattr("pinky_daemon.__main__.main", fake_daemon_main)
    monkeypatch.setattr(sys, "argv", ["pinky", "run"])

    cli_main()

    assert seen["argv"][:3] == ["pinky-daemon", "--mode", "api"]
