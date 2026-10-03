"""Operator exceptions must authorize the actual destination and explicit port."""
import io
import logging
import socket
import urllib.request
from email.message import Message
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.response import addinfourl

import pytest

from pinky_daemon import trigger_fetch
from pinky_daemon.scheduler import AgentScheduler


@pytest.fixture(autouse=True)
def isolated_network(monkeypatch):
    monkeypatch.delenv('PINKY_URL_TRIGGER_ALLOW', raising=False)

    def dns(host, port, **kwargs):
        address = {'monitor.test': '100.64.0.25', 'public.test': '93.184.216.34'}.get(
            host, host)
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))]

    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    # Every fetch uses a fake response; an unexpected real socket is a test error.
    real_socket = socket.socket

    def safe_socket(family=socket.AF_INET, *args, **kwargs):
        assert family == socket.AF_UNIX, 'real network socket'
        return real_socket(family, *args, **kwargs)

    monkeypatch.setattr(socket, 'socket', safe_socket)
    fetched = []

    def network(self, connection, request, **kwargs):
        fetched.append(request.full_url)
        headers = Message()
        code = 200
        if request.full_url.endswith('/redirect'):
            headers['Location'] = 'http://10.99.0.1/private?secret=never-log'
            code = 302
        response = addinfourl(io.BytesIO(b'private response never-log'), headers,
                             request.full_url, code)
        response.msg = 'fixture'
        return response

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, 'do_open', network)
    return fetched


@pytest.mark.parametrize('url,allowed', [
    ('http://100.64.0.25:8081/state.json', '100.64.0.25:8081'),
    ('http://monitor.test:8081/state.json', '100.64.0.25:8081'),
    ('https://10.1.2.3/state.json', '10.0.0.0/8:443'),
    ('http://[fd00::7]:8081/state.json', '[fd00::/8]:8081'),
    ('http://127.0.0.1:8081/state.json', '127.0.0.1:8081'),
    ('http://169.254.169.254:8081/state.json', '169.254.169.254:8081'),
])
def test_allowlisted_address_and_port_succeeds(monkeypatch, isolated_network, url, allowed):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', allowed)
    with trigger_fetch.open_trigger_url(url) as response:
        assert response.status == 200
    assert isolated_network == [url]


@pytest.mark.parametrize('url,allowed', [
    ('http://0.0.0.0:8081/x', '0.0.0.0/0:8081'),
    ('http://0.0.0.0:8081/x', '0.0.0.0:8081'),
    ('http://[::]:8081/x', '[::/0]:8081'),
    ('http://224.0.0.251:8081/x', '224.0.0.0/4:8081'),
    ('http://100.64.0.25:8082/state.json', '100.64.0.25:8081'),
    ('http://10.99.0.1/state.json', '100.64.0.25:8081'),
    ('http://100.64.0.25:8081/state.json', ''),
    ('http://127.0.0.1:8081/state.json', '127.0.0.0/8:8081'),
    ('http://169.254.169.254:8081/state.json', '169.254.0.0/16:8081'),
    ('http://[::1]:8081/state.json', '[::/0]:8081'),
    ('http://[fe80::1]:8081/state.json', '[fe80::/10]:8081'),
])
def test_unlisted_address_or_port_refused(monkeypatch, isolated_network, url, allowed):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', allowed)
    with pytest.raises(ValueError):
        trigger_fetch.open_trigger_url(url)
    assert isolated_network == []


def test_allowlisted_initial_redirect_unlisted_refused(monkeypatch, isolated_network):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', '100.64.0.25:8081')
    url = 'http://100.64.0.25:8081/redirect'
    with pytest.raises(ValueError):
        trigger_fetch.open_trigger_url(url)
    assert isolated_network == [url]


@pytest.mark.parametrize('entry', ['100.64.0.25', '10.0.0.0/8', '100.64.0.25:bad',
    '100.64.0.25:0', '100.64.0.25:65536', 'http://100.64.0.25:8081',
    '100.64.0.25:*', 'user:secret@100.64.0.25:8081'])
def test_malformed_allowlist_ignored_with_warning(monkeypatch, caplog, isolated_network, entry):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', entry)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError):
            trigger_fetch.open_trigger_url('http://100.64.0.25:8081/state.json')
    assert isolated_network == []
    assert any(r.levelno == logging.WARNING and 'PINKY_URL_TRIGGER_ALLOW' in r.message
               for r in caplog.records)
    assert 'secret' not in caplog.text


def test_bad_entry_does_not_disable_valid_entry(monkeypatch, caplog):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', 'bad,100.64.0.25:8081')
    with caplog.at_level(logging.WARNING):
        with trigger_fetch.open_trigger_url('http://monitor.test:8081/state.json') as response:
            assert response.status == 200
    assert 'PINKY_URL_TRIGGER_ALLOW' in caplog.text


@pytest.mark.parametrize('url', ['file:///fixture', 'http://user:secret@100.64.0.25:8081/',
    'http://100.64.0.25:8081/a\n', 'http://100.64.0.25:8081/a\x7f'])
def test_allowlist_never_bypasses_url_validation(monkeypatch, isolated_network, url):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', '100.64.0.25:8081')
    with pytest.raises(ValueError):
        trigger_fetch.open_trigger_url(url)
    assert isolated_network == []


def test_allowed_dns_is_pinned_and_all_answers_checked(monkeypatch):
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', '100.64.0.25:8081')
    sock = Mock()
    monkeypatch.setattr(socket, 'socket', Mock(return_value=sock))
    trigger_fetch._public_connection(('monitor.test', 8081), 5)
    sock.connect.assert_called_once_with(('100.64.0.25', 8081))
    sock.reset_mock()
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 8081))
        for ip in ['100.64.0.25', '10.99.0.1']])
    with pytest.raises(ValueError):
        trigger_fetch._public_connection(('monitor.test', 8081), 5)
    sock.connect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('redirect', [False, True])
async def test_refused_internal_warning_per_trigger_per_hour(
    monkeypatch, caplog, isolated_network, redirect,
):
    clock = [1000.0]
    monkeypatch.setenv('PINKY_URL_TRIGGER_ALLOW', '100.64.0.25:8081' if redirect else '')
    url = ('http://100.64.0.25:8081/redirect' if redirect
           else 'http://10.99.0.1/private?secret=never-log')
    row = SimpleNamespace(id=6, name='fixture', url=url, method='GET', last_value='old')
    store = Mock()
    scheduler = AgentScheduler(Mock(), trigger_store=store, wake_callback=Mock())
    with caplog.at_level(logging.WARNING):
        await scheduler._poll_url_trigger(row, clock[0])
        clock[0] += 3599
        await scheduler._poll_url_trigger(row, clock[0])
        row.id = 7
        await scheduler._poll_url_trigger(row, clock[0])
        row.id = 6
        clock[0] += 1
        await scheduler._poll_url_trigger(row, clock[0])
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING
                and '10.99.0.1:80' in r.message]
    assert len(warnings) == 3, caplog.text
    assert '6' in warnings[0] and '7' in warnings[1] and '6' in warnings[2]
    assert 'never-log' not in caplog.text and 'secret' not in caplog.text
    assert store.record_check.call_count == 4
    store.record_fire.assert_not_called()
    scheduler._wake_callback.assert_not_called()


def test_starlette_route_path_private_contract():
    try:
        from starlette._utils import get_route_path
    except ImportError:
        pytest.fail('Security guard requires starlette._utils.get_route_path; review dispatcher')
    assert get_route_path({'path': '/hooks/admin/restart', 'root_path': '/hooks'}) == '/admin/restart'
    assert get_route_path({'path': '/hooks2/admin', 'root_path': '/hooks'}) == '/hooks2/admin'
