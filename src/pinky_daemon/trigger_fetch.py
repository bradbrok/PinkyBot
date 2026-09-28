"""HTTP-only URL watcher transport with public destinations on every hop.

A private opener avoids ambient proxies/handlers. Connections use the validated
DNS sockaddr directly, preserving the URL hostname for Host and TLS verification.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.parse
import urllib.request


def validate_trigger_url(url: str) -> urllib.parse.SplitResult:
    """Validate syntax without performing blocking DNS in an API handler."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 33 for c in url)):
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError):
        raise ValueError("URL triggers require an http/https URL without credentials") from None
    return parsed


def _public_addresses(host: str, port: int):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("URL trigger destination has no addresses")
    for _, _, _, _, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise ValueError("URL trigger destination must be a public address")
    return addresses


def _validate_destination(url: str) -> None:
    parsed = validate_trigger_url(url)
    _public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))


def _public_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
    """Resolve once, validate every answer, and connect without another lookup."""
    host, port = address
    addresses = _public_addresses(host, port)
    last_error = None
    for family, kind, proto, _, sockaddr in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
    raise last_error or OSError("URL trigger connection failed")


def _connection_factory(connection_type):
    def connect(host, **kwargs):
        connection = connection_type(host, **kwargs)
        connection._create_connection = _public_connection
        return connection
    return connect


class _PublicHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(_connection_factory(http.client.HTTPConnection), request)


class _PublicHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_connection_factory(http.client.HTTPSConnection), request,
                            context=self._context)


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # ValueError is intentional: an unsafe destination is a refused fetch,
        # never an HTTP status that could satisfy a watcher's condition.
        _validate_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

    def http_error_302(self, req, fp, code, msg, headers):
        # urllib rejects some non-HTTP schemes with HTTPError before invoking
        # redirect_request. Validate first so these cannot fire status watchers.
        destination = headers.get("location") or headers.get("uri")
        if destination:
            try:
                _validate_destination(urllib.parse.urljoin(req.full_url, destination))
            except ValueError:
                fp.close()
                raise
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


def open_trigger_url(url: str, *, method: str = "GET", timeout: float = 5):
    """Open a public HTTP(S) URL; reject unsafe persisted rows and redirects."""
    _validate_destination(url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(),
        _PublicRedirectHandler(),
    )
    return opener.open(urllib.request.Request(url, method=method), timeout=timeout)
