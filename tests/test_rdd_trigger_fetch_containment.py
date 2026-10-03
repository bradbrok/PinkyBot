"""Separable trigger URL safety REDs for creation and deferred execution."""

import builtins
import io
import socket
import time
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.routes import triggers
from pinky_daemon.scheduler import AgentScheduler
from tests.isolated_policy_support import daemon as daemon
from tests.isolated_policy_support import signed

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("actor", ["tenant", "normal"])
@pytest.mark.parametrize(
    "url",
    [
        "file:///fixture-peer.txt",
        "ftp://example.test/a",
        "data:text/plain,fixture",
        "gopher://example.test/a",
        "https://example.test/ok",
    ],
)
def test_trigger_create_allows_only_http_schemes(daemon, actor, url):
    d = daemon("off")
    path = f"/agents/{actor}/triggers"
    client = TestClient(d.app)
    response = client.post(
        path,
        headers=signed(d, "POST", path, actor),
        json={
            "trigger_type": "url",
            "name": "fixture",
            "url": url,
            "condition": "body_contains",
            "condition_value": "",
            "prompt_template": "{{body_raw}}",
        },
    )
    client.close()
    if url.startswith("https:"):
        assert response.status_code == 200, response.text
    else:
        assert response.status_code in (400, 422), (response.status_code, response.text)
        assert triggers._trigger_store.list(agent_name=actor) == []


async def poll(d, url):
    store = triggers._trigger_store
    store.create(
        agent_name="tenant",
        name="persisted-fixture",
        trigger_type="url",
        url=url,
        method="GET",
        condition="body_contains",
        condition_value="",
        prompt_template="{{body_raw}}",
        interval_seconds=1,
    )
    wakes = []

    async def wake(*args, **kwargs):
        wakes.append(args)

    scheduler = AgentScheduler(d.agents, trigger_store=store, wake_callback=wake)
    await scheduler._check_url_watchers(time.time())
    return wakes


@pytest.mark.asyncio
async def test_persisted_file_trigger_never_reads_peer_file(daemon, monkeypatch):
    d = daemon("off")
    peer = d.root / "peer" / "trigger-fixture.txt"
    peer.write_text("harmless peer body")
    opened = []
    original = builtins.open

    def observe(path, *args, **kwargs):
        if str(path) == str(peer):
            opened.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", observe)
    wakes = await poll(d, peer.as_uri())
    assert (opened, wakes) == ([], []), (opened, wakes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["ftp://example.test/a", "gopher://example.test/a", "data:text/plain,fixture"]
)
async def test_persisted_nonsupported_scheme_refused_before_fetch(daemon, monkeypatch, url):
    d = daemon("off")
    fetched = []

    def opener(request, **kwargs):
        fetched.append(request.full_url)
        return addinfourl(io.BytesIO(b"harmless forbidden response"), Message(), url, 200)

    monkeypatch.setattr(urllib.request, "urlopen", opener)
    wakes = await poll(d, url)
    assert (fetched, wakes) == ([], []), (fetched, wakes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "destination",
    [
        "file:///fixture-peer.txt",
        "http://127.0.0.1/x",
        "http://169.254.169.254/x",
        "http://10.0.0.7/x",
        "http://[::1]/x",
        "https://example.test/final",
    ],
)
@pytest.mark.parametrize("redirect", [False, True], ids=["direct", "redirect"])
async def test_deferred_fetch_destination_and_redirect_guard(
    daemon,
    monkeypatch,
    destination,
    redirect,
):
    d = daemon("off")
    if destination.startswith("file:"):
        peer = d.root / "peer" / "destination-fixture.txt"
        peer.write_text("harmless peer redirect fixture")
        destination = peer.as_uri()
    start = "https://example.test/start" if redirect else destination
    fetched = []

    def dns(host, port, *args, **kwargs):
        address = "93.184.216.34" if host == "example.test" else host
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", dns)

    def network(handler, connection, request, **kwargs):
        del handler
        url = request.full_url
        fetched.append(url)
        headers = Message()
        code = 200
        if redirect and url == start:
            headers["Location"] = destination
            code = 302
        response = addinfourl(io.BytesIO(b"harmless response"), headers, url, code)
        response.msg = "Found" if code == 302 else "OK"
        return response

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", network)
    # No ambient proxy or opener may redirect the scratch fetch externally.
    monkeypatch.setattr(
        urllib.request, "_opener", urllib.request.build_opener(urllib.request.ProxyHandler({}))
    )
    wakes = await poll(d, start)
    if destination == "https://example.test/final":
        assert destination in fetched and len(wakes) == 1, (fetched, wakes)
    else:
        assert destination not in fetched and not wakes, (fetched, wakes)
