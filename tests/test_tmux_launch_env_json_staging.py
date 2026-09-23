"""Nonce cancellation and publication remain atomic across process boundaries."""

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pinky_daemon import tmux_launch_env
from tests.tmux_env_r3_support import NONCE, OTHER_NONCE, SCOPE, SECRET, cancel, stage
from tests.tmux_env_support import secret_files


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


def test_stage_uses_exact_scope_despite_target_socket_environment(home, monkeypatch):
    monkeypatch.setenv("TMUX", "/target/socket,42,0")
    monkeypatch.setenv("TMUX_TMPDIR", "/target/tmp")
    result = stage(home)
    assert set(result) == {"path"}
    path = Path(result["path"])
    assert path == home / ".local/state/pinkybot/tmux-launch-env" / SCOPE / f"env-{NONCE}.json"
    assert json.loads(path.read_text()) == {"nonce": NONCE, "env": {"SECRET": SECRET}}


@pytest.mark.parametrize("nonce", ["", "a" * 31, "A" * 32, "g" * 32, "a" * 33,
                                  "a" * 32 + "\n", "../" + "a" * 32])
def test_nonce_is_strict_before_side_effects(home, nonce):
    with pytest.raises(ValueError):
        stage(home, nonce=nonce)
    assert not list(home.iterdir())


@pytest.mark.parametrize("scope", ["", "c" * 63, "C" * 64, "z" * 64,
                                  "c" * 64 + "\n", "../" + "c" * 64])
def test_scope_is_strict_before_side_effects(home, scope):
    with pytest.raises(ValueError):
        stage(home, scope=scope)
    assert not list(home.iterdir())


def test_cancel_only_its_nonce_and_preserves_replacement(home):
    first = Path(stage(home)["path"])
    second = Path(stage(home, nonce=OTHER_NONCE)["path"])
    assert first.exists(), "staging a replacement must not prune a young launch"
    cancel()
    assert not first.exists()
    assert json.loads(second.read_text())["env"]["SECRET"] == SECRET


def test_cleanup_first_prevents_late_stager_recreation(home):
    cancel()
    with pytest.raises((RuntimeError, ValueError)):
        stage(home)
    assert not secret_files(home, SECRET)


@pytest.mark.parametrize("deadline", [float("nan"), float("inf"), -1, 10**20])
def test_publication_deadline_is_finite_and_bounded_before_side_effects(home, deadline):
    with pytest.raises(ValueError):
        stage(home, deadline=deadline)
    assert not list(home.iterdir())


def test_recent_cancellation_metadata_is_not_collected(home):
    cancel()
    directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
    files = list(directory.iterdir())
    assert files
    recent = time.time() - 5 * 60
    for path in files:
        os.utime(path, (recent, recent))
    stage(home, nonce=OTHER_NONCE)
    assert all(path.exists() for path in files), "metadata retention must greatly exceed publication time"
    with pytest.raises((RuntimeError, ValueError)):
        stage(home)


def test_gc_removes_old_metadata_but_expired_launch_cannot_recreate(home):
    cancel()
    directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
    old_metadata = [p for p in directory.iterdir() if p.is_file()]
    assert old_metadata, "cancellation must persist across target helper processes"
    old = time.time() - 7 * 24 * 3600
    for path in old_metadata:
        os.utime(path, (old, old))
    stage(home, nonce=OTHER_NONCE)
    assert all(not path.exists() for path in old_metadata), "expired metadata must be collected"
    with pytest.raises((RuntimeError, ValueError)):
        stage(home, deadline=time.time() - 301)
    assert not (directory / f"env-{NONCE}.json").exists()
    assert not any(NONCE in p.name for p in directory.iterdir()), "expired stager recreated metadata"


def test_orphan_sweep_removes_old_secret_but_preserves_young_launch(home):
    old_path = Path(stage(home)["path"])
    young_path = Path(stage(home, nonce=OTHER_NONCE)["path"])
    old = time.time() - 7 * 24 * 3600
    os.utime(old_path, (old, old))
    stage(home, nonce="d4" * 16)
    assert not old_path.exists()
    assert young_path.exists()


def test_secret_orphan_ttl_is_ten_minutes_not_metadata_ttl(home):
    old_path = Path(stage(home)["path"])
    young_path = Path(stage(home, nonce=OTHER_NONCE)["path"])
    now = time.time()
    metadata = old_path.with_suffix(".lock")
    os.utime(old_path, (now - 900, now - 900))
    os.utime(metadata, (now - 900, now - 900))
    os.utime(young_path, (now - 30, now - 30))
    stage(home, nonce="d4" * 16)
    assert not old_path.exists(), "secret orphans must not wait for metadata retention"
    assert young_path.exists(), "a live launch still needs its secret file"
    assert metadata.exists(), "cancellation metadata needs a longer retention period"


def test_empty_only_launch_sweeps_existing_orphans_without_creating_state(home):
    assert stage(home, env={"SECRET": ""}) is None
    assert not list(home.iterdir())
    old_path = Path(stage(home)["path"])
    young_path = Path(stage(home, nonce=OTHER_NONCE)["path"])
    old = time.time() - 900
    os.utime(old_path, (old, old))
    assert stage(home, env={"SECRET": ""}) is None
    assert not old_path.exists()
    assert young_path.exists()


def test_gc_never_follows_foreign_symlink(home):
    path = Path(stage(home)["path"])
    foreign = home / "foreign"
    foreign.write_text("keep")
    link = path.with_name(f"env-{OTHER_NONCE}.json")
    link.symlink_to(foreign)
    old = time.time() - 7 * 24 * 3600
    os.utime(link, (old, old), follow_symlinks=False)
    stage(home, nonce="d4" * 16)
    assert foreign.read_text() == "keep"


def test_fdopen_write_failure_closes_descriptor_once(home, monkeypatch):
    real_fdopen = os.fdopen
    closed = []
    real_close = os.close

    class BrokenOutput:
        def __init__(self, fd, *args, **kwargs):
            self.fd = fd
            self.stream = real_fdopen(fd, *args, **kwargs)

        def __enter__(self):
            return self

        def write(self, value):
            raise OSError("synthetic write failure")

        def __exit__(self, *args):
            self.stream.close()
            closed.append(self.fd)

    def close(fd):
        assert fd not in closed, "fdopen already owned and closed this descriptor"
        real_close(fd)

    monkeypatch.setattr(os, "fdopen", BrokenOutput)
    monkeypatch.setattr(os, "close", close)
    with pytest.raises(OSError, match="synthetic write failure"):
        stage(home)
    assert closed
    assert not secret_files(home, SECRET)


def worker(home, code, *args):
    module_root = str(Path(tmux_launch_env.__file__).parents[1])
    prelude = "import sys;sys.path.insert(0," + repr(module_root) + ");"
    return subprocess.Popen(
        [sys.executable, "-c", prelude + code, *map(str, args)],
        env={"HOME": str(home), "PATH": os.defpath},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


def await_file(path, process):
    until = time.monotonic() + 5
    while not path.exists() and process.poll() is None and time.monotonic() < until:
        time.sleep(0.01)
    if not path.exists():
        process.kill()
        out, err = process.communicate(timeout=5)
        pytest.fail(f"stager did not reach barrier: {out!r} {err!r}")


def finish(process):
    if process is not None:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_cleanup_waits_for_checked_but_not_yet_published_stager(home):
    # Assert the real file contract exists before the child process barrier.
    probe = Path(stage(home, nonce=OTHER_NONCE)["path"])
    ready, release = home / "ready", home / "release"
    code = '''
import os,time
from pathlib import Path
from pinky_daemon import tmux_launch_env as m
ready,release=map(Path,sys.argv[1:3])
real_open=os.open
def parked_open(name,flags,*args,**kwargs):
    if str(name).endswith("env-"+"a1"*16+".json") and flags & os.O_CREAT:
        ready.touch()
        while not release.exists(): time.sleep(.01)
    return real_open(name,flags,*args,**kwargs)
os.open=parked_open
m.stage_env({"SECRET":"synthetic-json-launch-value-82e6"},"c3"*32,"a1"*16,deadline=time.time()+30)
'''
    stager = worker(home, code, ready, release)
    cleaner = None
    try:
        await_file(ready, stager)
        lock_path = probe.with_name(f"env-{NONCE}.lock")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(lock_fd)
        cleaner = worker(home, '''
import fcntl,os
from pathlib import Path
from pinky_daemon import tmux_launch_env as m
original=fcntl.flock
expected=os.stat(sys.argv[2])
def witnessed_flock(fd,op):
    info=os.fstat(fd)
    assert (info.st_dev,info.st_ino)==(expected.st_dev,expected.st_ino)
    try:
        original(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        Path(sys.argv[1]).touch()
    else:
        raise AssertionError("cleanup did not contend with the publisher")
    return original(fd,op)
fcntl.flock=witnessed_flock
m.cancel_env("c3"*32,"a1"*16)
''', home / "cleanup-entered", lock_path)
        await_file(home / "cleanup-entered", cleaner)
        assert cleaner.poll() is None, "cleanup crossed the locked check/publication boundary"
        release.touch()
        assert stager.wait(timeout=5) == 0
        assert cleaner.wait(timeout=5) == 0
        assert not probe.with_name(f"env-{NONCE}.json").exists()
        assert probe.exists(), "unrelated launch was deleted"
    finally:
        release.touch()
        finish(stager)
        finish(cleaner)


def test_stale_lock_fd_after_gc_cannot_publish_after_deadline(home):
    cancel()
    directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
    ready, release = home / "opened-lock", home / "resume-lock"
    code = '''
import fcntl,json,os,time
from pathlib import Path
from pinky_daemon import tmux_launch_env as m
ready,release=map(Path,sys.argv[1:3])
original=fcntl.flock
parked=False
wall=time.time()
deadline=wall+60
mono=time.monotonic()
lease_end=mono+60
time.monotonic=lambda: mono
time.time=lambda: wall
lock_path=Path.home()/".local/state/pinkybot/tmux-launch-env"/("c3"*32)/("env-"+"a1"*16+".lock")
def parked_flock(fd,op):
    global parked
    if op & fcntl.LOCK_EX and not parked:
        parked=True
        info=os.fstat(fd)
        expected=lock_path.stat()
        assert (info.st_dev,info.st_ino)==(expected.st_dev,expected.st_ino)
        ready.write_text(json.dumps({"dev":info.st_dev,"ino":info.st_ino,"deadline":deadline}))
        while not release.exists(): time.sleep(.01)
        still_open=os.fstat(fd)
        ready.with_suffix(".resumed").write_text(json.dumps({"dev":still_open.st_dev,"ino":still_open.st_ino}))
        time.monotonic=lambda: lease_end+1
    return original(fd,op)
fcntl.flock=parked_flock
try:
    m.stage_env({"SECRET":"synthetic-json-launch-value-82e6"},"c3"*32,"a1"*16,deadline=deadline)
except (RuntimeError,ValueError):
    pass
else:
    raise AssertionError("expired stager published through stale lock fd")
'''
    stager = worker(home, code, ready, release)
    try:
        await_file(ready, stager)
        inode_a = json.loads(ready.read_text())
        lock_a = directory / f"env-{NONCE}.lock"
        assert (inode_a["dev"], inode_a["ino"]) == (lock_a.stat().st_dev, lock_a.stat().st_ino)
        old = time.time() - 7 * 24 * 3600
        old_files = list(directory.iterdir())
        for path in old_files:
            os.utime(path, (old, old))
        stage(home, nonce=OTHER_NONCE)
        assert all(not p.exists() for p in old_files), "GC did not remove the old lock inode"
        # Recreate only the lock pathname, without a cancellation marker. The
        # child's already-open inode must not bypass its publication deadline.
        locks = [p for p in old_files if p.suffix == ".lock"]
        assert len(locks) == 1
        locks[0].touch(mode=0o600)
        inode_b = locks[0].stat()
        assert (inode_b.st_dev, inode_b.st_ino) != (inode_a["dev"], inode_a["ino"])
        release.touch()
        out, err = stager.communicate(timeout=5)
        assert stager.returncode == 0, (out, err)
        resumed = json.loads(ready.with_suffix(".resumed").read_text())
        assert resumed == {"dev": inode_a["dev"], "ino": inode_a["ino"]}
        assert not (directory / f"env-{NONCE}.json").exists()
        assert [p for p in directory.iterdir() if NONCE in p.name] == locks
    finally:
        release.touch()
        finish(stager)


def test_deadline_expiring_during_publication_leaves_no_secret(home):
    # A process can be descheduled after the deadline check. It must recheck
    # before returning publication success, while still excluding cleanup.
    stage(home, nonce=OTHER_NONCE)
    code = '''
import os,time
from pinky_daemon import tmux_launch_env as m
original=os.open
now=time.time()
mono=time.monotonic()
time.monotonic=lambda: mono
time.time=lambda: now
def delayed_open(name,flags,*args,**kwargs):
    if str(name).endswith("env-"+"a1"*16+".json") and flags & os.O_CREAT:
        time.monotonic=lambda: mono+120
    return original(name,flags,*args,**kwargs)
os.open=delayed_open
try:
    m.stage_env({"SECRET":"synthetic-json-launch-value-82e6"},"c3"*32,"a1"*16,deadline=now+30)
except (RuntimeError,ValueError):
    pass
else:
    raise AssertionError("publication completed after deadline")
'''
    stager = worker(home, code)
    try:
        out, err = stager.communicate(timeout=5)
        assert stager.returncode == 0, (out, err)
        directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
        assert not (directory / f"env-{NONCE}.json").exists()
    finally:
        finish(stager)


@pytest.mark.parametrize("operation", ["stage", "cancel"])
def test_nonce_lock_retries_replaced_inode(home, monkeypatch, operation):
    path = Path(stage(home)["path"])
    path.unlink()
    lock_path = path.with_suffix(".lock")
    real_flock = fcntl.flock
    acquired = []

    def replace_first_lock(fd, flags):
        real_flock(fd, flags)
        if flags == fcntl.LOCK_EX:
            info = os.fstat(fd)
            acquired.append((info.st_dev, info.st_ino))
            if len(acquired) == 1:
                lock_path.unlink()
                lock_path.touch(mode=0o600)
                assert lock_path.stat().st_ino != info.st_ino

    monkeypatch.setattr(fcntl, "flock", replace_first_lock)
    if operation == "stage":
        assert Path(stage(home)["path"]).exists()
    else:
        cancel()
        assert lock_path.read_bytes() == b"cancelled\n"
    assert len(acquired) == 2
    assert acquired[0] != acquired[1]


def test_sweep_never_uses_replaced_lock_inode(home, monkeypatch):
    path = Path(stage(home)["path"])
    old = time.time() - 15 * 60
    os.utime(path, (old, old))
    lock_path = path.with_suffix(".lock")
    real_flock = fcntl.flock
    replacement_fd = None

    def replace_gc_lock(fd, flags):
        nonlocal replacement_fd
        if replacement_fd is None and flags == fcntl.LOCK_EX | fcntl.LOCK_NB:
            original = os.fstat(fd)
            lock_path.unlink()
            replacement_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            assert os.fstat(replacement_fd).st_ino != original.st_ino
            real_flock(replacement_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        real_flock(fd, flags)

    monkeypatch.setattr(fcntl, "flock", replace_gc_lock)
    try:
        stage(home, nonce=OTHER_NONCE)
        assert replacement_fd is not None, "the old candidate must reach the GC lock"
        assert path.exists(), "GC used an unlinked lock instead of the active replacement"
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)


def test_cancel_retries_missing_lock_after_partial_gc(home, monkeypatch):
    path = Path(stage(home)["path"])
    lock_path = path.with_suffix(".lock")
    old = time.time() - 2 * 24 * 60 * 60
    os.utime(lock_path, (old, old))
    os.utime(path, (old, old))
    real_flock, real_unlink = fcntl.flock, os.unlink
    collected = False
    acquired = []

    class InterruptedSweepError(Exception):
        pass

    def stop_after_lock_removal(name, *args, **kwargs):
        real_unlink(name, *args, **kwargs)
        if name == lock_path.name:
            raise InterruptedSweepError

    def collect_before_cancel_locks(fd, flags):
        nonlocal collected
        if flags == fcntl.LOCK_EX and not collected:
            collected = True
            acquired.append(os.fstat(fd).st_ino)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with monkeypatch.context() as during_gc:
                    during_gc.setattr(os, "listdir", lambda _: [lock_path.name, path.name])
                    during_gc.setattr(os, "unlink", stop_after_lock_removal)
                    with pytest.raises(InterruptedSweepError):
                        tmux_launch_env._sweep(directory)
                assert not lock_path.exists() and path.exists()
                assert os.fstat(fd).st_ino == acquired[0], "the canceller still holds A"
            finally:
                os.close(directory)
        real_flock(fd, flags)

    monkeypatch.setattr(fcntl, "flock", collect_before_cancel_locks)
    cancel()
    assert collected
    assert not path.exists()
    assert lock_path.stat().st_ino != acquired[0]
    assert lock_path.read_bytes() == b"cancelled\n"
