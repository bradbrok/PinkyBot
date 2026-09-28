"""Always-on signed tenant admin boundary, using real auth and scratch stores."""
import asyncio
from contextlib import contextmanager
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.auth import SESSION_COOKIE_NAME, create_session_cookie
from tests.conftest import TEST_SESSION_SECRET
from tests.hotfix_support import daemon as daemon
from tests.hotfix_support import signed

pytestmark = pytest.mark.real_auth

@contextmanager
def scratch_client(app, **kwargs):
    # Exercise HTTP middleware/handlers without starting the daemon lifespan.
    client = TestClient(app, **kwargs)
    try:
        yield client
    finally:
        client.close()


CASES = [
    ('POST', '/admin/restart', {}, {}, 200),
    ('POST', '/admin/update', {'branch': 'invalid'}, {}, 400),
    ('POST', '/admin/channel', {'channel': 'invalid'}, {}, 400),
    ('POST', '/admin/force-restart-agent/peer', {}, {}, 404),
    ('GET', '/admin/channel', {}, None, 200),
    ('POST', '/agents', {}, {}, 400),
]


@pytest.fixture
def no_restart(monkeypatch):
    scheduled = []
    original = asyncio.create_task

    def capture(coro, *args, **kwargs):
        if getattr(coro, '__name__', '') == '_delayed_exit':
            scheduled.append('restart')
            coro.close()
            return None
        return original(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_task', capture)
    return scheduled


@pytest.mark.parametrize('mode', [None, 'off', 'shadow', 'enforce'])
@pytest.mark.parametrize('method,path,params,body,baseline', CASES)
def test_isolated_admin_denied_always(daemon, no_restart, mode, method, path, params, body, baseline):
    d = daemon(mode)
    with scratch_client(d.app) as client:
        response = client.request(method, path, params=params, json=body,
                                  headers=signed(d, method, path))
    assert (response.status_code, no_restart) == (403, [])


@pytest.mark.parametrize('principal', ['normal', 'owner'])
@pytest.mark.parametrize('method,path,params,body,baseline', CASES)
def test_admin_controls_unchanged(daemon, no_restart, principal, method, path, params, body, baseline):
    d = daemon()
    with scratch_client(d.app) as client:
        if principal == 'owner':
            client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
            headers = {}
        else:
            headers = signed(d, method, path, principal)
        response = client.request(method, path, params=params, json=body, headers=headers)
    assert response.status_code == baseline, response.text
    assert no_restart == (['restart'] if path == '/admin/restart' else [])


@pytest.mark.parametrize('failure', ['error', 'missing'])
@pytest.mark.parametrize('path', ['/admin/restart', '/agents'])
def test_registry_uncertainty_denies(daemon, monkeypatch, no_restart, failure, path):
    d = daemon('off')
    headers = signed(d, 'POST', path)
    original = d.agents.get

    def lookup(name):
        if name == 'tenant':
            if failure == 'error':
                raise RuntimeError('fixture registry unavailable')
            return None
        return original(name)

    monkeypatch.setattr(d.agents, 'get', lookup)
    with scratch_client(d.app, raise_server_exceptions=False) as client:
        response = client.post(path, headers=headers, json={})
    assert (response.status_code, no_restart) == (403, [])


@pytest.mark.parametrize('path', ['/admin//restart', '/admin%2Frestart', '/%61dmin/restart',
                                '/admin/%2Frestart', '/admin/restart/', '/agents/'])
def test_admin_path_variants(daemon, no_restart, path):
    d = daemon('off')
    with scratch_client(d.app, follow_redirects=False) as client:
        response = client.post(path, headers=signed(d, 'POST', unquote(path)), json={})
    assert (response.status_code, no_restart) == (403, [])


def test_double_encoding_does_not_dispatch(daemon, no_restart):
    d = daemon('off')
    path = '/admin%252Frestart'
    with scratch_client(d.app, follow_redirects=False) as client:
        response = client.post(path, headers=signed(d, 'POST', unquote(path)))
    assert response.status_code in (200, 403, 404)
    if response.status_code == 200:
        assert "text/html" in response.headers["content-type"]
    assert no_restart == []


def test_signed_isolated_precedes_owner_cookie(daemon, no_restart):
    d = daemon('off')
    with scratch_client(d.app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
        response = client.post('/admin/restart', headers=signed(d, 'POST', '/admin/restart'))
    assert (response.status_code, no_restart) == (403, [])


def test_existing_registration_guard_control(daemon):
    d = daemon('off')
    with scratch_client(d.app) as client:
        response = client.post('/agents', headers=signed(d, 'POST', '/agents'),
                               json={'name': 'fixture-new', 'working_dir': str(d.root/'fixture-new')})
    assert response.status_code == 403
    assert d.agents.get('fixture-new') is None


@pytest.mark.parametrize('prefix', ['/proxy', '/administrator'])
@pytest.mark.parametrize('principal', ['tenant', 'normal', 'owner'])
def test_admin_root_path_matches_dispatch(daemon, no_restart, prefix, principal):
    d = daemon('off')
    path = prefix + '/admin/restart'
    with scratch_client(d.app, root_path=prefix) as client:
        if principal == 'owner':
            client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(TEST_SESSION_SECRET))
            headers = {}
        else:
            headers = signed(d, 'POST', path, principal)
        response = client.post(path, headers=headers)
    expected = (403, []) if principal == 'tenant' else (200, ['restart'])
    assert (response.status_code, no_restart) == expected


def test_misleading_root_prefix_does_not_change_route(daemon, no_restart):
    d = daemon('off')
    hits = []

    @d.app.post('/proxyish/admin/restart')
    async def unrelated():
        hits.append(True)
        return {'ok': True}

    path = '/proxyish/admin/restart'
    with scratch_client(d.app, root_path='/proxy') as client:
        response = client.post(path, headers=signed(d, 'POST', path))
    assert (response.status_code, hits, no_restart) == (200, [True], [])
