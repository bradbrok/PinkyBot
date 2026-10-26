"""Real temporary releases exercise the guard independently of the daemon."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_deploy_guard import TOKEN, guard_module, load


@pytest.fixture
def alert_server():
    messages = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(body) == {"chat_id", "text"}
            assert len(body["text"]) <= 500 and TOKEN not in body["text"]
            messages.append(body["text"])
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", messages=messages)
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


ENTRY = """import argparse, asyncio, json, os, socket, sys, time
from pathlib import Path
import pinky_daemon
print("TREE_IMPORT=" + pinky_daemon.__file__, flush=True)
parser = argparse.ArgumentParser()
parser.add_argument("--data", required=True)
parser.add_argument("--url", required=True)
parser.add_argument("--now", type=float, default=1000.0)
parser.add_argument("--hang", action="store_true")
args = parser.parse_args()
repo = Path(__file__).resolve().parent
state = Path(args.data) / "deploy"
if not (repo / "src/pinky_daemon/deploy_guard.py").exists():
    (Path(args.data) / "legacy-ready").touch()
    sys.exit(0)
from pinky_daemon import deploy_guard as g
settings = g.GuardSettings(settle_seconds=0.001, start_deadline_seconds=2.0)
ctx = g.begin_start(str(repo), state, settings=settings, clock=lambda: args.now,
                    sleep=lambda seconds: None, host="fixture-node", log_path="fixture.log",
                    sender=lambda directory, text: g.send_alert(directory, text, base_url=args.url))
async def notify(text):
    with (Path(args.data) / "owner.jsonl").open("a") as output:
        output.write(json.dumps(text) + "\\n")
    return True
async def ready():
    await ctx.ready(owner_notify=notify)
try:
    ctx.activate()
    if args.hang:
        time.sleep(60)
    elif (repo / "release-kind").read_text() == "fail":
        raise RuntimeError("synthetic startup failure")
    else:
        asyncio.run(ready())
except BaseException as exc:
    ctx.failed(exc)
    raise
finally:
    ctx.close()
"""


@pytest.fixture
def release_factory(tmp_path, alert_server):
    def make(*, guard_aware=True, previous_fails=False, armed=True):
        g = guard_module()
        tree = tmp_path / "release"
        package = tree / "src/pinky_daemon"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (tree / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        (tree / "entry.py").write_text(ENTRY)
        (tree / "release-kind").write_text("fail" if previous_fails else "ready")
        source = Path(g.__file__).read_bytes()
        if guard_aware:
            (package / "deploy_guard.py").write_bytes(source)

        def git(*args):
            return (
                subprocess.check_output(["git", *args], cwd=tree, stderr=subprocess.PIPE)
                .decode()
                .strip()
            )

        git("init", "-b", "main")
        git("config", "user.name", "Fixture Author")
        git("config", "user.email", "fixture@example.invalid")
        git("add", ".")
        git("commit", "-m", "Previous fixture")
        previous = git("rev-parse", "HEAD")
        (package / "deploy_guard.py").write_bytes(source)
        (tree / "release-kind").write_text("fail")
        git("add", ".")
        git("commit", "--allow-empty", "-m", "Target fixture")
        target = git("rev-parse", "HEAD")
        data = tmp_path / "data"
        data.mkdir(mode=0o700)
        state = data / "deploy"
        routes = [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ]
        if armed:
            g.arm_deploy(
                str(tree),
                state,
                previous_ref=previous,
                target_ref=target,
                previous_label="previous",
                target_label="target",
                deps_rebuilt=False,
                forced_reset=False,
                routes=routes,
                clock=lambda: 900.0,
            )
        else:
            g.refresh_alert_route(state, routes)
        env = dict(os.environ, PYTHONPATH=str(tree / "src"), PYTHONDONTWRITEBYTECODE="1")

        def command(*, now=1000.0, hang=False):
            return [
                sys.executable,
                "-B",
                str(tree / "entry.py"),
                "--data",
                str(data),
                "--url",
                alert_server.url,
                "--now",
                str(now),
            ] + (["--hang"] if hang else [])

        def start(**kwargs):
            result = subprocess.run(
                command(**kwargs), cwd=tree, env=env, text=True, capture_output=True, timeout=10
            )
            assert f"TREE_IMPORT={package / '__init__.py'}" in result.stdout
            assert TOKEN not in result.stdout + result.stderr
            return result

        return SimpleNamespace(
            g=g,
            tree=tree,
            package=package,
            data=data,
            state=state,
            previous=previous,
            target=target,
            git=git,
            start=start,
            command=command,
            env=env,
            server=alert_server,
        )

    return make


@pytest.mark.parametrize(
    "guard_aware", [True, False], ids=["aware-predecessor", "legacy-predecessor"]
)
def test_temporary_release_rolls_back_and_reports_once(release_factory, guard_aware):
    fx = release_factory(guard_aware=guard_aware)
    for n in range(1, 4):
        assert fx.start().returncode == 1
        assert len(load(fx.state)["attempts"]) == n
    assert not fx.server.messages
    assert fx.start().returncode == 75
    assert fx.git("rev-parse", "HEAD") == fx.previous
    assert load(fx.state)["state"] == "rolled_back"
    assert len(fx.server.messages) == int(not guard_aware)
    assert fx.start().returncode == 0
    if guard_aware:
        assert load(fx.state)["state"] == "reported"
        owner = [json.loads(line) for line in (fx.data / "owner.jsonl").read_text().splitlines()]
        assert len(owner) == 1 and "serving again" in owner[0]
        assert not fx.server.messages
    else:
        assert "starting" in fx.server.messages[0] and "not confirm" in fx.server.messages[0]
        assert not (fx.data / "owner.jsonl").exists()


def test_previous_release_failure_alert_has_six_hour_suppression(release_factory):
    fx = release_factory(previous_fails=True)
    for _ in range(3):
        assert fx.start().returncode == 1
    assert fx.start().returncode == 75
    for _ in range(3):
        assert fx.start().returncode == 1
    assert len(load(fx.state)["post_rollback_attempts"]) == 3
    assert len(fx.server.messages) == 1
    assert "does not start either" in fx.server.messages[0]
    assert fx.start().returncode == 1
    assert len(fx.server.messages) == 1
    assert fx.start(now=1000.0 + 21600).returncode == 1
    assert len(fx.server.messages) == 2


@pytest.mark.parametrize("armed", [True, False])
def test_start_deadline_only_ends_an_armed_start(release_factory, armed):
    fx = release_factory(armed=armed)
    child = subprocess.Popen(
        fx.command(hang=True),
        cwd=fx.tree,
        env=fx.env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if armed:
            out, err = child.communicate(timeout=8)
            assert child.returncode == 75
            attempt = load(fx.state)["attempts"][-1]
            assert attempt["failure"] == "start deadline exceeded"
            assert TOKEN not in out + err
        else:
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=2.5)
            assert child.poll() is None
    finally:
        if child.poll() is None:
            child.terminate()
        out, _ = child.communicate(timeout=5)
        assert f"TREE_IMPORT={fx.package / '__init__.py'}" in out


def test_guard_import_is_standard_library_only(tmp_path):
    g = guard_module()
    source = Path(g.__file__).read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert not node.level
            names = [node.module]
        else:
            continue
        assert all(name.split(".")[0] in sys.stdlib_module_names for name in names)
    package = tmp_path / "src/pinky_daemon"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "deploy_guard.py").write_text(source)
    code = (
        f"import sys; sys.path.insert(0, {str(package.parent)!r}); "
        "import pinky_daemon.deploy_guard as g; print(g.__file__)"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(package / "deploy_guard.py")


def test_api_guard_is_first_after_argument_parsing():
    from pinky_daemon import __main__ as entry

    tree = ast.parse(Path(entry.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    for index, statement in enumerate(main.body):
        if any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "parse_args"
            for n in ast.walk(statement)
        ):
            following = main.body[index + 1]
            assert any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_start_api_guard"
                for n in ast.walk(following)
            )
            break
    else:
        pytest.fail("argument parser not found")
    helper = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_start_api_guard"
    )
    assert isinstance(helper.body[0], ast.If)
    assert any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Constant) and n.value.value is None
        for n in ast.walk(helper.body[0])
    )
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "begin_start"
        for n in ast.walk(helper)
    )


@pytest.mark.parametrize("mode", ["api", "poll", "check-startup"])
def test_dotenv_failure_is_accounted_only_for_api(tmp_path, monkeypatch, capsys, mode):
    guard_module()
    from pinky_daemon import __main__ as entry

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["pinky", "--mode", mode, "--db-path", str(data / "db")])

    def unreadable():
        raise PermissionError("synthetic dotenv unreadable")

    monkeypatch.setattr(entry, "_load_dotenv", unreadable)
    monkeypatch.setattr(entry, "_install_faulthandler", lambda: None)
    if mode == "check-startup":
        with pytest.raises(SystemExit) as ended:
            entry.main()
        assert ended.value.code == 2
        assert capsys.readouterr().err.count("startup check could not complete:") == 1
    else:
        with pytest.raises(PermissionError):
            entry.main()
    state = data / "deploy"
    if mode == "api":
        assert len((state / "starts.jsonl").read_text().splitlines()) == 1
        assert load(state, "last_failure.json")["exc_type"] == "PermissionError"
        assert load(state, "last_failure.json")["detail"] == ""
    else:
        assert not state.exists()


@pytest.mark.parametrize("busy", [True, False])
def test_five_authority_exits_do_not_count_busy_starts(tmp_path, monkeypatch, busy):
    g = guard_module()
    from pinky_daemon import __main__ as entry
    from pinky_daemon.store_authority import StoreAuthorityError

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setattr(sys, "argv", ["pinky", "--mode", "api", "--db-path", str(data / "db")])
    monkeypatch.setattr(entry, "_load_dotenv", lambda: None)
    notices = []
    monkeypatch.setattr(g, "send_alert", lambda directory, text: notices.append(text) or True)
    monkeypatch.setattr(g.socket, "gethostname", lambda: "fixture-node")
    monkeypatch.setattr(g.time, "sleep", lambda seconds: None)

    def stopped(args):
        raise StoreAuthorityError(
            "daemon store authority is already held" if busy else "other error"
        )

    monkeypatch.setattr(entry, "_run_api", stopped)
    for _ in range(5):
        with pytest.raises(StoreAuthorityError):
            entry.main()
    state = data / "deploy"
    lines = [json.loads(line) for line in (state / "starts.jsonl").read_text().splitlines()]
    assert len(lines) == 5
    assert all(bool(line.get("lock_busy")) is busy for line in lines)
    assert (state / "last_failure.json").exists() is not busy
    assert not notices
    with pytest.raises(StoreAuthorityError):
        entry.main()
    assert len(notices) == int(not busy)


@pytest.mark.parametrize("broken", [False, True])
def test_startup_check_includes_pure_guard_selftest(tmp_path, monkeypatch, capsys, broken):
    g = guard_module()
    from pinky_daemon import __main__ as entry

    if broken:
        cases = list(g.SELFTEST_CASES)
        cases[0] = (*cases[0][:-1], "wrong expectation")
        monkeypatch.setattr(g, "SELFTEST_CASES", cases)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    rc = entry._run_startup_check(SimpleNamespace(db_path=str(data / "db")))
    out, err = capsys.readouterr()
    assert rc == (2 if broken else 0)
    if broken:
        assert out == "" and err.count("startup check could not complete:") == 1
    else:
        assert json.loads(out)["checked"] == ["import", "layout", "guard_selftest"]
    assert not (data / "deploy").exists()


def test_dependency_install_has_one_implementation():
    from pinky_daemon import api

    g = guard_module()
    application = ast.parse(Path(api.__file__).read_text())
    guard = ast.parse(Path(g.__file__).read_text())

    def calls(tree):
        return [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (
                (isinstance(n.func, ast.Name) and n.func.id == "install_dependencies")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "install_dependencies")
            )
        ]

    assert len(calls(application)) == 2
    assert len(calls(guard)) == 1
    assert not any(
        isinstance(n, ast.List)
        and any(isinstance(v, ast.Constant) and v.value == "pip" for v in n.elts)
        for n in ast.walk(application)
    )


def test_alert_sender_does_not_follow_redirects(tmp_path, caplog):
    g = guard_module()
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append("POST")
            self.send_response(302)
            self.send_header("Location", "/followed")
            self.end_headers()

        def do_GET(self):
            calls.append("GET")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *args):
            pass

    state = tmp_path / "deploy"
    g.refresh_alert_route(
        state,
        [
            dict(
                platform="telegram",
                account_id="fixture-account",
                conversation_id="fixture-chat",
                token=TOKEN,
            )
        ],
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        assert not g.send_alert(
            state, "fixture alert", base_url=f"http://127.0.0.1:{server.server_port}"
        )
        assert calls == ["POST"]
        assert TOKEN not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
