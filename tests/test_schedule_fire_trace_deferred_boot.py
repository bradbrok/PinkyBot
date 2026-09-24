"""Regression tests for fire-trace recovery under SQLite contention."""
import asyncio
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import pinky_daemon.schedule_fire_trace as ft
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import _derive_api_store_manifest, create_api
from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie


def test_real_api_boot_with_locked_trace(tmp_path, monkeypatch):
    base = str(tmp_path / 'api.db')
    path = _derive_api_store_manifest(base)['agents'].path
    seed = AgentRegistry(path)
    trace_path = seed._fire_trace.path
    seed.close()
    child = subprocess.Popen(
        [sys.executable, '-u', '-c',
         "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute('BEGIN EXCLUSIVE'); print('locked',flush=True); "
         "sys.stdin.readline(); c.rollback(); c.close()", trace_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    app = None
    try:
        assert child.stdout.readline().strip() == 'locked'
        monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .04)
        monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', 60)
        started = time.monotonic()
        try:
            app = create_api(db_path=base)
        finally:
            elapsed = time.monotonic() - started
            print('REAL_BOOT_SECONDS', elapsed)
            assert elapsed < ft.SETUP_TIMEOUT_SECONDS + 2.0, (
                'telemetry preflight ignored its injected bounded budget'
            )
        assert app.state.agents._fire_trace.status()['state'] == 'degraded'
        preflight = app.state.storage_observability.snapshot()['preflight']
        assert preflight['schedule_fire_trace'] == 'deferred-busy'
        assert preflight['schedule_fire_trace_read'] == 'deferred-busy'
        writer = app.state.agents._fire_trace
        order = []
        verify = app.state.store_catalog.verify_deferred_preflight
        connect = writer._connect
        def tracked_verify(logical_name, *, timeout):
            order.append('quick-check')
            return verify(logical_name, timeout=timeout)
        def tracked_connect(**kwargs):
            order.append('write-open')
            return connect(**kwargs)
        monkeypatch.setattr(app.state.store_catalog, 'verify_deferred_preflight', tracked_verify)
        monkeypatch.setattr(writer, '_connect', tracked_connect)
        with writer._worker_lock:
            writer._retry_timer.cancel()
            writer._retry_timer = None
        monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', .02)
        writer._schedule_failure_retry()
        child.communicate('\n', timeout=5)
        deadline = time.monotonic() + 2
        while writer.status()['state'] != 'recovered' and time.monotonic() < deadline:
            threading.Event().wait(.01)
        assert writer.status()['state'] == 'recovered'
        assert order[:2] == ['quick-check', 'write-open']
        assert app.state.storage_observability.snapshot()['preflight']['schedule_fire_trace'] == (
            'deferred-busy-verified'
        )
    finally:
        if child.poll() is None:
            child.communicate('\n', timeout=5)
        if app:
            app.state.agents.close()
            app.state.store_catalog.shutdown(deadline_seconds=2)


async def test_api_shutdown_stops_degraded_recovery(tmp_path, monkeypatch):
    def unavailable(db):
        raise sqlite3.OperationalError('synthetic telemetry outage')
    monkeypatch.setattr(ft.ScheduleFireTrace, '_ensure_columns', staticmethod(unavailable))
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', .03)
    monkeypatch.setattr(ft, 'SETUP_RETRY_MAX_SECONDS', .06)
    app = create_api(db_path=str(tmp_path / 'api.db'))
    writer = app.state.agents._fire_trace
    try:
        assert writer.status()['state'] == 'degraded'
        for callback in app.router.on_shutdown:
            await callback()
        attempts = writer.status()['setup_attempts']
        await asyncio.sleep(.25)
        print('SHUTDOWN_STATE', writer.status(), 'attempts_at_shutdown', attempts,
              'closed', writer._closed.is_set(), 'timer', writer._retry_timer)
        assert writer.status()['setup_attempts'] == attempts
        assert writer._closed.is_set()
        assert writer._retry_timer is None
    finally:
        writer.close()


def test_recovery_never_blocks_accept_and_drop_counter_is_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / 'registry.db')
    seed = AgentRegistry(path)
    seed.close()
    lock = sqlite3.connect(ft.ScheduleFireTrace.path_for(path))
    lock.execute('BEGIN EXCLUSIVE')
    monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .03)
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', 60)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    entered, release = threading.Event(), threading.Event()
    original = writer._connect
    def held(**kwargs):
        if kwargs.get('setup'):
            entered.set()
            assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(writer, '_connect', held)
    try:
        writer._kick()
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: writer.submit({'edge':'replay','at':100}), range(2000)))
        assert writer.status()['dropped_events'] == 2000
        registry.register('worker', working_dir=str(tmp_path/'worker'))
        schedule = registry.add_schedule('worker', '* * * * *', name='wake', prompt='wake')
        started = time.monotonic()
        pending, _ = registry.persist_schedule_wake(
            schedule.id, agent_name='worker', schedule_name='wake', prompt='wake', fired_at=100)
        assert registry.confirm_pending_schedule_wake(pending.id)
        assert time.monotonic()-started < .5
        assert registry.get_schedule_wake_by_fire(schedule.id,100).accepted_at > 0
        assert writer.status()['dropped_events'] == 2002
        assert writer.status()['setup_attempts'] == 2
        assert writer._queue.empty()
    finally:
        release.set()
        lock.rollback()
        lock.close()
        assert writer.flush()
        registry.close()


async def test_watchdog_uses_no_trace_database(tmp_path, monkeypatch):
    app = create_api(db_path=str(tmp_path/'api.db'))
    writer = app.state.agents._fire_trace
    def forbidden(**kwargs):
        pytest.fail('watchdog opened the trace database')
    monkeypatch.setattr(writer, '_connect', forbidden)
    try:
        with writer._worker_lock:
            writer._setup_state = 'degraded'
            writer._dropped_events = 123
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test',
            cookies={SESSION_COOKIE_NAME:create_session_cookie(os.environ['PINKY_SESSION_SECRET'])}) as client:
            response = await client.get('/admin/watchdog')
        assert response.status_code == 200
        assert response.json()['fire_trace']['dropped_events'] == 123
        assert response.json()['fire_trace']['state'] == 'degraded'
    finally:
        app.state.agents.close()
        app.state.store_catalog.shutdown(deadline_seconds=2)


def test_recovery_timer_firing_before_worker_finally_is_not_lost(tmp_path, monkeypatch):
    path = str(tmp_path/'registry.db')
    seed = AgentRegistry(path)
    seed.close()
    lock = sqlite3.connect(ft.ScheduleFireTrace.path_for(path))
    lock.execute('BEGIN EXCLUSIVE')
    monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .02)
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', 60)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    callback_done = threading.Event()
    original_schedule, original_kick = writer._schedule_failure_retry, writer._kick
    with writer._worker_lock:
        writer._retry_timer.cancel()
        writer._retry_timer = None
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', .02)
    def kick():
        original_kick()
        if isinstance(threading.current_thread(), threading.Timer):
            callback_done.set()
    def schedule():
        original_schedule()
        # Allow a legal scheduler interleaving: ready() runs after the retry
        # scheduler releases its lock, but before _run reaches its finally.
        assert callback_done.wait(5)
    monkeypatch.setattr(writer, '_kick', kick)
    monkeypatch.setattr(writer, '_schedule_failure_retry', schedule)
    try:
        writer._kick()
        assert callback_done.wait(5)
        assert writer.flush()
        print('LOST_RETRY_STATE', writer.status(), 'timer', writer._retry_timer,
              'running',writer._running)
        lock.rollback()
        deadline = time.monotonic()+.5
        while writer.status()['state']=='degraded' and time.monotonic()<deadline:
            time.sleep(.01)
        assert writer.status()['state']=='recovered', 'timer fired while _running; recovery stopped'
    finally:
        lock.rollback()
        lock.close()
        registry.close()


def test_setup_failure_logging_cannot_abort_registry(tmp_path, monkeypatch):
    def unavailable(db):
        raise sqlite3.OperationalError('synthetic telemetry outage')
    monkeypatch.setattr(ft.ScheduleFireTrace, '_ensure_columns', staticmethod(unavailable))
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', 60)
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError('synthetic log sink failure')
    handler = BrokenHandler()
    old_level = ft.logger.level
    ft.logger.setLevel(logging.INFO)
    ft.logger.addHandler(handler)
    registry = None
    try:
        registry = AgentRegistry(str(tmp_path/'registry.db'))
        assert registry._fire_trace.status()['state']=='degraded'
    finally:
        ft.logger.removeHandler(handler)
        ft.logger.setLevel(old_level)
        if registry:
            registry.close()


def test_recovery_logging_failure_is_counted_and_trace_still_persists(tmp_path, monkeypatch):
    path = str(tmp_path / 'registry.db')
    seed = AgentRegistry(path)
    seed.close()
    lock = sqlite3.connect(ft.ScheduleFireTrace.path_for(path))
    lock.execute('BEGIN EXCLUSIVE')
    monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .03)
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', .02)
    monkeypatch.setattr(ft, 'SETUP_RETRY_MAX_SECONDS', .04)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    class BrokenHandler(logging.Handler):
        def emit(self, record):
            raise OSError('synthetic recovery log sink failure')
    handler = BrokenHandler()
    old_level = ft.logger.level
    ft.logger.setLevel(logging.INFO)
    ft.logger.addHandler(handler)
    try:
        lock.rollback()
        lock.close()
        deadline = time.monotonic() + 2
        while writer.status()['state'] != 'recovered' and time.monotonic() < deadline:
            threading.Event().wait(.01)
        assert writer.status()['state'] == 'recovered'
        assert writer.status()['logging_failures'] == 1
        writer.submit({'edge': 'replay', 'schedule_id': 99, 'fired_at': 100, 'at': 101})
        assert writer.flush()
        assert writer.report()['rows'][0]['schedule_id'] == 99
    finally:
        ft.logger.removeHandler(handler)
        ft.logger.setLevel(old_level)
        lock.close()
        registry.close()


def test_authoritative_lock_and_corrupt_telemetry_still_fail_closed(tmp_path):
    from pinky_daemon.store_catalog import (
        DaemonStoreCatalog,
        StoreCatalogError,
        StoreIntegrityTarget,
    )

    path = str(tmp_path / 'same.db')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state(value)')
    telemetry = StoreIntegrityTarget('schedule_fire_trace', path, criticality='telemetry')
    authority = StoreIntegrityTarget('authority', path, criticality='authoritative')
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest={'trace': telemetry,
                                                                    'authority': authority})
    lock = sqlite3.connect(path)
    lock.execute('BEGIN EXCLUSIVE')
    try:
        with pytest.raises(StoreCatalogError):
            catalog.preflight_integrity([telemetry], telemetry_busy_timeout=.02)
    finally:
        lock.rollback()
        lock.close()
        catalog.close()


def test_preflight_refuses_error_text_without_sqlite_busy_code(tmp_path, monkeypatch):
    from pinky_daemon.store_catalog import (
        BoundSQLiteFile,
        DaemonStoreCatalog,
        StoreCatalogError,
        StoreIntegrityTarget,
    )

    path = tmp_path / 'error-code.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state(value)')
    target = StoreIntegrityTarget('schedule_fire_trace', str(path), criticality='telemetry')
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest={'trace': target})
    original = BoundSQLiteFile.connect_read_only
    def no_error_code(self, *, timeout=5):
        if self.path == str(path):
            raise sqlite3.OperationalError('database is locked')
        return original(self, timeout=timeout)
    monkeypatch.setattr(BoundSQLiteFile, 'connect_read_only', no_error_code)
    try:
        with pytest.raises(StoreCatalogError):
            catalog.preflight_integrity([target], telemetry_busy_timeout=.01)
    finally:
        catalog.close()


def test_preflight_does_not_defer_other_telemetry_without_a_recovery_owner(
    tmp_path, monkeypatch
):
    from pinky_daemon.store_catalog import (
        DaemonStoreCatalog,
        StoreCatalogError,
        StoreIntegrityTarget,
    )

    monkeypatch.setattr('pinky_daemon.store_catalog._SQLITE_DEFAULT_TIMEOUT_SECONDS', .01)
    path = tmp_path / 'analytics.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE state(value)')
    target = StoreIntegrityTarget('analytics', str(path), criticality='telemetry')
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest={'analytics': target})
    lock = sqlite3.connect(path)
    lock.execute('BEGIN EXCLUSIVE')
    try:
        with pytest.raises(StoreCatalogError):
            catalog.preflight_integrity([target], telemetry_busy_timeout=.01)
    finally:
        lock.rollback()
        lock.close()
        catalog.close()

    corrupt = tmp_path / 'corrupt.db'
    corrupt.write_bytes(b'not a sqlite database')
    target = StoreIntegrityTarget('trace', str(corrupt), criticality='telemetry')
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest={'trace': target})
    try:
        with pytest.raises(StoreCatalogError):
            catalog.preflight_integrity([target], telemetry_busy_timeout=.02)
    finally:
        catalog.close()


def test_real_process_exclusive_lock_on_authority_still_aborts_api_boot(tmp_path, monkeypatch):
    from pinky_daemon.api import _derive_api_store_manifest
    from pinky_daemon.store_catalog import StoreCatalogError

    base = str(tmp_path / 'api.db')
    agents_path = _derive_api_store_manifest(base)['agents'].path
    seed = AgentRegistry(agents_path)
    seed.close()
    child = subprocess.Popen(
        [sys.executable, '-u', '-c',
         "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
         "c.execute('BEGIN EXCLUSIVE'); print('locked',flush=True); "
         "sys.stdin.readline(); c.rollback(); c.close()", agents_path],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == 'locked'
        monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .03)
        with pytest.raises(StoreCatalogError):
            create_api(db_path=base)
    finally:
        if child.poll() is None:
            child.communicate('\n', timeout=5)


def test_deferred_telemetry_corruption_is_checked_before_any_write(tmp_path, monkeypatch):
    from pinky_daemon.api import _derive_api_store_manifest
    from pinky_daemon.storage_observability import StorageObservability
    from pinky_daemon.store_catalog import DaemonStoreCatalog

    base = str(tmp_path / 'api.db')
    manifest = _derive_api_store_manifest(base)
    target = manifest['schedule_fire_trace']
    seed = AgentRegistry(manifest['agents'].path)
    trace_path = seed._fire_trace.path
    seed.close()
    # Keep this test at the catalog boundary so it can corrupt exactly after
    # the lock-only defer and before ScheduleFireTrace's first open.
    catalog = DaemonStoreCatalog(expected_root=tmp_path, manifest=manifest)
    observations = StorageObservability(manifest)
    catalog.configure_observability(observations)
    lock = sqlite3.connect(trace_path)
    lock.execute('BEGIN EXCLUSIVE')
    try:
        catalog.preflight_integrity([target], on_outcome=observations.record_preflight,
                                    telemetry_busy_timeout=.02)
        assert observations.snapshot()['preflight']['schedule_fire_trace'] == 'deferred-busy'
    finally:
        lock.rollback()
        lock.close()
    with open(trace_path, 'r+b') as db_file:
        db_file.seek(0)
        db_file.write(b'not sqlite')
    opens = []
    original = ft.ScheduleFireTrace._connect
    def tracked_connect(self, **kwargs):
        opens.append(kwargs)
        return original(self, **kwargs)
    monkeypatch.setattr(ft.ScheduleFireTrace, '_connect', tracked_connect)
    writer = ft.ScheduleFireTrace(manifest['agents'].path, catalog=catalog)
    try:
        assert writer.status()['state'] == 'degraded'
        assert writer._setup() is False
        assert opens == [], 'deferred quick_check must precede the first trace write-capable open'
    finally:
        writer.close()
        catalog.close()


def test_close_during_recovery_setup_is_bounded_and_prevents_another_retry(
    tmp_path, monkeypatch
):
    path = str(tmp_path / 'registry.db')
    calls = 0
    setup_entered, release = threading.Event(), threading.Event()
    real_ensure = ft.ScheduleFireTrace._ensure_columns

    def block_second_setup(db):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError('initial telemetry outage')
        setup_entered.set()
        assert release.wait(2)
        return real_ensure(db)

    monkeypatch.setattr(ft.ScheduleFireTrace, '_ensure_columns', staticmethod(block_second_setup))
    monkeypatch.setattr(ft, 'SETUP_TIMEOUT_SECONDS', .04)
    monkeypatch.setattr(ft, 'SETUP_RETRY_SECONDS', .02)
    monkeypatch.setattr(ft, 'SETUP_RETRY_MAX_SECONDS', .04)
    registry = AgentRegistry(path)
    writer = registry._fire_trace
    try:
        assert setup_entered.wait(2)
        started = time.monotonic()
        writer.close()
        assert time.monotonic() - started < 1
        assert writer._closed.is_set()
        assert writer._retry_timer is None
    finally:
        release.set()
        assert writer.flush(timeout=2)
        attempts = writer.status()['setup_attempts']
        time.sleep(.08)
        assert writer.status()['setup_attempts'] == attempts
        registry._db.close()
