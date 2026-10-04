"""Bounded roster HTTP policy with synthetic responses and no network."""

from __future__ import annotations

import importlib
import importlib.util
import ssl
import urllib.error
import urllib.request
from email.message import Message

import pytest

from pinky_daemon.model_roster import MAX_BYTES
from tests._model_roster_local import document, encode

URL = "https://raw.githubusercontent.com/example/catalog/main/models.json"
FINAL = "https://pinkybot.ai/catalog/models.json"


def sync_module():
    name = "pinky_daemon.model_roster_sync"
    assert importlib.util.find_spec(name) is not None, "Bounded roster sync module is required"
    return importlib.import_module(name)


def required(obj, name):
    value = getattr(obj, name, None)
    assert callable(value), f"Roster sync must provide {name}"
    return value


def result(raw, url=URL):
    return required(sync_module(), "RosterFetchResult")(document=raw, url=url)


def make_service(registry, **kwargs):
    return required(sync_module(), "ModelRosterSync")(registry, **kwargs)


def error_type():
    cls = required(sync_module(), "RosterSyncError")
    assert isinstance(cls, type) and issubclass(cls, Exception)
    return cls


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Response:
    def __init__(self, body=b"", *, status=200, headers=(), clock=None, step=0, chunk=None):
        self.status = status
        self.headers = Message()
        for key, value in headers:
            self.headers[key] = value
        self.body = body
        self.position = 0
        self.read_sizes = []
        self.closed = False
        self.clock = clock
        self.step = step
        self.chunk = chunk

    def getcode(self):
        return self.status

    def read1(self, size):
        assert isinstance(size, int) and 0 < size <= MAX_BYTES + 1
        self.read_sizes.append(size)
        if self.clock is not None:
            self.clock.now += self.step
        size = min(size, self.chunk) if self.chunk is not None else size
        data = self.body[self.position : self.position + size]
        self.position += len(data)
        return data

    read = read1

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class Opener:
    def __init__(self, *responses, clock=None, open_step=0):
        self.responses = list(responses)
        self.opens = []
        self.clock = clock
        self.open_step = open_step

    def open(self, request, timeout):
        assert isinstance(request, urllib.request.Request)
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") is None
        assert request.get_header("Cookie") is None
        assert 0 < timeout <= 10
        self.opens.append((request.full_url, timeout))
        assert self.responses, "Unexpected HTTP contact"
        if self.clock is not None:
            self.clock.now += self.open_step
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Roster tests must inject their HTTP transport")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)


def fetch(opener, url=URL, clock=None):
    kwargs = {"opener": opener, "timeout": 10.0}
    if clock is not None:
        kwargs["monotonic"] = clock
    return required(sync_module(), "fetch_roster")(url, **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        URL,
        FINAL,
        "HTTPS://RAW.GITHUBUSERCONTENT.COM:443/example/models.json",
    ],
)
def test_allowed_url_keeps_exact_document_bytes(url):
    raw = encode(document()) + b"\n "
    response = Response(raw, headers=[("Content-Type", "text/plain")])
    opener = Opener(response)
    value = fetch(opener, url)
    assert value.document == raw and value.url == url
    assert len(opener.opens) == 1 and response.closed
    assert response.read_sizes and response.position == len(raw)
    with pytest.raises((AttributeError, TypeError)):
        value.document = b"replacement"


@pytest.mark.parametrize(
    "url",
    [
        "http://pinkybot.ai/models.json",
        "https://example.invalid/models.json",
        "https://pinkybot.ai.example.invalid/a",
        "https://pinkybot.ai./a",
        "https://user@pinkybot.ai/a",
        "https://user:secret@pinkybot.ai/a",
        "https://pinkybot.ai:444/a",
        "https://pinkybot.ai:bad/a",
        "https://pinkybot.ai:99999/a",
        "https://[broken/a",
        "https:///a",
        "https://pinkybot.ai/a#fragment",
        "https://pinkybot.ai/a\\b",
        " https://pinkybot.ai/a",
        "https://pinkybot.ai/a\n",
        "https://pinkybot.ai/a\t",
        "https://pinkybot.ai/a b",
        "",
        "file:///models.json",
    ],
)
def test_invalid_initial_url_is_refused_before_contact(url):
    opener = Opener()
    with pytest.raises(error_type()):
        fetch(opener, url)
    assert opener.opens == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [FINAL, "../next.json"])
def test_one_validated_redirect_never_reads_redirect_body(status, location):
    redirect = Response(b"discard", status=status, headers=[("Location", location)])
    final = Response(encode(document()))
    opener = Opener(redirect, final)
    value = fetch(opener)
    expected = urllib.parse.urljoin(URL, location)
    assert [url for url, _ in opener.opens] == [URL, expected]
    assert value.url == expected and value.document == final.body
    assert redirect.read_sizes == [] and redirect.closed and final.closed


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Location", "")],
        [("Location", "http://pinkybot.ai/a")],
        [("Location", "https://example.invalid/a")],
        [("Location", "https://user@pinkybot.ai/a")],
        [("Location", FINAL), ("Location", FINAL)],
    ],
)
def test_bad_redirect_is_refused_without_second_contact(headers):
    response = Response(b"private body", status=302, headers=headers)
    opener = Opener(response)
    with pytest.raises(error_type()):
        fetch(opener)
    assert len(opener.opens) == 1
    assert response.closed and response.read_sizes == []


def test_second_redirect_is_refused_before_third_contact():
    first = Response(status=302, headers=[("Location", FINAL)])
    second = Response(status=307, headers=[("Location", URL)])
    opener = Opener(first, second)
    with pytest.raises(error_type()):
        fetch(opener)
    assert len(opener.opens) == 2 and first.closed and second.closed
    assert not first.read_sizes and not second.read_sizes


def test_redirect_http_error_from_disabled_handler_is_followed_manually():
    redirect = Response(b"discard", status=302, headers=[("Location", FINAL)])
    raised = urllib.error.HTTPError(URL, 302, "redirect", redirect.headers, redirect)
    final = Response(encode(document()))
    opener = Opener(raised, final)
    value = fetch(opener)
    assert value.url == FINAL and value.document == final.body
    assert [url for url, _ in opener.opens] == [URL, FINAL]
    assert redirect.closed and not redirect.read_sizes and final.closed


@pytest.mark.parametrize("status", [201, 204, 304, 400, 404, 500])
def test_only_200_is_accepted_without_reading_error_body(status):
    response = Response(b"secret response", status=status)
    opener = Opener(response)
    with pytest.raises(error_type()):
        fetch(opener)
    assert response.closed and not response.read_sizes


@pytest.mark.parametrize("headers", [[], [("Content-Length", "1")]])
def test_cap_plus_one_stops_even_without_truthful_content_length(headers):
    response = Response(b"x" * (MAX_BYTES + 100), headers=headers, chunk=4096)
    with pytest.raises(error_type()):
        fetch(Opener(response))
    assert response.position == MAX_BYTES + 1
    assert response.closed and sum(response.read_sizes) >= response.position


def test_exact_byte_cap_requires_eof_and_accepts_valid_padded_document():
    raw = encode(document())
    raw += b" " * (MAX_BYTES - len(raw))
    response = Response(raw, headers=[("Content-Length", str(MAX_BYTES))])
    value = fetch(Opener(response))
    assert value.document == raw and response.closed
    assert response.read_sizes[-1] == 1, "A full cap needs an extra-byte EOF probe"


@pytest.mark.parametrize("length", ["-1", "invalid", str(MAX_BYTES + 1), "1.5"])
def test_bad_declared_length_rejects_before_body(length):
    response = Response(b"{}", headers=[("Content-Length", length)])
    with pytest.raises(error_type()):
        fetch(Opener(response))
    assert response.closed and not response.read_sizes


def test_declared_body_truncation_is_refused():
    response = Response(b"{}", headers=[("Content-Length", "20")])
    with pytest.raises(error_type()):
        fetch(Opener(response))
    assert response.closed and response.position == 2


@pytest.mark.parametrize("encoding", ["gzip", "br", "deflate", "identity, gzip"])
def test_nonidentity_encoding_is_refused_before_body(encoding):
    response = Response(b"encoded", headers=[("Content-Encoding", encoding)])
    with pytest.raises(error_type()):
        fetch(Opener(response))
    assert response.closed and not response.read_sizes


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("transport secret"),
        ssl.SSLCertVerificationError("TLS secret"),
        TimeoutError("timeout secret"),
    ],
)
def test_transport_failures_are_typed_and_do_not_expose_exception_text(failure):
    with pytest.raises(error_type()) as caught:
        fetch(Opener(failure))
    assert "secret" not in str(caught.value)


def test_budget_is_shared_across_redirect_and_body():
    clock = Clock()
    redirect = Response(status=302, headers=[("Location", FINAL)])
    final = Response(encode(document()), clock=clock, step=3, chunk=1)
    opener = Opener(redirect, final, clock=clock, open_step=3)
    with pytest.raises(error_type()):
        fetch(opener, clock=clock)
    assert opener.opens[0][1] == 10 and opener.opens[1][1] == 7
    assert final.position <= 2 and redirect.closed and final.closed


def test_default_opener_verifies_tls_and_disables_automatic_redirects(monkeypatch):
    raw = encode(document())
    response = Response(raw)
    observations = []

    def inspect_open(opener, request, timeout):
        https = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
        context = https._context or ssl.create_default_context()
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        redirect = next(
            h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
        )
        try:
            target = redirect.redirect_request(request, response, 302, "redirect", Message(), FINAL)
        except urllib.error.HTTPError:
            target = None
        assert target is None, "Redirects must be validated manually before contact"
        observations.append(request.full_url)
        return response

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", inspect_open)
    value = required(sync_module(), "fetch_roster")(URL)
    assert value.document == raw and observations == [URL] and response.closed
