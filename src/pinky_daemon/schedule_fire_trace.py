"""Best-effort scheduler observations, isolated from authoritative delivery IO."""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pinky_daemon.store_catalog import open_store_connection

logger = logging.getLogger(__name__)
_WORKERS = ThreadPoolExecutor(max_workers=1, thread_name_prefix="schedule-fire-trace")
OUTCOMES = (
    "delivered",
    "late_delivered",
    "observer_unmatched",
    "producer_no_user_turn",
    "never_pasted",
    "drain_parked",
    "trace_incomplete",
    "pending",
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
    "ledger_released_at": "REAL NOT NULL DEFAULT 0",
    "ledger_abandoned_at": "REAL NOT NULL DEFAULT 0",
    "terminal_abandoned_at": "REAL NOT NULL DEFAULT 0",
}


def derive_outcome(row: dict, ledger: dict | None = None) -> str:
    """Re-derive the diagnostic class without granting receipt authority."""
    ledger = dict(ledger or {})
    ledger["accepted_at"] = max(ledger.get("accepted_at", 0), row.get("ledger_accepted_at", 0))
    ledger["attempts"] = max(ledger.get("attempts", 0), row.get("ledger_attempts", 0))
    if max(ledger.get("released_at", 0), row.get("ledger_released_at", 0)) > row.get(
        "released_at", 0
    ) or max(ledger.get("abandoned_at", 0), row.get("ledger_abandoned_at", 0)) > row.get(
        "terminal_abandoned_at", 0
    ):
        return "trace_incomplete"
    evidence = {
        "enqueue": "enqueued_at",
        "paste": "paste_at",
        "observed": "user_message_observed_at",
        "accept": "matched_at",
        "abandon": "abandoned_at",
    }
    if any(
        not row.get(evidence[edge], 0) for edge in row.get("failed_edges", ()) if edge in evidence
    ):
        return "trace_incomplete"
    if (ledger.get("accepted_at", 0) and not row.get("matched_at", 0)) or (
        ledger.get("attempts", 0) and not row.get("paste_at", 0)
    ):
        return "trace_incomplete"
    if row.get("matched_at", 0) and row.get("receipt_accept_result") == 1:
        age = row["matched_at"] - row["fired_at"]
        cadence = row.get("cadence_seconds", 0)
        late = (
            age > 900
            or (cadence > 0 and age > cadence)
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
        try:
            writer.failed(event, exc)
        except Exception:
            pass


class ScheduleFireTrace:
    """One bounded FIFO worker owns all trace IO; callers never await it.

    Trace writes own a separate TRUNCATE database. Ledger access is read-only;
    all ledger reads finish before a trace write transaction starts. Workers use
    zero busy timeout with bounded backoff, while API readers run off the loop.
    Bounded memory retains failed edges until background persistence succeeds;
    overflow keeps per-edge daily counts and conservative diagnostic uncertainty.
    """

    DETAIL_LIMIT = 1024
    FAILURE_BATCH = 100
    DRAIN_QUANTUM = 50

    @staticmethod
    def path_for(registry_db_path):
        return str(Path(registry_db_path).resolve()) + ".fire-trace.db"

    def __init__(self, db_path: str, connection=None, *, catalog=None):
        self.registry_path = str(Path(db_path).resolve())
        self.path = self.path_for(db_path)
        self._catalog = catalog
        db = self._connect()
        try:
            self._ensure_columns(db)
        finally:
            db.close()
        if catalog is not None:
            for name in ("schedule_fire_trace", "schedule_fire_trace_read"):
                catalog.register(
                    name,
                    self.path,
                    journal_mode="truncate",
                    owner="schedule_fire_trace",
                    criticality="telemetry",
                )
        self._queue = queue.Queue(maxsize=1024)
        self._failures: dict[str, dict] = {}
        self._overflow: dict[tuple[int, str], dict] = {}
        self._overflow_persisted: dict[str, int] = {}
        self._failure_lock = threading.RLock()
        self._identities = {}
        self._closed = threading.Event()
        self._worker_lock = threading.RLock()
        self._running = False
        self._retry_timer = None
        self._retry_attempts = 0

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
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_fire_trace_id ON schedule_fire_trace(fire_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_trace_time ON schedule_fire_trace(fired_at)"
        )
        db.execute("""CREATE TABLE IF NOT EXISTS schedule_fire_trace_failures (
            event_id TEXT PRIMARY KEY, schedule_id INTEGER NOT NULL, fired_at REAL NOT NULL,
            fire_id INTEGER, edge TEXT NOT NULL, failed_at REAL NOT NULL,
            failures INTEGER NOT NULL, reason TEXT NOT NULL)""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_trace_updated ON schedule_fire_trace(updated_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_failure_time ON schedule_fire_trace_failures(failed_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_failure_identity ON schedule_fire_trace_failures(schedule_id,fired_at)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_failure_id ON schedule_fire_trace_failures(fire_id)"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_fire_failure_overflow ON schedule_fire_trace_failures(reason,failed_at)"
        )
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
        self._queue.put_nowait(event)
        self._kick()

    def _kick(self):
        with self._worker_lock:
            if not self._running and not self._closed.is_set():
                self._running = True
                try:
                    _WORKERS.submit(self._run)
                except Exception:
                    self._running = False
                    raise

    def failed(self, event, error):
        # No diagnostic sink or SQLite operation runs on a delivery caller.
        try:
            identity = self._identities.get(event.get("fire_id"), (0, 0))
            row = {
                "event_id": uuid.uuid4().hex,
                "schedule_id": event.get("schedule_id", identity[0]),
                "fired_at": event.get("fired_at", identity[1]),
                "fire_id": event.get("fire_id"),
                "edge": event["edge"],
                "failed_at": event["at"],
                "failures": 1,
                "reason": type(error).__name__,
                "logged": False,
            }
            with self._failure_lock:
                if len(self._failures) < self.DETAIL_LIMIT:
                    self._failures[row["event_id"]] = row
                else:
                    day = int(row["failed_at"] // 86400)
                    key = (day, row["edge"])
                    if key not in self._overflow:
                        self._overflow[key] = {
                            **row,
                            "schedule_id": 0,
                            "fired_at": 0,
                            "fire_id": None,
                            "reason": "overflow",
                            "failures": 0,
                        }
                    self._overflow[key]["failures"] += 1
                    self._overflow[key]["failed_at"] = max(
                        self._overflow[key]["failed_at"], row["failed_at"]
                    )
                    # At most 32 daily buckets per edge; older buckets are beyond retention.
                    for old in list(self._overflow):
                        if old[0] < day - 31:
                            evicted = self._overflow.pop(old)
                            self._overflow_persisted.pop(evicted["event_id"], None)
            self._schedule_failure_retry()
        except Exception:
            pass

    def _schedule_failure_retry(self):
        with self._worker_lock:
            if self._closed.is_set() or self._retry_timer is not None or self._retry_attempts >= 10:
                return
            delay = min(0.05 * 2**self._retry_attempts, 1.0)
            self._retry_attempts += 1

            def ready():
                with self._worker_lock:
                    self._retry_timer = None
                try:
                    self._kick()
                except Exception:
                    pass

            self._retry_timer = threading.Timer(delay, ready)
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _connect(self, *, timeout=0):
        name = "schedule_fire_trace_read" if timeout else "schedule_fire_trace"
        db = open_store_connection(
            self._catalog, name, self.path, owner="schedule_fire_trace", timeout=timeout, uri=True
        )
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA mmap_size=0")
            db.execute("PRAGMA journal_mode=TRUNCATE")
            db.execute(
                "ATTACH DATABASE ? AS ledger", (Path(self.registry_path).as_uri() + "?mode=ro",)
            )
            return db
        except BaseException:
            db.close()
            raise

    def _run(self):
        slice_started = time.monotonic()
        try:
            for _ in range(self.DRAIN_QUANTUM):
                if time.monotonic() - slice_started >= 0.1:
                    break
                try:
                    event = self._queue.get_nowait()
                except queue.Empty:
                    break
                started = time.monotonic()
                try:
                    if not self._closed.is_set():
                        self._write_with_retry(event)
                        if time.monotonic() - started > 0.5:
                            self.failed(event, TimeoutError("slow trace write"))
                except Exception as exc:
                    try:
                        self.failed(event, exc)
                    except Exception:
                        pass
                finally:
                    try:
                        self._persist_failures()
                    finally:
                        self._queue.task_done()
            self._persist_failures()
        finally:
            with self._worker_lock:
                self._running = False
                if not self._queue.empty() and not self._closed.is_set():
                    self._kick()

    def _write_with_retry(self, event):
        deadline = time.monotonic() + 0.5
        for attempt in range(5):
            try:
                return self._write(event)
            except sqlite3.OperationalError as exc:
                if getattr(exc, "sqlite_errorcode", None) != sqlite3.SQLITE_BUSY or attempt == 4:
                    raise
                delay = 0.02 * (2**attempt)
                if time.monotonic() + delay >= deadline:
                    raise
                time.sleep(delay)

    def _persist_failures(self):
        with self._failure_lock:
            pending = list(self._failures.values())[: self.FAILURE_BATCH]
            if len(pending) < self.FAILURE_BATCH:
                pending += [
                    row
                    for row in self._overflow.values()
                    if row["failures"] > self._overflow_persisted.get(row["event_id"], 0)
                ][: self.FAILURE_BATCH - len(pending)]
            pending = [dict(row) for row in pending]
            for row in pending:
                original = self._failures.get(row["event_id"])
                if original is None:
                    original = next(
                        (r for r in self._overflow.values() if r["event_id"] == row["event_id"]),
                        None,
                    )
                if original is not None:
                    original["logged"] = True
        if not pending:
            return
        for row in pending:
            if not row["logged"]:
                try:
                    logger.warning(
                        "schedule fire trace failed: schedule=%s fire=%s edge=%s (%s)",
                        row["schedule_id"],
                        row["fired_at"],
                        row["edge"],
                        row["reason"],
                    )
                except Exception:
                    pass
        db = None
        try:
            db = self._connect()
            with db:
                for row in pending:
                    db.execute(
                        """INSERT INTO schedule_fire_trace_failures
                        VALUES (:event_id,:schedule_id,:fired_at,:fire_id,:edge,
                                :failed_at,:failures,:reason)
                        ON CONFLICT(event_id) DO UPDATE SET
                        failures=MAX(failures,excluded.failures),
                        failed_at=MAX(failed_at,excluded.failed_at)""",
                        row,
                    )
            with self._failure_lock:
                for row in pending:
                    self._failures.pop(row["event_id"], None)
                    if row["reason"] == "overflow":
                        self._overflow_persisted[row["event_id"]] = max(
                            row["failures"], self._overflow_persisted.get(row["event_id"], 0)
                        )
                remaining = bool(self._failures) or any(
                    row["failures"] > self._overflow_persisted.get(row["event_id"], 0)
                    for row in self._overflow.values()
                )
            with self._worker_lock:
                self._retry_attempts = 0
            if remaining:
                self._schedule_failure_retry()
        except Exception:
            self._schedule_failure_retry()
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _cadence(schedule, fired_at):
        if not schedule or schedule["one_shot"]:
            return 0
        from pinky_daemon.scheduler import cron_matches

        start = datetime.fromtimestamp(fired_at, ZoneInfo(schedule["timezone"])).replace(
            second=0,
            microsecond=0,
        )
        # Only cadences shorter than 15 minutes affect the lateness rule.
        for minutes in range(1, 16):
            if cron_matches(schedule["cron"], start + timedelta(minutes=minutes)):
                return minutes * 60
        return 900

    def _write(self, event):
        db = self._connect()
        try:
            if event["edge"] == "prune":
                cutoff = event["at"] - max(30, event.get("retention_days", 30)) * 86400
                deadline = time.monotonic() + 2
                while True:
                    with db:
                        traces = db.execute(
                            """DELETE FROM schedule_fire_trace WHERE rowid IN (
                            SELECT rowid FROM schedule_fire_trace WHERE updated_at < ?
                            ORDER BY updated_at LIMIT 500)""",
                            (cutoff,),
                        ).rowcount
                        failures = db.execute(
                            """DELETE FROM schedule_fire_trace_failures WHERE event_id IN (
                            SELECT event_id FROM schedule_fire_trace_failures WHERE failed_at < ?
                            ORDER BY failed_at LIMIT 500)""",
                            (cutoff,),
                        ).rowcount
                    if max(traces, failures) < 500:
                        break
                    if time.monotonic() >= deadline:
                        self.submit(event)
                        break
                    time.sleep(0)
                self._identities = {
                    key: value for key, value in self._identities.items() if value[1] >= cutoff
                }
                return
            self._apply(db, event)
        finally:
            db.close()

    def _apply(self, db, event):
        if "schedule_id" in event and "fired_at" in event:
            key = (event["schedule_id"], event["fired_at"])
            ledger = db.execute(
                "SELECT * FROM ledger.pending_schedule_wakes WHERE schedule_id=? AND fired_at=?",
                key,
            ).fetchone()
        else:
            ledger = db.execute(
                "SELECT * FROM ledger.pending_schedule_wakes WHERE id=?", (event["fire_id"],)
            ).fetchone()
            identity = (
                ledger
                or db.execute(
                    "SELECT * FROM schedule_fire_trace WHERE fire_id=?", (event["fire_id"],)
                ).fetchone()
            )
            if identity is None:
                raise LookupError("outbox and trace identity unavailable")
            key = (identity["schedule_id"], identity["fired_at"])
        existing = db.execute(
            "SELECT * FROM schedule_fire_trace WHERE schedule_id=? AND fired_at=?", key
        ).fetchone()
        row = (
            dict(existing)
            if existing
            else {
                name: (
                    None
                    if name in {"fire_id", "receipt_accept_result"}
                    else ""
                    if declaration.startswith("TEXT")
                    else 0
                )
                for name, declaration in _COLUMNS.items()
            }
        )
        row.update(schedule_id=key[0], fired_at=key[1])
        at, edge = event["at"], event["edge"]
        row["fire_id"] = row["fire_id"] or event.get("fire_id")
        row["agent_name"] = row["agent_name"] or event.get("agent_name", "")
        row["schedule_name"] = row["schedule_name"] or event.get("schedule_name", "")
        if not row["prompt_hash"] and "prompt" in event:
            row["prompt_hash"] = hashlib.sha256(event["prompt"].encode()).hexdigest()[:12]
        if ledger:
            row["ledger_accepted_at"] = max(row["ledger_accepted_at"], ledger["accepted_at"])
            row["ledger_attempts"] = max(row["ledger_attempts"], ledger["attempts"])
            row["ledger_released_at"] = max(row["ledger_released_at"], ledger["released_at"])
            row["ledger_abandoned_at"] = max(row["ledger_abandoned_at"], ledger["abandoned_at"])
            row["fire_id"] = ledger["id"]
            row["agent_name"] = ledger["agent_name"]
            row["schedule_name"] = ledger["schedule_name"]
            if not row["prompt_hash"]:
                row["prompt_hash"] = hashlib.sha256(ledger["prompt"].encode()).hexdigest()[:12]
            if not row["transport_kind"]:
                agent = db.execute(
                    "SELECT runtime,transport FROM ledger.agents WHERE name=?",
                    (ledger["agent_name"],),
                ).fetchone()
                row["transport_kind"] = (
                    ("tmux_codex" if agent["runtime"] == "codex_cli" else "tmux_claude")
                    if agent and agent["transport"] == "tmux"
                    else "sdk"
                )
            if not row["cadence_seconds"]:
                schedule = db.execute(
                    "SELECT cron,timezone,one_shot FROM ledger.agent_schedules WHERE id=?",
                    (key[0],),
                ).fetchone()
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
            row["late_accept_after_abandon"] = int(
                bool(row["abandoned_at"] and row["matched_at"] >= row["abandoned_at"])
            )
        elif edge == "abandon":
            row["abandoned_at"] = min(row["abandoned_at"] or at, at)
            row["abandon_reason"] = event.get("reason", "reaper")
            if row["abandon_reason"] == "drain_parked":
                row["drain_parked_at"] = max(row["drain_parked_at"], at)
            elif row["abandon_reason"] == "released":
                row["released_at"] = max(row["released_at"], at)
            else:
                row["terminal_abandoned_at"] = max(row["terminal_abandoned_at"], at)
                # Preserve park history but a terminal abandonment is no longer parked.
                row["released_at"] = max(row["released_at"], row["drain_parked_at"])
        elif edge == "replay":
            row["replay_count"] += 1
            row["notes"] = event.get("reason", "idle_replay")
        row["late_accept_after_abandon"] = int(
            bool(row["abandoned_at"] and row["matched_at"] >= row["abandoned_at"])
        )
        row["failed_edges"] = [
            r[0]
            for r in db.execute(
                "SELECT edge FROM schedule_fire_trace_failures WHERE schedule_id=? AND fired_at=?",
                key,
            )
        ]
        row["outcome"] = derive_outcome(row, dict(ledger) if ledger else {})
        row["outcome_at"] = max(
            row["enqueued_at"],
            row["paste_at"],
            row["user_message_observed_at"],
            row["matched_at"],
            row["abandoned_at"],
            row["released_at"],
        )
        # Duplicate accepts retain the exact persisted evidence and retention stamp.
        row["updated_at"] = max(row["updated_at"], row["outcome_at"], at if edge == "replay" else 0)
        names = list(_COLUMNS)
        # Ledger reads and outcome computation finish before the only trace DML.
        with db:
            db.execute(
                "INSERT INTO schedule_fire_trace(schedule_id,fired_at,"
                + ",".join(names)
                + ") VALUES ("
                + ",".join("?" for _ in range(len(names) + 2))
                + ") "
                + "ON CONFLICT(schedule_id,fired_at) DO UPDATE SET "
                + ",".join(f"{name}=excluded.{name}" for name in names),
                (*key, *[row[name] for name in names]),
            )

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
        # Snapshot memory before SQLite: commit/pop between these reads cannot hide a failure.
        with self._failure_lock:
            overlay = [
                dict(row)
                for row in (*self._failures.values(), *self._overflow.values())
                if row["failed_at"] >= since
            ]
        db = self._connect(timeout=1.0)
        try:
            records = {
                r["event_id"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM schedule_fire_trace_failures WHERE failed_at>=?", (since,)
                )
            }
        finally:
            db.close()
        for row in overlay:
            key = row["event_id"]
            if key not in records or row["failures"] > records[key]["failures"]:
                records[key] = row
        return list(records.values())

    def failure_counts(self, *, since):
        counts = dict.fromkeys(EDGES, 0)
        for row in self.failures(since):
            counts[row["edge"]] = counts.get(row["edge"], 0) + row["failures"]
        return counts

    def report(self, *, since=0, agent=None, schedule_id=None, outcome=None, limit=200, offset=0):
        """Return a bounded page, full-window counts, and counts for the page's keys."""
        limit = max(1, min(1000, int(limit)))
        offset = max(0, int(offset))
        with self._failure_lock:
            overlay = [
                dict(row)
                for row in (*self._failures.values(), *self._overflow.values())
                if row["failed_at"] >= since
            ]
        by_key, by_id, overflow = {}, {}, []
        for failure in overlay:
            if failure["reason"] == "overflow":
                overflow.append(failure)
            else:
                by_key.setdefault((failure["schedule_id"], failure["fired_at"]), []).append(
                    failure["edge"]
                )
                if failure["fire_id"] is not None:
                    by_id.setdefault(failure["fire_id"], []).append(failure["edge"])

        def classify(payload):
            row = json.loads(payload)
            row["failed_edges"] = row.pop("failed_edges_sql").split(",")
            row["failed_edges"] += by_key.get((row["schedule_id"], row["fired_at"]), [])
            row["failed_edges"] += by_id.get(row["fire_id"], [])
            row["failed_edges"] += [
                f["edge"] for f in overflow if f["failed_at"] >= row["fired_at"]
            ]
            ledger = {
                name: row.pop("current_" + name) or 0
                for name in ("accepted_at", "attempts", "released_at", "abandoned_at")
            }
            return derive_outcome(row, ledger)

        # Bound values remain parameters; only static schema names build this SQL.
        json_fields = ",".join(
            f"'{name}',t.{name}" for name in ("schedule_id", "fired_at", *_COLUMNS)
        )
        ledger_fields = ",".join(
            f"'current_{name}',w.{name}"
            for name in ("accepted_at", "attempts", "released_at", "abandoned_at")
        )
        cte = (
            """WITH observed AS (
            SELECT t.*, json_object("""
            + json_fields
            + ","
            + ledger_fields
            + """,
                'failed_edges_sql',COALESCE((SELECT group_concat(f.edge) FROM schedule_fire_trace_failures f
                    WHERE f.failed_at>=? AND ((f.schedule_id=t.schedule_id AND f.fired_at=t.fired_at)
                    OR f.fire_id=t.fire_id OR (f.reason='overflow' AND f.failed_at>=t.fired_at))),'')) AS evidence
            FROM schedule_fire_trace t LEFT JOIN ledger.pending_schedule_wakes w
                ON w.schedule_id=t.schedule_id AND w.fired_at=t.fired_at
            WHERE t.fired_at>=? AND (? IS NULL OR t.agent_name=?)
                AND (? IS NULL OR t.schedule_id=?)
        ), classified AS MATERIALIZED (
            SELECT *, trace_outcome(evidence) AS derived_outcome FROM observed
        ), filtered AS (
            SELECT * FROM classified WHERE (? IS NULL OR derived_outcome=?)
        ) """
        )
        parameters = (since, since, agent, agent, schedule_id, schedule_id, outcome, outcome)
        db = self._connect(timeout=1.0)
        try:
            db.create_function("trace_outcome", 1, classify)
            records = [
                dict(row)
                for row in db.execute(
                    cte + "SELECT * FROM filtered ORDER BY fired_at,schedule_id LIMIT ? OFFSET ?",
                    (*parameters, limit, offset),
                )
            ]
            counts = dict.fromkeys(OUTCOMES, 0)
            for name, count in db.execute(
                cte + "SELECT derived_outcome,COUNT(*) FROM filtered GROUP BY derived_outcome",
                parameters,
            ):
                counts[name] = count
            groups = {}
            for label, field in (("per_agent", "agent_name"), ("per_schedule", "schedule_id")):
                keys = sorted({row[field] for row in records})
                group = {}
                if keys:
                    query = (
                        cte
                        + f"SELECT {field},derived_outcome,COUNT(*) FROM filtered WHERE {field} IN ("
                        + ",".join("?" for _ in keys)
                        + f") GROUP BY {field},derived_outcome"
                    )
                    for key, name, count in db.execute(query, (*parameters, *keys)):
                        group.setdefault(str(key), dict.fromkeys(OUTCOMES, 0))[name] = count
                groups[label] = group
        finally:
            db.close()
        for row in records:
            row.pop("evidence")
            row["outcome"] = row.pop("derived_outcome")
        total = sum(counts.values())
        return {
            "rows": records,
            "counts": counts,
            **groups,
            "limit": limit,
            "offset": offset,
            "total": total,
            "next_offset": offset + len(records) if offset + len(records) < total else None,
            "counts_scope": "window",
            "group_counts_scope": "page_keys",
        }

    def close(self):
        """Cancel queued observations; an already executing bounded slice may finish.

        Failure records have a documented crash window until their asynchronous
        persistence succeeds. Closing never waits for a diagnostic database lock.
        """
        self._closed.set()
        with self._worker_lock:
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            else:
                self._queue.task_done()
        self.flush(timeout=0.5)
