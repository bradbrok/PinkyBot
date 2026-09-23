"""The exec loader validates ownership and consumes data without shell parsing."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import tmux_launch_env
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_r3_support import (
    NONCE,
    OTHER_NONCE,
    SECRET,
    payload,
    probe_command,
    run_loader,
)
from tests.tmux_env_support import LaunchRecorder, secret_files


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


async def test_wire_is_json_and_only_daemon_nonce_path_returns(home):
    recorder = LaunchRecorder(home)
    control = _TmuxControl("json-wire", command_runner=recorder)
    await control.new_session(cwd=str(home), command=probe_command(), env={"SECRET": SECRET})
    paths = secret_files(home, SECRET)
    assert len(paths) == 1
    path = paths[0]
    assert path.suffix == ".json", "no staged secret may be shell source"
    data = json.loads(path.read_text())
    assert set(data) == {"nonce", "env"}
    assert data["env"] == {"SECRET": SECRET}
    assert path.name == f"env-{data['nonce']}.json"
    assert len(data["nonce"]) == 32 and set(data["nonce"]) <= set("0123456789abcdef")
    argv = shlex.split(recorder.tmux_calls[-1][-1])
    assert argv[:4] == ["exec", sys.executable, "-I", "-c"]
    assert argv[-3:] == [str(path), data["nonce"], probe_command()]
    assert SECRET not in " ".join(argv)
    result = run_loader(path, nonce=data["nonce"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["SECRET"] == SECRET
    assert not path.exists()


@pytest.mark.parametrize("shell", ["sh", "bash"])
@pytest.mark.parametrize("locale", ["ja_JP.SJIS", "zh_CN.GBK"])
async def test_locale_values_round_trip_without_shell_evaluation(home, shell, locale):
    binary = "/bin/sh" if shell == "sh" else shutil.which("bash")
    assert binary, "bash is required for the POSIX-mode regression witness"
    marker = home / "injection-marker"
    value = "$(touch " + shlex.quote(str(marker)) + ")'"
    env = {"LC_ALL": locale, "A": "с", "B": value, "SECRET": SECRET}
    recorder = LaunchRecorder(home)
    control = _TmuxControl("locale-roundtrip", command_runner=recorder)
    await control.new_session(cwd=str(home), command=probe_command(), env=env)
    argv = [binary, *( ["--posix"] if shell == "bash" else []), "-c",
            recorder.tmux_calls[-1][-1]]
    result = subprocess.run(argv, env={"HOME": str(home), "PATH": os.defpath},
                            capture_output=True, timeout=10)
    assert not marker.exists(), "a secret was parsed as shell program text"
    assert result.returncode == 0, result.stderr
    child = json.loads(result.stdout)
    for key, expected in env.items():
        assert child[key].encode("utf-8") == expected.encode("utf-8")
    assert not secret_files(home, SECRET)
    assert all(SECRET not in " ".join(call) and value not in " ".join(call)
               for call in recorder.tmux_calls)


async def test_oversized_environment_never_prevents_secret_unlink(home):
    recorder = LaunchRecorder(home)
    control = _TmuxControl("oversized-env", command_runner=recorder)
    await control.new_session(cwd=str(home), command="true", env={"LARGE": SECRET * 40000})
    paths = secret_files(home, SECRET)
    assert len(paths) == 1
    result = subprocess.run(["/bin/sh", "-c", recorder.tmux_calls[-1][-1]],
                            env={"HOME": str(home), "PATH": os.defpath},
                            capture_output=True, timeout=10)
    assert not paths[0].exists(), "unlink must happen before an E2BIG-prone exec"
    assert not secret_files(home, SECRET)
    assert SECRET.encode() not in result.stderr + result.stdout
    if result.returncode:
        assert result.stderr.strip() == b"launch environment load failed"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_loader_refuses_nonprivate_mode(home, mode):
    path = payload(home)
    path.chmod(mode)
    result = run_loader(path)
    assert result.returncode == 1
    assert not result.stdout
    assert SECRET.encode() not in result.stderr
    assert not path.exists(), "own nonce-bound bad payload is safe to remove"


@pytest.mark.parametrize("corruption", ["json", "env_list", "value_number", "bad_name",
                                        "reserved", "extra", "nul", "wrong_nonce"])
def test_loader_removes_own_invalid_payload_without_exec(home, corruption):
    path = payload(home)
    data = json.loads(path.read_text())
    if corruption == "json":
        path.write_text("{broken:" + SECRET)
    else:
        if corruption == "env_list":
            data["env"] = []
        elif corruption == "value_number":
            data["env"] = {"SECRET": 17}
        elif corruption == "bad_name":
            data["env"] = {"BAD-NAME": SECRET}
        elif corruption == "reserved":
            data["env"] = {"__PINKY_LAUNCH_COLLISION": SECRET}
        elif corruption == "extra":
            data["extra"] = SECRET
        elif corruption == "nul":
            data["env"] = {"SECRET": "a\x00b"}
        else:
            data["nonce"] = OTHER_NONCE
        path.write_text(json.dumps(data))
    result = run_loader(path)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr.strip() == b"launch environment load failed"
    assert not path.exists()


def test_loader_wrong_nonce_does_not_authorize_foreign_file_deletion(home):
    path = payload(home, nonce=OTHER_NONCE)
    before = path.read_bytes()
    result = run_loader(path, nonce=NONCE)
    assert result.returncode == 1 and not result.stdout
    assert path.read_bytes() == before


def test_loader_rejects_symlink_without_following_or_removing_target(home):
    target = payload(home, filename="foreign.json")
    link = target.with_name(f"env-{NONCE}.json")
    link.symlink_to(target)
    before = target.read_bytes()
    result = run_loader(link)
    assert result.returncode == 1 and not result.stdout
    assert target.read_bytes() == before


def test_loader_fifo_without_writer_refuses_promptly(home):
    path = payload(home)
    path.unlink()
    os.mkfifo(path, mode=0o600)
    source = Path(tmux_launch_env.__file__).read_text()
    result = subprocess.run(
        [sys.executable, "-I", "-c", source, str(path), NONCE, probe_command()],
        input=b"", capture_output=True, timeout=2,
        env={"HOME": str(home), "PATH": os.defpath},
    )
    assert result.returncode == 1 and not result.stdout
    assert result.stderr.strip() == b"launch environment load failed"


def loader():
    fn = getattr(tmux_launch_env, "load_env", None)
    assert callable(fn), "the isolated helper must expose its loader for syscall-order probes"
    return fn


def test_loader_wrong_owner_refuses_without_unlink(home, monkeypatch):
    path = payload(home)
    fn = loader()
    real_fstat = os.fstat

    def wrong_owner(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)

    monkeypatch.setattr(os, "fstat", wrong_owner)
    with pytest.raises(SystemExit) as error:
        fn(str(path), NONCE, "true")
    assert error.value.code == 1
    assert path.exists()


@pytest.mark.parametrize("unlink_error", [None, FileNotFoundError, PermissionError])
def test_loader_unlinks_before_environment_update_and_exec(home, monkeypatch, capsys, unlink_error):
    path = payload(home)
    fn = loader()
    calls = []
    real_unlink = os.unlink
    real_open = os.open
    descriptor = []

    def open_file(name, *args, **kwargs):
        fd = real_open(name, *args, **kwargs)
        if str(name) == str(path):
            descriptor.append(fd)
        return fd

    class ObservedEnvironment(dict):
        def update(self, *args, **kwargs):
            calls.append("environment")
            assert not path.exists()
            assert descriptor
            with pytest.raises(OSError):
                os.fstat(descriptor[0])
            return super().update(*args, **kwargs)

    def unlink(name, *args, **kwargs):
        calls.append("unlink")
        assert "SECRET" not in os.environ
        if unlink_error is FileNotFoundError:
            real_unlink(name, *args, **kwargs)
            raise FileNotFoundError(name)
        if unlink_error is PermissionError:
            raise PermissionError(SECRET)
        return real_unlink(name, *args, **kwargs)

    def execv(name, argv):
        calls.append("exec")
        assert not path.exists()
        assert os.environ["SECRET"] == SECRET
        assert name == "/bin/sh" and argv == ["/bin/sh", "-c", "true"]
        raise SystemExit(0)

    monkeypatch.delenv("SECRET", raising=False)
    monkeypatch.setattr(os, "environ", ObservedEnvironment(os.environ))
    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(os, "execv", execv)
    with pytest.raises(SystemExit) as error:
        fn(str(path), NONCE, "true")
    if unlink_error is PermissionError:
        assert error.value.code == 1 and "exec" not in calls
        assert "SECRET" not in os.environ
        assert capsys.readouterr().err.strip() == "launch environment load failed"
    else:
        assert error.value.code == 0
        assert calls == ["unlink", "environment", "exec"]


def test_cleanup_before_loader_open_exits_without_partial_environment(home):
    path = payload(home)
    path.unlink()
    result = run_loader(path)
    assert result.returncode == 1 and not result.stdout
    assert result.stderr.strip() == b"launch environment load failed"


async def test_helper_sources_are_cached_at_import_not_read_per_launch(home, monkeypatch):
    original = Path.read_text

    def forbidden(path, *args, **kwargs):
        assert "tmux_launch_env" not in path.name, "helper source was re-read during launch"
        return original(path, *args, **kwargs)

    from pinky_daemon.command_runner import RunuserCommandRunner

    recorder = LaunchRecorder(home)
    control = _TmuxControl("cached-source", command_runner=RunuserCommandRunner("test", inner=recorder))
    monkeypatch.setattr(Path, "read_text", forbidden)
    await control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET})
    assert len(recorder.tmux_calls) == 1
