"""Offline update checks using real temporary checkouts and target children."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import api, self_update


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    tree = tmp_path / 'tree'
    tree.mkdir()
    (tree / '.gitignore').write_text('__pycache__/\n*.pyc\n')
    shutil.copytree(Path(api.__file__).resolve().parents[1], tree / 'src',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (tree / 'tracked.txt').write_text('original\n')
    (tree / 'local.txt').write_text('original local\n')
    (tree / 'src/sitecustomize.py').write_text(
        "import sys, pinky_daemon\n"
        "print('TREE_IMPORT=' + pinky_daemon.__file__, file=sys.stderr)\n"
    )
    original_output = subprocess.check_output
    original_run = subprocess.run
    original_child = api._run_startup_child

    def git(*args):
        return original_output(['git', *args], cwd=tree, stderr=subprocess.PIPE).decode().strip()

    git('init', '-b', 'main')
    git('config', 'user.name', 'Fixture Author')
    git('config', 'user.email', 'fixture@example.invalid')
    git('add', '.')
    git('commit', '-m', 'Initial fixture')
    previous = git('rev-parse', 'HEAD')
    origin = tmp_path / 'origin.git'
    original_output(['git', 'init', '--bare', str(origin)], cwd=tmp_path, stderr=subprocess.STDOUT)
    git('remote', 'add', 'origin', str(origin))
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    monkeypatch.chdir(tree)
    monkeypatch.setattr(api, '__file__', str(tree / 'src/pinky_daemon/api.py'))
    monkeypatch.setattr(api, '_check_installed_deps_drift', lambda repo: [])
    app = api.create_api(db_path=str(data / 'conversations.db'), default_working_dir=str(tmp_path))
    fx = SimpleNamespace(tree=tree, data=data, previous=previous, target=previous,
                         app=app, git=git, children=[], commands=[], scheduled=[],
                         installs=[], child_timeout=None, fail_install=False, fail_target_install=False,
                         fail_restore=False, fail_undo=False, fail_probe=False,
                         fail_spawn=False, fail_previous=False, fail_confirmation=False)
    owner = AsyncMock(return_value=True)
    app.state.scheduler._owner_notify_callback = owner
    fx.owner = owner
    fx.update = next(r.endpoint for r in app.routes if r.path == '/admin/update')
    fx.restart = next(r.endpoint for r in app.routes if r.path == '/admin/restart')

    def target(*, report=None, rc=0, raw=None, raw_bytes=None, effect='', supports=True):
        if raw_bytes is not None:
            source = '# check-startup\nimport sys\nsys.stdout.buffer.write(' + repr(raw_bytes) + ')\n'
            (tree / 'src/pinky_daemon/__main__.py').write_text(source)
        elif report is not None or raw is not None:
            output = json.dumps(report) if raw is None else raw
            source = "# check-startup\nimport sys\nprint(" + repr(output) + ")\nsys.exit(" + str(rc) + ")\n"
            (tree / 'src/pinky_daemon/__main__.py').write_text(source)
        elif not supports:
            (tree / 'src/pinky_daemon/__main__.py').write_text('raise SystemExit(0)\n')
        if effect:
            with (tree / 'src/pinky_daemon/api.py').open('a') as out:
                out.write('\n' + effect + '\n')
        (tree / 'tracked.txt').write_text('target\n')
        git('add', '.')
        git('commit', '-m', 'Target fixture')
        assert not any(path.endswith('.pyc') for path in git('ls-files').splitlines())
        fx.target = git('rev-parse', 'HEAD')
        git('push', '--force', 'origin', 'HEAD:main')
        git('checkout', '--detach', previous)
        return fx.target

    fx.set_target = target
    monkeypatch.setattr(self_update, 'resolve_and_verify', lambda *a, **k: self_update.DeployDecision(
        ref=fx.target, kind='commit', verified=True))
    monkeypatch.setattr(self_update, 'latest_release_tag', lambda *a: None)
    real_which = shutil.which
    monkeypatch.setattr(shutil, 'which', lambda cmd: None if cmd == 'npm' else real_which(cmd))

    def output(cmd, **kwargs):
        fx.commands.append(list(cmd))
        if cmd[:2] == ['git', 'show'] and fx.fail_probe:
            raise subprocess.CalledProcessError(1, cmd, output=b'cannot read source')
        if cmd[:3] == ['git', 'checkout', '--detach'] and fx.fail_undo:
            raise subprocess.CalledProcessError(1, cmd, output=b'checkout blocked')
        if cmd == ['git', 'rev-parse', 'HEAD']:
            if fx.fail_previous and not fx.children:
                raise subprocess.CalledProcessError(1, cmd, output=b'unknown')
            if fx.fail_confirmation and fx.children:
                return (fx.target + '\n').encode()
        if 'pip' in cmd and 'install' in cmd:
            fx.installs.append((git('rev-parse', 'HEAD'), list(cmd)))
            if (fx.fail_install or (fx.fail_target_install and len(fx.installs) == 1)
                    or (fx.fail_restore and len(fx.installs) > 1)):
                raise subprocess.CalledProcessError(1, cmd, output=b'fixture install failed')
            return b''
        return original_output(cmd, **kwargs)

    def record_child(runner, cmd, **kwargs):
        if '-m' in cmd and 'pinky_daemon' in cmd:
            entry = {'command': list(cmd), 'kwargs': dict(kwargs)}
            fx.children.append(entry)
            if fx.fail_spawn:
                raise OSError('fixture spawn failure')
            if fx.child_timeout is not None:
                kwargs['timeout'] = fx.child_timeout
            try:
                result = runner(cmd, **kwargs)
            except subprocess.TimeoutExpired as exc:
                entry['stderr'] = exc.stderr
                raise
            entry['stderr'] = result.stderr
            stderr_text = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else result.stderr
            assert 'TREE_IMPORT=' + str(tree / 'src/pinky_daemon/__init__.py') in stderr_text
            return result
        return runner(cmd, **kwargs)

    def run(cmd, **kwargs):
        return record_child(original_run, cmd, **kwargs)

    def run_child(cmd, **kwargs):
        return record_child(original_child, cmd, **kwargs)

    monkeypatch.setattr(subprocess, 'check_output', output)
    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(api, '_run_startup_child', run_child)
    create_task = asyncio.create_task

    def schedule(coro, *args, **kwargs):
        if coro.cr_code.co_name == '_delayed_exit':
            fx.scheduled.append(coro.cr_code.co_name)
            coro.close()
            return None
        return create_task(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_task', schedule)
    yield fx
    app.state.store_catalog.close()
    for catalog in app.state.tenant_store_catalogs.values():
        catalog.close()


def clean_report(data_root, **updates):
    report = dict(ok=True, root=str(data_root), checked=['import', 'layout'], findings=[],
                  warnings_count=0, warnings=[])
    report.update(updates)
    return report


def assert_child(fx):
    assert len(fx.children) == 1
    child = fx.children[0]
    assert child['command'][0] == sys.executable
    assert child['kwargs']['cwd'] == str(fx.tree)
    assert child['kwargs']['env']['PYTHONPATH'] == str(fx.tree / 'src')
    assert child['kwargs']['timeout'] == 600
    assert child['command'][-2:] == ['--db-path', str(fx.data / 'conversations.db')]
    stderr = child['stderr'] or ''
    if isinstance(stderr, bytes):
        stderr = stderr.decode()
    assert 'TREE_IMPORT=' + str(fx.tree / 'src/pinky_daemon/__init__.py') in stderr


@pytest.mark.parametrize('force_deps', [False, True])
async def test_dependency_probe_undo_leaves_source_uncached(deployment, force_deps):
    fx = deployment
    module = fx.tree / 'pip.py'
    safe = "MODE = 'safe'\nif __name__ == '__main__':\n    print('synthetic installer version')\n"
    module.write_text(safe)
    entry = fx.tree / 'src/pinky_daemon/__main__.py'
    guard = "from pip import MODE\nif MODE == 'fail':\n    raise RuntimeError('synthetic target failure')\n"
    entry.write_text(entry.read_text().replace('from __future__ import annotations\n',
                                             'from __future__ import annotations\n' + guard))
    with (fx.tree / '.gitignore').open('a') as stream:
        stream.write('.venv/\n')
    fx.git('add', 'pip.py', 'src/pinky_daemon/__main__.py', '.gitignore')
    fx.git('commit', '-m', 'Safe installer probe fixture')
    previous = fx.git('rev-parse', 'HEAD')
    target = safe.replace("'safe'", "'fail'")
    assert len(target.encode()) == len(safe.encode())
    module.write_text(target)
    fx.git('add', 'pip.py')
    fx.git('commit', '-m', 'Failing installer probe fixture')
    fx.target = fx.git('rev-parse', 'HEAD')
    fx.git('push', '--force', 'origin', 'HEAD:main')
    fx.git('checkout', '--detach', previous)
    executable = fx.tree / '.venv/bin'
    executable.mkdir(parents=True)
    (executable / 'pip').write_text('synthetic installer marker\n')
    (executable / 'python').symlink_to(sys.executable)

    result = await fx.update(force_deps=force_deps)
    cached = sorted(str(path.relative_to(fx.tree)) for path in fx.tree.rglob('pip*.pyc'))
    assert cached == [], 'installer probe must not cache the checkout module'
    assert result['undo']['ok'] and not fx.scheduled
    assert result['undo']['deps_restored'] is (True if force_deps else None)
    assert fx.git('rev-parse', 'HEAD') == previous
    assert module.read_text() == safe
    probes = [cmd for cmd in fx.commands if cmd[-3:] == ['-m', 'pip', '--version']]
    assert len(probes) == int(force_deps)
    assert len(fx.installs) == (2 if force_deps else 0)
    if force_deps:
        assert [ref for ref, _ in fx.installs] == [fx.target, previous]
    env = dict(os.environ, PYTHONPATH=str(fx.tree / 'src'))
    probe = subprocess.run([sys.executable, '-c', 'import pip; print(pip.__file__); print(pip.MODE)'],
                           cwd=fx.tree, env=env, capture_output=True, text=True, timeout=10)
    assert probe.returncode == 0
    assert probe.stdout.splitlines() == [str(module), 'safe']
    fresh = subprocess.run([sys.executable, '-m', 'pinky_daemon', '--mode', 'check-startup',
                            '--db-path', str(fx.data / 'conversations.db')],
                           cwd=fx.tree, env=env, capture_output=True, text=True, timeout=20)
    assert fresh.returncode == 0
    assert json.loads(fresh.stdout)['ok'] is True


@pytest.mark.parametrize('launch', ['version', 'project', 'fallback', 'restore_project', 'restore_fallback'])
async def test_dependency_children_disable_bytecode(deployment, monkeypatch, launch):
    fx = deployment
    fx.set_target(raw='invalid report')
    project = launch not in ('fallback', 'restore_fallback')
    executable = fx.tree / '.venv/bin'
    if project:
        executable.mkdir(parents=True)
        (executable / 'pip').write_text('synthetic installer marker\n')
    monkeypatch.setenv('UPDATE_CHILD_TEST_VALUE', 'preserved value')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '0')
    daemon_env = dict(os.environ)
    expected_env = dict(daemon_env, PYTHONDONTWRITEBYTECODE='1')
    actual_output = subprocess.check_output
    launches = []

    def output(cmd, **kwargs):
        if 'pip' in cmd:
            launches.append((list(cmd), dict(kwargs)))
            if cmd[-1] == '--version':
                return b'synthetic installer version\n'
        return actual_output(cmd, **kwargs)

    monkeypatch.setattr(subprocess, 'check_output', output)
    result = await fx.update(force_deps=True)
    assert result['undo']['ok'] and result['undo']['deps_restored']
    assert not fx.scheduled
    installs = [(cmd, kwargs) for cmd, kwargs in launches if 'install' in cmd]
    assert len(installs) == 2
    if launch == 'version':
        versions = [(cmd, kwargs) for cmd, kwargs in launches if cmd[-1] == '--version']
        assert len(versions) == 1
        cmd, kwargs = versions[0]
    else:
        cmd, kwargs = installs[1 if launch.startswith('restore_') else 0]
    assert cmd[1] == '-B'
    interpreter = str(executable / 'python') if project else sys.executable
    tail = ['--version'] if launch == 'version' else ['install', '-e', '.[all]', '--quiet']
    if not project:
        tail.append('--break-system-packages')
    assert cmd == [interpreter, '-B', '-m', 'pip', *tail]
    assert kwargs == {'cwd': str(fx.tree), 'stderr': subprocess.STDOUT,
                      'timeout': 10 if launch == 'version' else 180, 'env': expected_env}
    assert dict(os.environ) == daemon_env
    assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('unsafe', [False, True])
async def test_update_layout_preflight_declines_before_checkout(deployment, tmp_path, force, unsafe):
    fx = deployment
    fx.set_target(supports=False)
    outside = tmp_path / 'outside'
    outside.mkdir()
    inside = fx.data / 'inside'
    inside.mkdir()
    (fx.data / 'link').symlink_to(outside if unsafe else inside, target_is_directory=True)
    dirty = fx.tree / 'local.txt'
    dirty.write_text('local edit\n')
    before = dirty.read_bytes()
    result = await fx.update(force=force)
    assert result['preflight']['ok'] is (not unsafe)
    if unsafe:
        assert result['error'] == 'startup layout pre-flight failed'
        assert result['preflight']['findings'][0]['path'] == str(fx.data / 'link')
        assert fx.git('rev-parse', 'HEAD') == fx.previous
        assert dirty.read_bytes() == before
        assert not any(c[:2] == ['git', 'checkout'] for c in fx.commands)
        assert not fx.installs and not fx.scheduled
        fx.owner.assert_awaited_once()
    else:
        assert result['updated'] and fx.git('rev-parse', 'HEAD') == fx.target
        assert fx.scheduled
        if not force:
            assert dirty.read_bytes() == before


@pytest.mark.parametrize('unsafe', [False, True])
async def test_restart_layout_preflight(deployment, tmp_path, unsafe):
    fx = deployment
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = fx.data / 'link'
    link.symlink_to(outside if unsafe else fx.data, target_is_directory=True)
    result = await fx.restart()
    assert result['preflight']['ok'] is (not unsafe)
    assert bool(fx.scheduled) is (not unsafe)
    assert bool(result.get('error')) is unsafe
    fx.owner.assert_not_awaited()


@pytest.mark.parametrize('requested', [False, True])
@pytest.mark.parametrize('unsafe', [False, True])
async def test_update_preview_inspects_only_when_requested(deployment, monkeypatch, requested, unsafe):
    fx = deployment
    fx.set_target(supports=False)
    outside = fx.data.parent / 'outside'
    outside.mkdir()
    (fx.data / 'link').symlink_to(outside if unsafe else fx.data, target_is_directory=True)
    calls = []
    inspect_layout = getattr(fx.app.state.store_catalog, 'inspect_layout', None)
    def record():
        calls.append(True)
        return inspect_layout()
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout', record, raising=False)
    result = await fx.update(dry_run=True, preflight=requested)
    assert ('preflight' in result) is requested
    assert bool(calls) is requested
    if requested:
        assert result['preflight']['ok'] is (not unsafe)
    assert not fx.scheduled and not fx.children and not fx.installs
    assert fx.git('rev-parse', 'HEAD') == fx.previous
    fx.owner.assert_not_awaited()


@pytest.mark.parametrize('effect,reason', [
    ("raise RuntimeError('fixture import failure')", 'incomplete'),
    ('import time; time.sleep(60)', 'timeout'),
])
@pytest.mark.parametrize('failing', [False, True])
async def test_target_failure_restores_previous_commit(deployment, effect, reason, failing):
    fx = deployment
    fx.set_target(effect=effect if failing else 'import time; time.sleep(0)')
    if failing and reason == 'timeout':
        fx.child_timeout = 4
    result = await fx.update()
    assert_child(fx)
    if failing:
        assert result['error'] == 'target release failed its startup check'
        assert result['target_check']['reason'] == reason
        assert result['undo'] == dict(checked_out=fx.previous, ok=True, deps_restored=None, error=None)
        assert result['staying_on_version'] == fx.previous
        assert fx.git('rev-parse', 'HEAD') == fx.previous
        assert not fx.scheduled
        fx.owner.assert_awaited_once()
        assert 'fixture import failure' not in fx.owner.await_args.args[1]
    else:
        assert result['updated'] and fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.target
        fx.owner.assert_not_awaited()


@pytest.mark.parametrize('stages', [['import', 'layout'], ['import', 'layout', 'future_check']])
@pytest.mark.parametrize('findings', [False, True])
async def test_target_report_accepts_extensions(deployment, stages, findings):
    fx = deployment
    found = [{'kind': 'future_kind', 'path': str(fx.data / 'entry'), 'extra': {'value': 7}}] if findings else []
    fx.set_target(report=clean_report(fx.data, checked=stages, ok=not findings, findings=found, future_key=True), rc=int(findings))
    result = await fx.update()
    assert_child(fx)
    if findings:
        assert result['target_check']['reason'] == 'findings'
        assert result['target_check']['findings'] == found
        alert = fx.owner.await_args.args[1]
        assert 'reason=findings' in alert and fx.target in alert
        assert found[0]['path'] in alert and 'TREE_IMPORT=' not in alert
        assert result['undo']['ok'] and not fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.previous
    else:
        assert result['updated'] and fx.scheduled


@pytest.mark.parametrize('changed', [
    {'checked': ['import']}, {'checked': ['layout', 'import']},
    {'checked': ['import', 'layout', 'layout']}, {'checked': ['import', 'layout', '']},
    {'checked': ['import', 'layout', 7]}, {'ok': 'yes'}, {'root': 7},
    {'warnings_count': True}, {'warnings_count': 1}, {'warnings': [7]},
    {'findings': [{'kind': '', 'path': '/fixture'}]}, {'ok': False},
])
@pytest.mark.parametrize('invalid', [False, True])
async def test_target_invalid_report_undo_has_valid_twin(deployment, changed, invalid):
    fx = deployment
    fx.set_target(report=clean_report(fx.data, **(changed if invalid else {})))
    result = await fx.update()
    assert_child(fx)
    if invalid:
        assert result['target_check']['reason'] == 'invalid_report'
        assert result['undo']['ok'] and not fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.previous
    else:
        assert result['updated'] and fx.scheduled


@pytest.mark.parametrize('raw', ['not json', '{}', '{"ok": true}\n{}'])
@pytest.mark.parametrize('invalid', [False, True])
async def test_target_malformed_stdout_undo(deployment, raw, invalid):
    fx = deployment
    fx.set_target(raw=raw if invalid else json.dumps(clean_report(fx.data)))
    result = await fx.update()
    assert_child(fx)
    if invalid:
        assert result['target_check']['reason'] == 'invalid_report'
        assert result['undo']['ok'] and not fx.scheduled
    else:
        assert result['updated'] and fx.scheduled


@pytest.mark.parametrize('supports', [False, True])
async def test_target_without_startup_mode_skips_check(deployment, supports):
    fx = deployment
    fx.set_target(supports=supports)
    result = await fx.update(force=True)
    assert result['updated'] and fx.scheduled
    if supports:
        assert_child(fx)
        assert 'skipped' not in result['target_check']
    else:
        assert result['target_check'] == {'skipped': 'target has no startup check'}
        assert not fx.children


@pytest.mark.parametrize('failure', ['fail_probe', 'fail_spawn', 'fail_previous'])
@pytest.mark.parametrize('failing', [False, True])
async def test_probe_spawn_and_previous_ref_failures(deployment, failure, failing):
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    setattr(fx, failure, failing)
    result = await fx.update()
    if not failing:
        assert result['updated'] and fx.scheduled
        assert_child(fx)
    elif failure == 'fail_previous':
        assert result['error'] and not fx.scheduled and not fx.children
        assert fx.git('rev-parse', 'HEAD') == fx.previous
        assert not any(c[:2] == ['git', 'checkout'] for c in fx.commands)
    else:
        assert result['target_check']['reason'] == ('source_unreadable' if failure == 'fail_probe' else 'spawn_failed')
        assert result['undo']['ok'] and not fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.previous


@pytest.mark.parametrize('failure', [None, 'fail_install', 'fail_target_install', 'fail_restore', 'fail_undo', 'fail_confirmation'])
async def test_undo_checkout_and_dependency_outcomes(deployment, failure):
    fx = deployment
    fx.set_target(raw='invalid')
    if failure:
        setattr(fx, failure, True)
    result = await fx.update(force_deps=True)
    assert_child(fx)
    undo = result['undo']
    assert undo['ok'] is (failure in (None, 'fail_target_install'))
    assert not fx.scheduled
    if failure in ('fail_undo', 'fail_confirmation'):
        assert len(fx.installs) == 1
        assert not undo['ok'] and undo['error']
        assert 'next restart' in json.dumps(result).lower()
        assert 'next restart' in fx.owner.await_args.args[1].lower()
    else:
        assert len(fx.installs) == 2
        assert fx.installs[0][0] == fx.target and fx.installs[1][0] == fx.previous
        assert fx.installs[0][1] == fx.installs[1][1]
        assert undo['deps_restored'] is (failure in (None, 'fail_target_install'))
        assert fx.git('rev-parse', 'HEAD') == fx.previous


@pytest.mark.parametrize('deeply_nested', [False, True])
async def test_nested_target_report_restores_previous_commit(deployment, deeply_nested):
    fx = deployment
    raw = '[' * 100_000 + ']' * 100_000 if deeply_nested else json.dumps(clean_report(fx.data))
    fx.set_target(raw=raw, rc=0)
    result = await fx.update(force_deps=True)
    assert_child(fx)
    if deeply_nested:
        assert result['target_check']['reason'] == 'invalid_report'
        assert result['target_check']['rc'] == 0
        assert result['undo'] == dict(checked_out=fx.previous, ok=True, deps_restored=True, error=None)
        assert result['staying_on_version'] == fx.previous
        assert fx.git('rev-parse', 'HEAD') == fx.previous
        assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]
        assert not fx.scheduled
        fx.owner.assert_awaited_once()
    else:
        assert result['updated'] and fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.target
        assert [ref for ref, _ in fx.installs] == [fx.target]
        fx.owner.assert_not_awaited()


@pytest.mark.parametrize('failure,force_deps', [
    ('fail_restore', True), (None, True), ('fail_undo', True),
    ('fail_confirmation', True), (None, False),
])
async def test_owner_alert_identifies_failed_dependency_restore(deployment, failure, force_deps):
    fx = deployment
    finding = dict(kind='directory_outside_root', path=str(fx.data / 'linked-directory'))
    fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    if failure:
        setattr(fx, failure, True)
    result = await fx.update(force_deps=force_deps)
    assert_child(fx)
    assert result['target_check']['reason'] == 'findings'
    assert not fx.scheduled
    fx.owner.assert_awaited_once()
    message = fx.owner.await_args.args[1]
    suffix = ' Dependency restore failed. Installed packages may not match the release in the working tree.'
    assert 'Findings: directory_outside_root: ' + finding['path'] in message
    if failure == 'fail_restore':
        assert not result['undo']['ok'] and result['undo']['deps_restored'] is False
        assert result['staying_on_version'] == fx.previous
        assert fx.git('rev-parse', 'HEAD') == fx.previous
        assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]
        assert message.endswith(suffix)
    else:
        assert suffix not in message
        if failure in ('fail_undo', 'fail_confirmation'):
            assert not result['undo']['ok'] and result['undo']['deps_restored'] is False
            assert [ref for ref, _ in fx.installs] == [fx.target]
            assert message.endswith(result['undo_warning'])
            assert 'Undo checkout is not confirmed.' in message
        else:
            assert result['undo']['ok']
            assert result['undo']['deps_restored'] is (True if force_deps else None)
            assert len(fx.installs) == (2 if force_deps else 0)
            assert fx.git('rev-parse', 'HEAD') == fx.previous


@pytest.mark.parametrize('with_findings', [True, False])
@pytest.mark.parametrize('outcomes', [
    ('undone', 'unconfirmed'), ('undone', 'restore_failed'),
    ('undone', 'undone'), ('unconfirmed', 'unconfirmed'),
    ('restore_failed', 'restore_failed'),
])
async def test_startup_alert_suppression_distinguishes_undo_outcomes(deployment, monkeypatch, with_findings, outcomes):
    fx = deployment
    monkeypatch.setattr(api, '_startup_alert_now', lambda: 100.0)
    finding = dict(kind='directory_outside_root', path=str(fx.data / 'linked-directory'))
    if with_findings:
        fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    else:
        fx.set_target(raw='invalid report')
    suffix = ' Dependency restore failed. Installed packages may not match the release in the working tree.'
    for index, outcome in enumerate(outcomes):
        fx.fail_undo = outcome == 'unconfirmed'
        fx.fail_restore = outcome == 'restore_failed'
        fx.installs.clear()
        result = await fx.update(force_deps=True)
        assert result['target_check']['reason'] == ('findings' if with_findings else 'invalid_report')
        assert result['target_check']['findings'] == ([finding] if with_findings else [])
        assert result['deps_rebuilt'] and result['deps_error'] is None
        assert result['undo']['ok'] is (outcome == 'undone')
        assert result['undo']['deps_restored'] is (outcome == 'undone')
        if outcome == 'unconfirmed':
            assert 'Undo checkout is not confirmed.' in result['undo_warning']
            assert [ref for ref, _ in fx.installs] == [fx.target]
        else:
            assert 'undo_warning' not in result
            assert fx.git('rev-parse', 'HEAD') == fx.previous
            assert [ref for ref, _ in fx.installs] == [fx.target, fx.previous]
        alerts = [call.args[1] for call in fx.owner.await_args_list
                  if call.args[1].startswith('ADMIN UPDATE STARTUP CHECK FAILED:')]
        assert len(alerts) == (2 if index and outcomes[0] != outcome else 1)
        if outcome == 'unconfirmed':
            assert alerts[-1].endswith(result['undo_warning'])
        elif outcome == 'restore_failed':
            assert alerts[-1].endswith(suffix)
        else:
            assert suffix not in alerts[-1] and 'Undo checkout is not confirmed.' not in alerts[-1]
    assert len(fx.children) == 2 and not fx.scheduled


async def test_startup_alert_distinguishes_preflight_from_confirmed_undo(deployment, monkeypatch):
    fx = deployment
    monkeypatch.setattr(api, '_startup_alert_now', lambda: 100.0)
    finding = dict(kind='directory_outside_root', path=str(fx.data / 'linked-directory'))
    fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    findings = [finding]
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout',
                        lambda: SimpleNamespace(findings=list(findings), warnings=[]))
    first = await fx.update()
    assert first['preflight']['findings'] == [finding] and 'undo' not in first
    findings.clear()
    second = await fx.update()
    assert second['target_check']['findings'] == [finding] and second['undo']['ok']
    assert second['undo']['deps_restored'] is None
    alerts = [call.args[1] for call in fx.owner.await_args_list
              if call.args[1].startswith('ADMIN UPDATE STARTUP CHECK FAILED:')]
    assert len(alerts) == 2
    assert not fx.scheduled and not fx.installs


@pytest.mark.parametrize('endpoint', ['update', 'restart'])
async def test_preflight_runs_outside_event_loop(deployment, monkeypatch, endpoint):
    fx = deployment
    fx.set_target(supports=False)
    entered = threading.Event()
    release = threading.Event()
    seen_threads = []
    loop_thread = threading.get_ident()
    def inspect_layout():
        seen_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return SimpleNamespace(findings=[], warnings=[])
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout', inspect_layout, raising=False)
    async def release_on_loop():
        for _ in range(300):
            if entered.is_set():
                release.set()
                return
            await asyncio.sleep(.01)
    peer = asyncio.create_task(release_on_loop())
    try:
        result = await getattr(fx, endpoint)()
    finally:
        release.set()
        await peer
    assert result['preflight']['ok']
    assert seen_threads and seen_threads[0] != loop_thread


@pytest.mark.parametrize('incomplete', [False, True])
async def test_identity_failure_is_incomplete_preflight(deployment, monkeypatch, incomplete):
    fx = deployment
    fx.set_target(supports=False)
    def inspect_layout():
        if incomplete:
            raise PermissionError(13, 'fixture', str(fx.data / 'store.db'))
        return SimpleNamespace(findings=[], warnings=[])
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout', inspect_layout, raising=False)
    result = await fx.update()
    if incomplete:
        assert result['error'] == 'startup layout pre-flight could not complete'
        assert result['preflight'] == dict(ok=False, incomplete=True, reason='PermissionError: '+str(fx.data/'store.db'), findings=[])
        assert not fx.scheduled and not fx.children
    else:
        assert result['updated'] and result['preflight']['ok']


async def test_update_alert_suppression_set_window_and_delivery(deployment, monkeypatch):
    fx = deployment
    fx.set_target(supports=False)
    now = [100.0]
    monkeypatch.setattr(api, '_startup_alert_now', lambda: now[0], raising=False)
    findings = [dict(kind='directory_outside_root', path=str(fx.data/'a')),
                dict(kind='directory_outside_root', path=str(fx.data/'b'))]
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout',
                        lambda: SimpleNamespace(findings=list(findings), warnings=[]), raising=False)
    first = await fx.update()
    assert first['error'] == 'startup layout pre-flight failed'
    assert fx.owner.await_count == 1
    findings.reverse()
    findings.append(findings[0])
    await fx.update()
    assert fx.owner.await_count == 1
    now[0] += 20*60*60 - 1
    await fx.update()
    assert fx.owner.await_count == 1
    now[0] += 1
    await fx.update()
    assert fx.owner.await_count == 2
    findings[:] = [dict(kind='directory_outside_root', path=str(fx.data/'changed'))]
    fx.owner.return_value = False
    await fx.update()
    assert fx.owner.await_count == 3
    fx.owner.return_value = True
    await fx.update()
    assert fx.owner.await_count == 4
    await fx.update()
    assert fx.owner.await_count == 4
    assert not fx.scheduled


async def test_alert_reservation_precedes_delivery(deployment, monkeypatch):
    fx = deployment
    fx.set_target(supports=False)
    finding = dict(kind='directory_outside_root', path=str(fx.data/'entry'))
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout',
                        lambda: SimpleNamespace(findings=[finding], warnings=[]), raising=False)
    entered = asyncio.Event()
    release = asyncio.Event()
    async def notify(*args):
        entered.set()
        await release.wait()
        return True
    fx.owner.side_effect = notify
    first = asyncio.create_task(fx.update())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        second = await asyncio.wait_for(fx.update(), 5)
        assert second['error'] and fx.owner.await_count == 1
    finally:
        release.set()
        await first


async def test_target_empty_findings_alert_uses_target_identity(deployment):
    fx = deployment
    fx.set_target(raw='invalid report one')
    first_target = fx.target
    for _ in range(2):
        result = await fx.update()
        assert result['undo']['ok']
    assert fx.owner.await_count == 1
    fx.fail_probe = True
    result = await fx.update()
    assert result['target_check']['reason'] == 'source_unreadable'
    assert fx.owner.await_count == 2
    fx.fail_probe = False
    await fx.update()
    assert fx.owner.await_count == 2
    fx.set_target(raw='invalid report two')
    assert fx.target != first_target
    result = await fx.update()
    assert result['undo']['ok']
    assert fx.owner.await_count == 3


@pytest.mark.parametrize('invalid', [False, True])
async def test_target_non_utf8_report_has_valid_twin(deployment, invalid):
    fx = deployment
    raw = json.dumps(clean_report(fx.data)).encode()
    fx.set_target(raw_bytes=raw + (b'\xff' if invalid else b''))
    result = await fx.update()
    if invalid:
        assert result['target_check']['reason'] == 'invalid_report'
        assert result['target_check']['rc'] == 0
        assert result['undo']['ok'] and not fx.scheduled
    else:
        assert result['updated'] and fx.scheduled
    assert_child(fx)


@pytest.mark.parametrize('failed', [False, True])
async def test_target_probe_preserves_available_returncode(deployment, failed):
    fx = deployment
    fx.set_target(report=clean_report(fx.data))
    fx.fail_probe = failed
    result = await fx.update()
    assert result['target_check']['rc'] == int(failed)
    if failed:
        assert result['target_check']['reason'] == 'source_unreadable'
        assert result['undo']['ok'] and not fx.scheduled
    else:
        assert result['updated'] and fx.scheduled


@pytest.mark.parametrize('effect', ['pass', 'raise SystemExit(0)', 'raise SystemExit(7)'])
async def test_target_import_exit_is_incomplete(deployment, effect):
    fx = deployment
    fx.set_target(effect=effect)
    result = await fx.update()
    assert_child(fx)
    if effect == 'pass':
        assert result['updated'] and fx.scheduled
    else:
        assert result['target_check']['reason'] == 'incomplete'
        assert result['target_check']['rc'] == 2
        assert 'startup check could not complete: SystemExit:' in result['target_check']['stderr_tail']
        assert result['undo']['ok'] and not fx.scheduled
        assert fx.git('rev-parse', 'HEAD') == fx.previous


@pytest.mark.parametrize('source_inside_root', [False, True])
def test_target_child_leaves_no_bytecode(deployment, source_inside_root):
    fx = deployment
    fx.set_target()
    fx.git('checkout', '--detach', fx.target)
    root = fx.tree if source_inside_root else fx.data
    before = sorted(str(p.relative_to(fx.tree)) for p in fx.tree.rglob('*.pyc'))
    result = api._check_target_startup(str(fx.tree), str(root / 'conversations.db'), fx.target)
    after = sorted(str(p.relative_to(fx.tree)) for p in fx.tree.rglob('*.pyc'))
    assert result['rc'] == 0 and 'reason' not in result
    assert after == before == [], after
    assert '-B' in fx.children[0]['command']
    assert fx.children[0]['kwargs']['env']['PYTHONDONTWRITEBYTECODE'] == '1'


@pytest.mark.parametrize('same_size', [False, True])
async def test_confirmed_undo_imports_restored_source(deployment, monkeypatch, same_size):
    fx = deployment
    marker = fx.tree / 'src/pinky_daemon/cache_marker.py'
    marker.write_text("MODE = 'safe'\n")
    entry = fx.tree / 'src/pinky_daemon/__main__.py'
    guard = (
        'import json, os, sys\n'
        'from pinky_daemon import cache_marker\n'
        "if cache_marker.MODE == 'fail':\n"
        "    print('CACHE_SOURCE=' + json.dumps({'mtime': os.stat(cache_marker.__file__).st_mtime, "
        "'size': os.stat(cache_marker.__file__).st_size}), file=sys.stderr)\n"
        "    raise RuntimeError('synthetic target import failure')\n"
    )
    entry.write_text(entry.read_text().replace(
        'from __future__ import annotations\n', 'from __future__ import annotations\n' + guard))
    fx.git('add', 'src/pinky_daemon/cache_marker.py', 'src/pinky_daemon/__main__.py')
    fx.git('commit', '-m', 'Safe fixture import')
    previous = fx.git('rev-parse', 'HEAD')
    marker.write_text("MODE = 'fail'\n" + ('' if same_size else '\n'))
    fx.git('add', 'src/pinky_daemon/cache_marker.py')
    fx.git('commit', '-m', 'Failing fixture import')
    fx.target = fx.git('rev-parse', 'HEAD')
    fx.git('push', '--force', 'origin', 'HEAD:main')
    fx.git('checkout', '--detach', previous)
    # Avoid optional command wrappers for this timing-sensitive owned fixture.
    # The test runner still fences mutations of the importing source checkout.
    monkeypatch.setenv('PATH', os.pathsep.join(
        part for part in os.environ['PATH'].split(os.pathsep)
        if Path(part) != Path(api.__file__).resolve().parents[3] / 'guards'
        and Path(part).name != 'guards'
    ))
    await asyncio.sleep(1 - time.time() % 1 + .01)
    result = await fx.update()
    assert result['undo']['ok'] and not fx.scheduled
    assert fx.git('rev-parse', 'HEAD') == previous
    assert marker.read_text() == "MODE = 'safe'\n"
    stderr = fx.children[0]['stderr'].decode()
    target_metadata = json.loads(next(line.split('=', 1)[1] for line in stderr.splitlines()
                                     if line.startswith('CACHE_SOURCE=')))
    assert int(target_metadata['mtime']) == int(marker.stat().st_mtime)
    assert (target_metadata['size'] == marker.stat().st_size) is same_size
    env = dict(os.environ, PYTHONPATH=str(fx.tree / 'src'))
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    probe = subprocess.run(
        [sys.executable, '-c', 'from pinky_daemon.cache_marker import MODE; print(MODE)'],
        cwd=fx.tree, env=env, capture_output=True, text=True, timeout=10,
    )
    fresh = subprocess.run(
        [sys.executable, '-m', 'pinky_daemon', '--mode', 'check-startup',
         '--db-path', str(fx.data / 'conversations.db')],
        cwd=fx.tree, env=env, capture_output=True, text=True, timeout=45,
    )
    assert probe.returncode == 0 and probe.stdout.strip() == 'safe', probe.stdout
    assert fresh.returncode == 0 and json.loads(fresh.stdout)['ok'], fresh.stderr


@pytest.mark.parametrize('spawn', [False, True])
@pytest.mark.parametrize('outcome', ['timeout', 'findings', 'clean'])
async def test_target_process_group_ends_before_continuing(deployment, tmp_path, monkeypatch, spawn, outcome):
    fx = deployment
    marker = tmp_path / 'descendant-completed'
    heartbeat = tmp_path / 'descendant-heartbeat'
    pidfile = tmp_path / 'descendant-pid'
    groupfile = tmp_path / 'child-group'
    child_code = (
        'import time\nfrom pathlib import Path\n'
        'until = time.monotonic() + 3\n'
        'while time.monotonic() < until:\n'
        f'    with Path({str(heartbeat)!r}).open("a") as stream:\n'
        '        stream.write("tick\\n")\n'
        '    time.sleep(.025)\n'
        f'Path({str(marker)!r}).write_text("survived")\n'
    )
    effect = (
        'import json, os, subprocess, sys, time\nfrom pathlib import Path\n'
        f'Path({str(groupfile)!r}).write_text(json.dumps([os.getpid(), os.getpgid(0), os.getsid(0)]))\n'
        f'if {spawn!r}:\n'
        f'    descendant = subprocess.Popen([sys.executable, "-c", {child_code!r}], '
        'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n'
        f'    Path({str(pidfile)!r}).write_text(str(descendant.pid))\n'
        '    for attempt in range(200):\n'
        f'        if Path({str(heartbeat)!r}).exists():\n'
        '            break\n'
        '        time.sleep(.005)\n'
    )
    if outcome == 'timeout':
        effect += 'time.sleep(60)\n'
        # Counted from the hand-back in ObservedChild; it must end well before the
        # descendant's own 3 s deadline, so only the group kill can stop it.
        fx.child_timeout = 1
    elif outcome == 'findings':
        finding = dict(kind='directory_outside_root', path=str(fx.data / 'link'))
        effect += (
            'from types import SimpleNamespace\nfrom pinky_daemon import store_catalog\n'
            f'store_catalog.inspect_filesystem_layout = lambda *a, **k: SimpleNamespace(findings=[{finding!r}], warnings=[])\n'
        )
    fx.set_target(effect=effect)
    actual_popen, actual_killpg = subprocess.Popen, os.killpg
    actual_output = subprocess.check_output
    events, processes, undo_states = [], [], []

    class ObservedChild(actual_popen):
        def __init__(self, command, *args, **kwargs):
            self.is_check = '-m' in command and 'pinky_daemon' in command
            super().__init__(command, *args, **kwargs)
            if self.is_check:
                processes.append(self)
                # The preflight's wait and timeout start when this returns, so hand the child
                # back only once its descendant is recorded and heartbeating (bounded).
                deadline = time.monotonic() + 30
                while spawn and time.monotonic() < deadline and not (
                        pidfile.exists() and heartbeat.exists() and heartbeat.stat().st_size):
                    time.sleep(.01)

        def wait(self, *args, **kwargs):
            if self.is_check and self.returncode is None:
                events.append(('reap', self.pid))
            return super().wait(*args, **kwargs)

        def poll(self):
            if self.is_check and self.returncode is None:
                events.append(('poll', self.pid))
            return super().poll()

    def kill_group(pid, sig):
        if processes and pid == processes[0].pid:
            events.append(('kill', pid, sig, processes[0].returncode))
        return actual_killpg(pid, sig)

    def output(command, **kwargs):
        if command[:3] == ['git', 'checkout', '--detach'] and processes:
            before = heartbeat.read_bytes() if spawn else b''
            time.sleep(.15)
            after = heartbeat.read_bytes() if spawn else b''
            undo_states.append((before, after))
        return actual_output(command, **kwargs)

    monkeypatch.setattr(subprocess, 'Popen', ObservedChild)
    monkeypatch.setattr(os, 'killpg', kill_group)
    monkeypatch.setattr(subprocess, 'check_output', output)
    observed_full_delay = False
    try:
        result = await fx.update()
        on_return = heartbeat.read_bytes() if spawn else b''
        assert pidfile.exists() is spawn
        if outcome == 'clean':
            assert result['updated'] and fx.scheduled and not undo_states
        else:
            assert result['target_check']['reason'] == outcome
            assert result['undo']['ok'] and not fx.scheduled
            assert fx.git('rev-parse', 'HEAD') == fx.previous
            assert len(undo_states) == 1
        await asyncio.sleep(3.2)
        observed_full_delay = True
        assert not marker.exists(), 'target descendant wrote after the endpoint continued'
        assert (heartbeat.read_bytes() if spawn else b'') == on_return
        assert all(before == after for before, after in undo_states)
        if spawn:
            assert on_return and all(before for before, _ in undo_states)
        if spawn:
            assert json.loads(groupfile.read_text()) == [processes[0].pid] * 3
            assert events and events[0] == ('kill', processes[0].pid, signal.SIGKILL, None)
            assert any(event[0] == 'reap' for event in events[1:])
            assert not any(event[0] == 'poll' for event in events)
    finally:
        # Let the finite owned child finish even after an earlier assertion.
        if not observed_full_delay:
            await asyncio.sleep(3.2)


@pytest.mark.parametrize('mismatch', [False, True])
async def test_undo_requires_complete_previous_id(deployment, monkeypatch, mismatch):
    fx = deployment
    fx.set_target(raw='invalid report')
    actual_ref = api._startup_full_ref
    other = fx.previous[:7] + ('a' if fx.previous[7] != 'a' else 'b') + fx.previous[8:]
    assert len(other) == 40 and other != fx.previous and other[:7] == fx.previous[:7]

    def full_ref(repo, ref='HEAD'):
        if mismatch and ref == 'HEAD' and fx.children:
            return other
        return actual_ref(repo, ref)

    monkeypatch.setattr(api, '_startup_full_ref', full_ref)
    result = await fx.update()
    assert result['target_check']['reason'] == 'invalid_report'
    assert result['undo']['ok'] is (not mismatch)
    assert ('undo_warning' in result) is mismatch
    assert fx.git('rev-parse', 'HEAD') == fx.previous and not fx.scheduled


@pytest.mark.parametrize('outcomes', [
    ('restore_failed', 'unconfirmed'), ('unconfirmed', 'restore_failed'),
    ('none', 'unconfirmed'), ('none', 'restore_failed'),
])
async def test_startup_alert_distinguishes_each_decline_pair(deployment, monkeypatch, outcomes):
    fx = deployment
    monkeypatch.setattr(api, '_startup_alert_now', lambda: 100.0)
    finding = dict(kind='directory_outside_root', path=str(fx.data / 'link'))
    fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    preflight_findings = []
    monkeypatch.setattr(fx.app.state.store_catalog, 'inspect_layout',
                        lambda: SimpleNamespace(findings=list(preflight_findings), warnings=[]))
    actual_ref = api._startup_full_ref
    attempt = {'outcome': None, 'children': 0}

    def full_ref(repo, ref='HEAD'):
        if (attempt['outcome'] == 'unconfirmed' and ref == 'HEAD'
                and len(fx.children) > attempt['children']):
            return fx.target
        return actual_ref(repo, ref)

    monkeypatch.setattr(api, '_startup_full_ref', full_ref)
    for index, outcome in enumerate(outcomes):
        preflight_findings[:] = [finding] if outcome == 'none' else []
        attempt.update(outcome=outcome, children=len(fx.children))
        fx.fail_restore = outcome == 'restore_failed'
        fx.installs.clear()
        result = await fx.update(force_deps=True)
        if outcome == 'none':
            assert result['preflight']['findings'] == [finding] and 'undo' not in result
            assert not fx.installs and not fx.children
        else:
            assert result['target_check']['findings'] == [finding]
            assert result['deps_rebuilt'] and result['deps_error'] is None
            assert not result['undo']['ok'] and result['undo']['deps_restored'] is False
            assert ('undo_warning' in result) is (outcome == 'unconfirmed')
            assert [ref for ref, _ in fx.installs] == (
                [fx.target] if outcome == 'unconfirmed' else [fx.target, fx.previous])
        alerts = [call.args[1] for call in fx.owner.await_args_list
                  if call.args[1].startswith('ADMIN UPDATE STARTUP CHECK FAILED:')]
        assert len(alerts) == index + 1
    assert not fx.scheduled


@pytest.mark.parametrize('platform,ended,error,accepted', [
    ('darwin', True, None, True), ('linux', True, None, True),
    ('darwin', False, None, True), ('linux', False, None, True),
    ('darwin', True, errno.EPERM, True), ('darwin', False, errno.EPERM, False),
    ('linux', True, errno.EPERM, False), ('linux', False, errno.EPERM, False),
    ('darwin', True, errno.EIO, False), ('linux', True, errno.EIO, False),
    ('darwin', True, errno.ESRCH, False), ('linux', True, errno.ESRCH, False),
])
def test_group_signal_error_policy(monkeypatch, platform, ended, error, accepted):
    send_group = getattr(api, '_signal_startup_group', None)
    assert callable(send_group), 'startup group signal decision is missing'
    calls = []

    def kill_group(pid, sig):
        calls.append((pid, sig))
        if error is not None:
            raise OSError(error, 'synthetic private details')

    with monkeypatch.context() as patch:
        patch.setattr(api.sys, 'platform', platform)
        patch.setattr(api.os, 'killpg', kill_group)
        assert send_group(12345, ended) is accepted
    assert calls == [(12345, signal.SIGKILL)]


@pytest.mark.parametrize('outcome', ['clean', 'findings', 'incomplete', 'timeout'])
async def test_group_cleanup_error_never_schedules_restart(deployment, monkeypatch, outcome):
    fx = deployment
    if outcome == 'clean':
        fx.set_target(report=clean_report(fx.data))
    elif outcome == 'findings':
        finding = dict(kind='directory_outside_root', path=str(fx.data / 'link'))
        fx.set_target(report=clean_report(fx.data, ok=False, findings=[finding]), rc=1)
    else:
        fx.set_target(effect=("raise RuntimeError('synthetic incomplete import')"
                              if outcome == 'incomplete' else 'import time; time.sleep(60)'))
        if outcome == 'timeout':
            fx.child_timeout = 2
    actual_signal = getattr(api, '_signal_startup_group', None)
    observed = []

    def failed_signal(pid, ended):
        observed.append(ended)
        if actual_signal is not None:
            actual_signal(pid, ended)
        return False

    monkeypatch.setattr(api, '_signal_startup_group', failed_signal, raising=False)
    result = await fx.update()
    assert result.get('target_check', {}).get('reason') == ('invalid_report' if outcome == 'clean' else outcome)
    assert result['undo']['ok'] and not fx.scheduled
    assert fx.git('rev-parse', 'HEAD') == fx.previous
    assert observed == [outcome != 'timeout']
    if outcome == 'clean':
        assert result['target_check']['stderr_tail'] == 'target process group cleanup failed'


@pytest.mark.parametrize('ends', [False, True])
def test_wait_for_child_keeps_linux_status_waitable(monkeypatch, ends):
    wait_for_child = getattr(api, '_startup_child_exited', None)
    assert callable(wait_for_child), 'non-reaping startup child wait is missing'
    calls, sleeps = [], []
    now = [0.0]

    def waitid(kind, pid, options):
        calls.append((kind, pid, options))
        return SimpleNamespace(si_pid=pid) if ends and len(calls) == 2 else None

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(api.os, 'waitid', waitid, raising=False)
    monkeypatch.setattr(api.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(api.time, 'sleep', sleep)
    assert wait_for_child(12345, .03) is ends
    assert calls and all(call == (os.P_PID, 12345, os.WEXITED | os.WNOWAIT | os.WNOHANG) for call in calls)
    assert sleeps and all(0 < interval <= .01 for interval in sleeps)
    assert (now[0] < .03) is ends


@pytest.mark.parametrize('error', [errno.ESRCH, errno.EIO])
def test_wait_for_already_ended_child_closes_queue(monkeypatch, error):
    import select

    registrations, constructions = [], []
    constants = dict(KQ_FILTER_PROC=-5, KQ_EV_ADD=1, KQ_EV_ONESHOT=2,
                     KQ_NOTE_EXIT=4, KQ_EV_ERROR=8)

    class Queue:
        closed = False

        def control(self, changes, maximum, timeout):
            registrations.append((changes, maximum, timeout))
            return [SimpleNamespace(flags=constants['KQ_EV_ERROR'], data=error)]

        def close(self):
            self.closed = True

    queue = Queue()

    def event(pid, **kwargs):
        result = SimpleNamespace(pid=pid, **kwargs)
        constructions.append(result)
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError('queue observation must not start a process, sleep or read a clock')

    with monkeypatch.context() as patch:
        patch.delattr(api.os, 'waitid', raising=False)
        patch.setattr(select, 'kqueue', lambda: queue, raising=False)
        patch.setattr(select, 'kevent', event, raising=False)
        for name, value in constants.items():
            patch.setattr(select, name, value, raising=False)
        patch.setattr(subprocess, 'Popen', forbidden)
        patch.setattr(api.time, 'sleep', forbidden)
        patch.setattr(api.time, 'monotonic', forbidden)
        patch.setattr(api.time, 'time', forbidden)
        if error == errno.ESRCH:
            assert api._startup_child_exited(12345, 30) is True
        else:
            with pytest.raises(OSError) as raised:
                api._startup_child_exited(12345, 30)
            assert raised.value.errno == error
    assert queue.closed
    assert len(constructions) == 1
    assert vars(constructions[0]) == dict(pid=12345, filter=constants['KQ_FILTER_PROC'],
                                         flags=constants['KQ_EV_ADD'] | constants['KQ_EV_ONESHOT'],
                                         fflags=constants['KQ_NOTE_EXIT'])
    assert registrations == [([constructions[0]], 1, 30)]
