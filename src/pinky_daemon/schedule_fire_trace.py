"""Best-effort scheduler observations, isolated from authoritative delivery IO."""

from __future__ import annotations

import hashlib
import logging
import queue
import sqlite3
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_WORKERS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="schedule-fire-trace")
OUTCOMES = (
    "delivered", "late_delivered", "observer_unmatched", "producer_no_user_turn",
    "never_pasted", "drain_parked", "trace_incomplete", "pending",
)
EDGES = ("enqueue", "paste", "observed", "accept", "abandon", "replay", "prune")
_COLUMNS = {
    "fire_id": "INTEGER",
    "agent_name": "TEXT NOT NULL DEFAULT ''",
    "schedule_name": "TEXT NOT NULL DEFAULT ''",
    "transport_kind": "TEXT NOT NULL DEFAULT ''",
    "prompt_hash": "TEXT NOT NULL DEFAULT ''",
    "cadence_seconds": "REAL NOT NULL DEFAULT 0",
    "enqueued_at": "REAL NOT NULL DEFAULT 0",
    "paste_at": "REAL NOT NULL DEFAULT 0",
    "paste_pointer": "TEXT NOT NULL DEFAULT ''",
    "paste_attempts": "INTEGER NOT NULL DEFAULT 0",
    "user_message_observed_at": "REAL NOT NULL DEFAULT 0",
    "user_message_pointer": "TEXT NOT NULL DEFAULT ''",
    "matched_at": "REAL NOT NULL DEFAULT 0",
    "matched_by": "TEXT NOT NULL DEFAULT ''",
    "receipt_accept_result": "INTEGER",
    "abandoned_at": "REAL NOT NULL DEFAULT 0",
    "abandon_reason": "TEXT NOT NULL DEFAULT ''",
    "late_accept_after_abandon": "INTEGER NOT NULL DEFAULT 0",
    "drain_parked_at": "REAL NOT NULL DEFAULT 0",
    "released_at": "REAL NOT NULL DEFAULT 0",
    "replay_count": "INTEGER NOT NULL DEFAULT 0",
    "outcome": "TEXT NOT NULL DEFAULT 'pending'",
    "outcome_at": "REAL NOT NULL DEFAULT 0",
    "notes": "TEXT NOT NULL DEFAULT ''",
    "updated_at": "REAL NOT NULL DEFAULT 0",
    "ledger_accepted_at": "REAL NOT NULL DEFAULT 0",
    "ledger_attempts": "INTEGER NOT NULL DEFAULT 0",
}


def derive_outcome(row: dict, ledger: dict | None = None) -> str:
    """Re-derive the diagnostic class without granting receipt authority."""
    ledger = dict(ledger or {})
    ledger["accepted_at"] = max(ledger.get("accepted_at", 0), row.get("ledger_accepted_at", 0))
    ledger["attempts"] = max(ledger.get("attempts", 0), row.get("ledger_attempts", 0))
    evidence = {"enqueue": "enqueued_at", "paste": "paste_at",
                "observed": "user_message_observed_at", "accept": "matched_at",
                "abandon": "abandoned_at"}
    if any(not row.get(evidence[edge], 0) for edge in row.get("failed_edges", ())
           if edge in evidence):
        return "trace_incomplete"
    if (ledger.get("accepted_at", 0) and not row.get("matched_at", 0)) or (
        ledger.get("attempts", 0) and not row.get("paste_at", 0)
    ):
        return "trace_incomplete"
    if row.get("matched_at", 0) and row.get("receipt_accept_result") == 1:
        age = row["matched_at"] - row["fired_at"]
        cadence = row.get("cadence_seconds", 0)
        late = (
            age > 900 or (cadence > 0 and age > cadence)
            or (0 < row.get("abandoned_at", 0) <= row["matched_at"])
        )
        return "late_delivered" if late else "delivered"
    if row.get("user_message_observed_at", 0):
        return "observer_unmatched"
    if row.get("paste_at", 0):
        return "producer_no_user_turn"
    if row.get("drain_parked_at", 0) > row.get("released_at", 0):
        return "drain_parked"
    return "never_pasted" if row.get("abandoned_at", 0) or row.get("released_at", 0) else "pending"


def trace_event(registry, edge: str, **fields) -> None:
    """Even failure to enqueue instrumentation must leave the caller alone."""
    writer = getattr(registry, "_fire_trace", None)
    if writer is None:
        return
    event = {"edge": edge, "at": time.time(), **fields}
    try:
        writer.submit(event)
    except Exception as exc:
        writer.failed(event, exc)


class ScheduleFireTrace:
    """One bounded FIFO worker owns all trace IO; callers never await it.

    The existing registry retains its TRUNCATE policy. This connection uses
    timeout=0 with bounded BUSY backoff, and never owns the registry's lock.
    Failed edges are logged once and retained in memory until their diagnostic
    record can be persisted by the worker. Read-side results include that memory
    overlay while the database is busy.
    """

    def __init__(self, db_path: str, connection: sqlite3.Connection):
        self.path = db_path
        self._ensure_columns(connection)
        self._queue = queue.Queue(maxsize=1024)
        self._failures: dict[str, dict] = {}
        self._identities = {}
        self._closed = threading.Event()
        self._worker_lock = threading.Lock()
        self._running = False

    @staticmethod
    def _ensure_columns(db):
        # fire_id IS pending_schedule_wakes.id, never an independent sequence.
        # The exact-fire key permits NULL fire_id until the outbox row exists;
        # there is deliberately no FK/cascade into the reaped outbox.
        db.execute("""CREATE TABLE IF NOT EXISTS schedule_fire_trace (
            schedule_id INTEGER NOT NULL, fired_at REAL NOT NULL,
            PRIMARY KEY (schedule_id, fired_at))""")
        existing = {row[1] for row in db.execute("PRAGMA table_info(schedule_fire_trace)")}
        for name, declaration in _COLUMNS.items():
            if name not in existing:
                db.execute(f"ALTER TABLE schedule_fire_trace ADD COLUMN {name} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fire_trace_id ON schedule_fire_trace(fire_id)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_fire_trace_time ON schedule_fire_trace(fired_at)")
        db.execute("""CREATE TABLE IF NOT EXISTS schedule_fire_trace_failures (
            event_id TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL, fired_at REAL NOT NULL,
            fire_id INTEGER, edge TEXT NOT NULL, failed_at REAL NOT NULL,
            failures INTEGER NOT NULL, reason TEXT NOT NULL)""")
        db.commit()

    def submit(self, event):
        if self._closed.is_set():
            raise RuntimeError("trace writer closed")
        event = dict(event)
        if event.get("fire_id"):
            if "schedule_id" in event:
                self._identities[event["fire_id"]] = (event["schedule_id"], event["fired_at"])
            elif event["fire_id"] in self._identities:
                event["schedule_id"], event["fired_at"] = self._identities[event["fire_id"]]
        try:
            self._queue.put_nowait(event)
            with self._worker_lock:
                if not self._running:
                    self._running = True
                    _WORKERS.submit(self._run)
        except Exception:
            raise

    def failed(self, event, error):
        identity = self._identities.get(event.get("fire_id"), (0, 0))
        row = {
            "event_id": uuid.uuid4().hex,
            "schedule_id": event.get("schedule_id", identity[0]),
            "fired_at": event.get("fired_at", identity[1]),
            "fire_id": event.get("fire_id"), "edge": event["edge"],
            "failed_at": event["at"], "failures": 1, "reason": type(error).__name__,
        }
        self._failures[row["event_id"]] = row
        logger.warning("schedule fire trace failed: schedule=%s fire=%s edge=%s (%s)",
                       row["schedule_id"], row["fired_at"], row["edge"], row["reason"])

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA mmap_size=0")
        return db

    def _run(self):
        while True:
            with self._worker_lock:
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    self._running = False
                    return
            started = time.monotonic()
            try:
                self._write_with_retry(event)
                if time.monotonic() - started > 0.5:
                    self.failed(event, TimeoutError("slow trace write"))
            except Exception as exc:
                self.failed(event, exc)
            finally:
                self._persist_failures()
                self._queue.task_done()

    def _write_with_retry(self, event):
        deadline = time.monotonic() + 0.5
        for attempt in range(5):
            try:
                return self._write(event)
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or attempt == 4:
                    raise
                delay = 0.02 * (2 ** attempt)
                if time.monotonic() + delay >= deadline:
                    raise
                # Only the observation worker sleeps. No receipt or paste caller waits.
                time.sleep(delay)

    def _persist_failures(self):
        if not self._failures:
            return
        pending = list(self._failures.items())
        try:
            with self._connect() as db:
                for _, row in pending:
                    db.execute("""INSERT OR IGNORE INTO schedule_fire_trace_failures
                        VALUES (:event_id,:schedule_id,:fired_at,:fire_id,:edge,
                                :failed_at,:failures,:reason)""", row)
                for _, failure in pending:
                    record = db.execute("""SELECT * FROM schedule_fire_trace
                        WHERE (schedule_id=? AND fired_at=?) OR fire_id=?""",
                        (failure["schedule_id"], failure["fired_at"], failure["fire_id"])).fetchone()
                    if record is not None:
                        record = dict(record)
                        record["failed_edges"] = [failure["edge"]]
                        if derive_outcome(record) == "trace_incomplete":
                            db.execute("""UPDATE schedule_fire_trace SET outcome='trace_incomplete'
                                WHERE schedule_id=? AND fired_at=?""",
                                (record["schedule_id"], record["fired_at"]))
            for key, _ in pending:
                self._failures.pop(key, None)
        except sqlite3.Error:
            pass
        finally:
            if "db" in locals():
                db.close()

    @staticmethod
    def _cadence(schedule, fired_at):
        if not schedule or schedule["one_shot"]:
            return 0
        from pinky_daemon.scheduler import cron_matches

        start = datetime.fromtimestamp(fired_at, ZoneInfo(schedule["timezone"])).replace(
            second=0, microsecond=0,
        )
        # Only cadences shorter than 15 minutes affect the lateness rule.
        for minutes in range(1, 16):
            if cron_matches(schedule["cron"], start + timedelta(minutes=minutes)):
                return minutes * 60
        return 900

    def _write(self, event):
        db = self._connect()
        try:
            with db:
                if event["edge"] == "prune":
                    cutoff = event["at"] - max(30, event.get("retention_days", 30)) * 86400
                    db.execute("""DELETE FROM schedule_fire_trace WHERE rowid IN (
                        SELECT rowid FROM schedule_fire_trace WHERE updated_at < ?
                        ORDER BY updated_at LIMIT 500)""", (cutoff,))
                    db.execute("""DELETE FROM schedule_fire_trace_failures WHERE event_id IN (
                        SELECT event_id FROM schedule_fire_trace_failures WHERE failed_at < ?
                        ORDER BY failed_at LIMIT 500)""", (cutoff,))
                    self._identities = {key: value for key, value in self._identities.items()
                                        if value[1] >= cutoff}
                    return
                if event.get("release_agent"):
                    for row in db.execute("""SELECT id, schedule_id, fired_at FROM pending_schedule_wakes
                        WHERE agent_name=? AND released_at=?""",
                        (event["release_agent"], event["at"])).fetchall():
                        self._apply(db, {**event, "fire_id": row["id"],
                                         "schedule_id": row["schedule_id"], "fired_at": row["fired_at"]})
                    return
                self._apply(db, event)
        finally:
            db.close()

    def _apply(self, db, event):
        if "schedule_id" in event and "fired_at" in event:
            key = (event["schedule_id"], event["fired_at"])
            ledger = db.execute("SELECT * FROM pending_schedule_wakes WHERE schedule_id=? AND fired_at=?", key).fetchone()
        else:
            ledger = db.execute("SELECT * FROM pending_schedule_wakes WHERE id=?", (event["fire_id"],)).fetchone()
            identity = ledger or db.execute("SELECT * FROM schedule_fire_trace WHERE fire_id=?", (event["fire_id"],)).fetchone()
            if identity is None:
                raise LookupError("outbox and trace identity unavailable")
            key = (identity["schedule_id"], identity["fired_at"])
        db.execute("INSERT OR IGNORE INTO schedule_fire_trace(schedule_id,fired_at) VALUES (?,?)", key)
        row = dict(db.execute("SELECT * FROM schedule_fire_trace WHERE schedule_id=? AND fired_at=?", key).fetchone())
        at, edge = event["at"], event["edge"]
        row["fire_id"] = row["fire_id"] or event.get("fire_id")
        row["agent_name"] = row["agent_name"] or event.get("agent_name", "")
        row["schedule_name"] = row["schedule_name"] or event.get("schedule_name", "")
        if not row["prompt_hash"] and "prompt" in event:
            row["prompt_hash"] = hashlib.sha256(event["prompt"].encode()).hexdigest()[:12]
        if ledger:
            row["ledger_accepted_at"] = max(row["ledger_accepted_at"], ledger["accepted_at"])
            row["ledger_attempts"] = max(row["ledger_attempts"], ledger["attempts"])
            row["fire_id"] = ledger["id"]
            row["agent_name"] = ledger["agent_name"]
            row["schedule_name"] = ledger["schedule_name"]
            if not row["prompt_hash"]:
                row["prompt_hash"] = hashlib.sha256(ledger["prompt"].encode()).hexdigest()[:12]
            if not row["transport_kind"]:
                agent = db.execute("SELECT runtime,transport FROM agents WHERE name=?", (ledger["agent_name"],)).fetchone()
                row["transport_kind"] = ("tmux_codex" if agent["runtime"] == "codex_cli" else "tmux_claude") if agent and agent["transport"] == "tmux" else "sdk"
            if not row["cadence_seconds"]:
                schedule = db.execute("SELECT cron,timezone,one_shot FROM agent_schedules WHERE id=?", (key[0],)).fetchone()
                row["cadence_seconds"] = self._cadence(schedule, key[1])
        if edge == "enqueue":
            row["enqueued_at"] = row["enqueued_at"] or event.get("enqueued_at", at)
        elif edge == "paste":
            row["paste_at"] = row["paste_at"] or at
            row["paste_pointer"] = row["paste_pointer"] or event.get("pointer", "")
            row["paste_attempts"] += 1
            row["transport_kind"] = event.get("transport_kind", row["transport_kind"])
        elif edge == "observed":
            row["user_message_observed_at"] = row["user_message_observed_at"] or at
            row["user_message_pointer"] = row["user_message_pointer"] or event.get("pointer", "")
        elif edge == "accept":
            result = event.get("result", True)
            if result and not row["matched_at"]:
                row["matched_at"] = at
                row["matched_by"] = event.get("matched_by", "on_accept")
            row["receipt_accept_result"] = max(row["receipt_accept_result"] or 0, int(result))
            row["late_accept_after_abandon"] = int(bool(
                row["abandoned_at"] and row["matched_at"] >= row["abandoned_at"]
            ))
        elif edge == "abandon":
            row["abandoned_at"] = row["abandoned_at"] or at
            row["abandon_reason"] = event.get("reason", "reaper")
            if row["abandon_reason"] == "drain_parked":
                row["drain_parked_at"] = at
            elif row["abandon_reason"] == "released":
                row["released_at"] = at
            else:
                # Preserve park history but a terminal abandonment is no longer parked.
                row["released_at"] = max(row["released_at"], row["drain_parked_at"])
        elif edge == "replay":
            row["replay_count"] += 1
            row["notes"] = event.get("reason", "idle_replay")
        row["failed_edges"] = [r[0] for r in db.execute(
            "SELECT edge FROM schedule_fire_trace_failures WHERE schedule_id=? AND fired_at=?", key)]
        row["outcome"] = derive_outcome(row, dict(ledger) if ledger else {})
        row["outcome_at"] = max(row["enqueued_at"], row["paste_at"], row["user_message_observed_at"],
                                 row["matched_at"], row["abandoned_at"], row["released_at"])
        # Duplicate accepts retain the exact persisted evidence and retention stamp.
        row["updated_at"] = max(row["updated_at"], row["outcome_at"], at if edge == "replay" else 0)
        names = list(_COLUMNS)
        db.execute("UPDATE schedule_fire_trace SET " + ",".join(f"{n}=?" for n in names)
                   + " WHERE schedule_id=? AND fired_at=?", (*[row[n] for n in names], *key))

    def flush(self, timeout=5):
        """Explicit maintenance/test barrier; never used by wake callers."""
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._queue.all_tasks_done.wait(remaining)
        return True

    def failures(self, since=0):
        db = self._connect()
        try:
            records = {r["event_id"]: dict(r)
                       for r in db.execute("SELECT * FROM schedule_fire_trace_failures WHERE failed_at>=?", (since,)) }
        finally:
            db.close()
        for key, row in list(self._failures.items()):
            if row["failed_at"] >= since:
                records[key] = row
        return list(records.values())

    def failure_counts(self, *, since):
        counts = dict.fromkeys(EDGES, 0)
        for row in self.failures(since):
            counts[row["edge"]] = counts.get(row["edge"], 0) + row["failures"]
        return counts

    def report(self, *, since=0, agent=None, schedule_id=None, outcome=None):
        db = self._connect()
        try:
            records = [dict(r) for r in db.execute("""SELECT t.*, w.accepted_at AS current_accepted_at,
                w.attempts AS current_attempts FROM schedule_fire_trace t
                LEFT JOIN pending_schedule_wakes w ON w.schedule_id=t.schedule_id AND w.fired_at=t.fired_at
                WHERE t.fired_at>=? AND (? IS NULL OR t.agent_name=?)
                AND (? IS NULL OR t.schedule_id=?) ORDER BY t.fired_at,t.schedule_id""",
                (since, agent, agent, schedule_id, schedule_id))]
        finally:
            db.close()
        failures = self.failures()
        failed_keys, failed_ids = {}, {}
        for failure in failures:
            failed_keys.setdefault((failure["schedule_id"], failure["fired_at"]), []).append(failure["edge"])
            if failure["fire_id"] is not None:
                failed_ids.setdefault(failure["fire_id"], []).append(failure["edge"])
        for row in records:
            row["failed_edges"] = failed_keys.get((row["schedule_id"], row["fired_at"]), []) + failed_ids.get(row["fire_id"], [])
            row["outcome"] = derive_outcome(row, {"accepted_at": row.pop("current_accepted_at") or 0,
                                                  "attempts": row.pop("current_attempts") or 0})
        records = [r for r in records if outcome is None or r["outcome"] == outcome]
        per_agent, per_schedule = {}, {}
        counts = Counter(r["outcome"] for r in records)
        for row in records:
            for groups, key in ((per_agent, row["agent_name"]), (per_schedule, str(row["schedule_id"]))):
                groups.setdefault(key, dict.fromkeys(OUTCOMES, 0))[row["outcome"]] += 1
        return {"rows": records, "counts": {k: counts[k] for k in OUTCOMES},
                "per_agent": per_agent, "per_schedule": per_schedule}

    def close(self):
        self._closed.set()
        self.flush(timeout=0.5)
        self._persist_failures()
