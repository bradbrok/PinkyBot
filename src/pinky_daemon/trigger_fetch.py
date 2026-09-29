"""HTTP-only URL watcher transport with operator-scoped exceptions on every hop.

A private opener avoids ambient proxies/handlers. Connections use the validated
DNS sockaddr directly, preserving the URL hostname for Host and TLS verification.
"""
from __future__ import annotations

import http.client
import ipaddress
import logging
import os
import socket
import urllib.parse
import urllib.request
from functools import lru_cache

_logger = logging.getLogger(__name__)


class InternalDestinationRefusedError(ValueError):
    """A nonpublic sockaddr was refused; expose only its URL host and port."""

    def __init__(self, host: str, port: int):
        self.destination = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        super().__init__("URL trigger internal destination refused: " + self.destination)


@lru_cache(maxsize=1)
def _operator_allowlist(raw: str):
    """Parse literal IP/CIDR:port entries; IPv6 uses [IP/CIDR]:port.

    DNS names are intentionally not policy identities. Match the resolved socket
    addresses against operator-controlled numeric entries, without policy DNS.
    Invalid entries warn once per configuration and never broaden access.
    """
    entries = []
    for index, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry and not raw.strip():
            continue
        try:
            host, separator, port_text = entry.rpartition(":")
            if host.startswith("[") and host.endswith("]"):
                host = host[1:-1]
            elif ":" in host:
                raise ValueError("IPv6 requires brackets")
            if (not separator or not port_text.isascii() or not port_text.isdecimal()
                    or not 1 <= int(port_text) <= 65535 or "%" in host):
                raise ValueError("explicit numeric port required")
            network = ipaddress.ip_network(host)
            entries.append((network, int(port_text)))
        except ValueError:
            # Do not echo config text: malformed entries could contain credentials.
            _logger.warning("Ignoring invalid PINKY_URL_TRIGGER_ALLOW entry %d; "
                            "expected IP-or-CIDR with required port", index)
    return tuple(entries)


def _operator_allows(address, port, entries):
    for network, allowed_port in entries:
        if port != allowed_port or address not in network:
            continue
        # Broad ranges must never implicitly authorize local service endpoints.
        if (address.is_loopback or address.is_link_local) and network.num_addresses != 1:
            continue
        return True
    return False


def validate_trigger_url(url: str) -> urllib.parse.SplitResult:
    """Validate syntax without performing blocking DNS in an API handler."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(ord(c) < 33 or ord(c) == 127 for c in url)):
            raise ValueError
        _ = parsed.port
    except (ValueError, TypeError):
        raise ValueError("URL triggers require an http/https URL without credentials") from None
    return parsed


def _public_addresses(host: str, port: int):
    entries = _operator_allowlist(os.environ.get("PINKY_URL_TRIGGER_ALLOW", ""))
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise ValueError("URL trigger destination has no addresses")
    for _, _, _, _, sockaddr in addresses:
        address = ipaddress.ip_address(sockaddr[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (address.is_multicast or address.is_unspecified
                or (not address.is_global and not _operator_allows(address, sockaddr[1], entries))):
            raise InternalDestinationRefusedError(host, port)
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
    """Open an approved HTTP(S) URL; validate persisted rows and every redirect."""
    _validate_destination(url)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _PublicHTTPHandler(), _PublicHTTPSHandler(),
        _PublicRedirectHandler(),
    )
    return opener.open(urllib.request.Request(url, method=method), timeout=timeout)
