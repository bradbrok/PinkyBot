"""Transport-level controls: validated DNS answers are the actual socket targets."""
import io
import socket
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import pytest

from pinky_daemon import trigger_fetch


def answer(address):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, 443))


@pytest.mark.parametrize('addresses', [[], ['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'],
    ['0.0.0.0'], ['224.0.0.1'], ['100.64.0.1'], ['::1'], ['fc00::1'], ['fe80::1'],
    ['::ffff:127.0.0.1'], ['93.184.216.34', '10.0.0.1']])
def test_unapproved_dns_never_connects(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [answer(x) for x in addresses])
    opened = []
    monkeypatch.setattr(socket, 'socket', lambda *a: opened.append(a))
    with pytest.raises(ValueError):
        trigger_fetch._public_connection(('fixture.test', 443), 5)
    assert opened == []


def test_connection_pins_validated_address(monkeypatch):
    resolutions, connections = [], []

    def resolve(*args, **kwargs):
        resolutions.append(args)
        # A second resolution would give an unsafe rebinding answer.
        return [answer('93.184.216.34' if len(resolutions) == 1 else '127.0.0.1')]

    class Sock:
        def settimeout(self, value):
            assert value == 5

        def connect(self, addr):
            connections.append(addr)

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', lambda *a: Sock())
    trigger_fetch._public_connection(('fixture.test', 443), 5)
    assert len(resolutions) == 1
    assert connections == [('93.184.216.34', 443)]


@pytest.mark.parametrize('url', ['file:///scratch', 'ftp://example.test/a', '',
    'https://', 'https://user:secret@example.test/a', 'https://example.test:bad/',
    'https://example.test/a\n'])
def test_invalid_url_syntax(url):
    with pytest.raises(ValueError):
        trigger_fetch.validate_trigger_url(url)


@pytest.mark.parametrize('scheme', ['http', 'https'])
@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
def test_public_redirect_chain_and_relative_location(monkeypatch, scheme, code):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [answer('93.184.216.34')])
    fetched = []

    def network(self, connection, request, **kwargs):
        fetched.append(request.full_url)
        headers = Message()
        status = 200
        if request.full_url.endswith('/start'):
            headers['Location'] = '/final'
            status = code
        response = addinfourl(io.BytesIO(b'fixture'), headers, request.full_url, status)
        response.msg = 'fixture'
        return response

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, 'do_open', network)
    with trigger_fetch.open_trigger_url(f'{scheme}://fixture.test/start') as response:
        assert response.read() == b'fixture'
    assert fetched == [f'{scheme}://fixture.test/start', f'{scheme}://fixture.test/final']


def test_https_connection_retains_original_hostname():
    # TLS uses HTTPConnection.host; only its socket dialer is replaced.
    connection = trigger_fetch._connection_factory(__import__('http.client').client.HTTPSConnection)(
        'fixture.test:443', timeout=5)
    assert connection.host == 'fixture.test'
    assert connection._create_connection is trigger_fetch._public_connection
