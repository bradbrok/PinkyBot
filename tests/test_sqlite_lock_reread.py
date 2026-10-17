"""A lock omitted from one paged snapshot can appear in a later read."""

import os
from pathlib import Path

import pytest

from pinky_daemon import sqlite_lock_check as lock_check


class ReadScript:
    def __init__(self, texts):
        self.texts = texts
        self.calls = []

    def __call__(self):
        text = self.texts[min(len(self.calls), len(self.texts) - 1)]
        self.calls.append(text)
        return text


def _stores(tmp_path, names=("s",)):
    stores = []
    for name in names:
        path = tmp_path / f"{name}.db"
        path.touch()
        Path(str(path) + "-shm").touch()
        stores.append([name, str(path)])
    return stores


def _line(
    store, label, *, pid=None, kind="READ", waiter=False, device=None, end=None,
    start=None, inode=None,
):
    suffix, offset, length = ("", 1073741826, 510) if label == "SHARED" else ("-shm", 128, 1)
    info = Path(store[1] + suffix).stat()
    major, minor = device or (os.major(info.st_dev), os.minor(info.st_dev))
    owner = os.getpid() if pid is None else pid
    start = offset if start is None else start
    inode = info.st_ino if inode is None else inode
    stop = start + length - 1 if end is None else end
    return (
        f"1: {'-> ' if waiter else ''}POSIX  ADVISORY  {kind}  {owner} "
        f"{major:02x}:{minor:02x}:{inode} {start} {stop}"
    )


def _snapshot(stores, absent=()):
    return "\n".join(
        _line(store, label)
        for store in stores for label in ("SHARED", "DMS")
        if (store[0], label) not in absent
    )


def _invoke(stores, pid, read, sleep):
    return lock_check._probe_linux(stores, pid, read=read, sleep=sleep)


def _probe(stores, texts):
    read = ReadScript(texts)
    pauses = []

    def sleep(delay=None):
        pauses.append(delay)

    result = _invoke(stores, os.getpid(), read, sleep)
    return result, read.calls, pauses


def _expected(missing=(), inconclusive=()):
    return {"missing": list(missing), "inconclusive": list(inconclusive),
            "reason": "device_mismatch"}


def test_empty_store_list_reads_once_without_sleep():
    result, calls, pauses = _probe([], [""])
    assert result == _expected()
    assert len(calls) == 1
    assert pauses == []


def test_healthy_snapshot_reads_once_without_sleep(tmp_path):
    stores = _stores(tmp_path)
    result, calls, pauses = _probe(stores, [_snapshot(stores)])
    assert result == _expected()
    assert len(calls) == 1
    assert pauses == []


def test_skipped_shared_lock_appears_on_second_read(tmp_path):
    stores = _stores(tmp_path)
    result, calls, pauses = _probe(stores, [
        _snapshot(stores, {("s", "SHARED")}), _snapshot(stores),
    ])
    assert result == _expected()
    assert len(calls) == 2
    assert pauses == [0.02]


def test_persistent_missing_lock_exhausts_five_reads(tmp_path):
    stores = _stores(tmp_path)
    result, calls, pauses = _probe(stores, [_snapshot(stores, {("s", "SHARED")})])
    assert result == _expected(["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_union_keeps_locks_that_disappear_from_later_snapshot(tmp_path):
    stores = _stores(tmp_path, ("a", "b"))
    result, calls, pauses = _probe(stores, [
        _snapshot(stores, {("a", "SHARED")}),
        _snapshot(stores, {("b", "DMS")}),
    ])
    assert result == _expected()
    assert len(calls) == 2
    assert pauses == [0.02]


@pytest.mark.parametrize("appears_on", [5, 6])
def test_fifth_read_counts_but_sixth_is_outside_budget(tmp_path, appears_on):
    stores = _stores(tmp_path)
    text = _snapshot(stores, {("s", "SHARED")})
    result, calls, pauses = _probe(stores, [text] * (appears_on - 1) + [_snapshot(stores)])
    assert result == _expected([] if appears_on == 5 else ["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_inconclusive_device_mismatch_does_not_retry(tmp_path):
    stores = _stores(tmp_path)
    info = Path(stores[0][1]).stat()
    text = "\n".join([
        _line(stores[0], "SHARED", device=(os.major(info.st_dev) + 1, os.minor(info.st_dev))),
        _line(stores[0], "DMS"),
    ])
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(inconclusive=["s:SHARED"])
    assert len(calls) == 1
    assert pauses == []


@pytest.mark.parametrize("invalid", ["other_pid", "write", "waiter", "short_range"])
def test_noncovering_or_ineligible_lines_remain_missing(tmp_path, invalid):
    stores = _stores(tmp_path)
    kwargs = {
        "other_pid": {"pid": os.getpid() + 1},
        "write": {"kind": "WRITE"},
        "waiter": {"waiter": True},
        "short_range": {"end": 1073742334},
    }[invalid]
    text = _line(stores[0], "DMS") + "\n" + _line(stores[0], "SHARED", **kwargs)
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_truncated_read_record_is_ignored(tmp_path):
    stores = _stores(tmp_path)
    truncated = _line(stores[0], "SHARED").rsplit(" ", 1)[0]
    assert len(truncated.split()) == 7
    text = _line(stores[0], "DMS") + "\n" + truncated
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_read_starting_after_offset_remains_missing(tmp_path):
    stores = _stores(tmp_path)
    text = "\n".join([
        _line(stores[0], "DMS"),
        _line(stores[0], "SHARED", start=1073741827, end=1073742335),
    ])
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_covering_identity_wins_over_foreign_device_match(tmp_path):
    stores = _stores(tmp_path)
    info = Path(stores[0][1]).stat()
    foreign = (os.major(info.st_dev) + 1, os.minor(info.st_dev))
    text = "\n".join([
        _line(stores[0], "SHARED", device=foreign),
        _line(stores[0], "SHARED"),
        _line(stores[0], "DMS"),
    ])
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected()
    assert len(calls) == 1
    assert pauses == []


@pytest.mark.parametrize("invalid", ["other_inode", "late_start", "short_end"])
def test_foreign_device_match_must_cover_the_same_inode(tmp_path, invalid):
    stores = _stores(tmp_path)
    info = Path(stores[0][1]).stat()
    foreign = (os.major(info.st_dev) + 1, os.minor(info.st_dev))
    kwargs = {
        "other_inode": {"inode": info.st_ino + 1},
        "late_start": {"start": 1073741827, "end": 1073742335},
        "short_end": {"end": 1073742334},
    }[invalid]
    text = _line(stores[0], "DMS") + "\n" + _line(
        stores[0], "SHARED", device=foreign, **kwargs,
    )
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(["s:SHARED"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_absent_shared_memory_file_stays_missing(tmp_path):
    stores = _stores(tmp_path)
    text = _line(stores[0], "SHARED")
    Path(stores[0][1] + "-shm").unlink()
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected(["s:DMS"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_probe_output_has_only_the_existing_keys(tmp_path):
    stores = _stores(tmp_path)
    result, _, _ = _probe(stores, [_snapshot(stores)])
    assert set(result) == {"missing", "inconclusive", "reason"}


def test_both_store_locks_can_appear_after_an_empty_read(tmp_path):
    stores = _stores(tmp_path)
    result, calls, pauses = _probe(stores, ["", _snapshot(stores)])
    assert result == _expected()
    assert len(calls) == 2
    assert pauses == [0.02]


def test_one_budget_recovers_different_missing_locks_across_stores(tmp_path):
    stores = _stores(tmp_path, ("a", "b"))
    result, calls, pauses = _probe(stores, [
        _snapshot(stores, {("a", "SHARED"), ("b", "DMS")}), _snapshot(stores),
    ])
    assert result == _expected()
    assert len(calls) == 2
    assert pauses == [0.02]


def test_one_budget_bounds_persistent_misses_across_stores(tmp_path):
    stores = _stores(tmp_path, ("a", "b"))
    result, calls, pauses = _probe(stores, [
        _snapshot(stores, {("a", "SHARED"), ("b", "DMS")}),
    ])
    assert result == _expected(["a:SHARED", "b:DMS"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_missing_order_follows_store_order_then_shared_before_dms(tmp_path):
    stores = _stores(tmp_path, ("b", "a"))
    result, calls, pauses = _probe(stores, [""])
    assert result == _expected(["b:SHARED", "b:DMS", "a:SHARED", "a:DMS"])
    assert len(calls) == 5
    assert pauses == [0.02] * 4


def test_open_ended_read_lock_covers_the_shared_range(tmp_path):
    stores = _stores(tmp_path)
    text = _line(stores[0], "SHARED", end="EOF") + "\n" + _line(stores[0], "DMS")
    result, calls, pauses = _probe(stores, [text])
    assert result == _expected()
    assert len(calls) == 1
    assert pauses == []


def test_read_seam_reads_the_proc_lock_path(tmp_path, monkeypatch):
    original = Path.read_text
    seen = []

    def read_text(path, *args, **kwargs):
        if path == Path("/proc/locks"):
            seen.append(path)
            return "recorded locks"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert lock_check._read_proc_locks() == "recorded locks"
    assert seen == [Path("/proc/locks")]
    other = tmp_path / "other.txt"
    other.write_text("unchanged")
    assert other.read_text() == "unchanged"


@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_hooks_resolve_at_call_time(tmp_path, monkeypatch, explicit_none):
    stores = _stores(tmp_path)
    first = ReadScript([_snapshot(stores)])
    second = ReadScript([_snapshot(stores, {("s", "SHARED")}), _snapshot(stores)])
    pauses = []
    kwargs = {"read": None, "sleep": None} if explicit_none else {}
    monkeypatch.setattr(lock_check, "_read_proc_locks", first)
    monkeypatch.setattr(lock_check.time, "sleep", pauses.append)
    assert lock_check._probe_linux(stores, os.getpid(), **kwargs) == _expected()
    assert len(first.calls) == 1 and pauses == []
    monkeypatch.setattr(lock_check, "_read_proc_locks", second)
    assert lock_check._probe_linux(stores, os.getpid(), **kwargs) == _expected()
    assert len(second.calls) == 2 and pauses == [0.02]
