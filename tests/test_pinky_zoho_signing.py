from __future__ import annotations

import hashlib
import hmac

from pinky_zoho.client import normalize_path, resolve_secret, sign_headers


def _verify(secret: str, agent: str, method: str, path: str, timestamp: int, signature: str) -> bool:
    payload = f"{agent}\n{method.upper()}\n{normalize_path(path)}\n{timestamp}".encode()
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def test_sign_headers_match_server_wire_protocol() -> None:
    headers = sign_headers(
        "shared-secret",
        "lera",
        "get",
        "/crm/Contacts/search?email=a%40b.test",
        timestamp=1777777777,
    )

    assert headers["x-pinky-agent"] == "lera"
    assert headers["x-pinky-timestamp"] == "1777777777"
    assert len(headers["x-pinky-signature"]) == 64
    assert _verify(
        "shared-secret",
        "lera",
        "GET",
        "/crm/Contacts/search?email=a%40b.test",
        1777777777,
        headers["x-pinky-signature"],
    )
    assert not _verify(
        "shared-secret",
        "lera",
        "POST",
        "/crm/Contacts/search?email=a%40b.test",
        1777777777,
        headers["x-pinky-signature"],
    )


def test_signature_normalizes_query_string_out_of_path() -> None:
    with_query = sign_headers("s", "ivan", "GET", "/desk/search?q=billing", timestamp=1)
    without_query = sign_headers("s", "ivan", "GET", "/desk/search", timestamp=1)

    assert with_query == without_query


def test_secret_resolution_prefers_zoho_api_secret(monkeypatch) -> None:
    monkeypatch.setenv("ZOHO_API_SECRET", "zoho")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "session")

    assert resolve_secret() == "zoho"


def test_secret_resolution_falls_back_to_pinky_session_secret(monkeypatch) -> None:
    monkeypatch.delenv("ZOHO_API_SECRET", raising=False)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "session")

    assert resolve_secret() == "session"


def test_secret_resolution_empty_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("ZOHO_API_SECRET", raising=False)
    monkeypatch.delenv("PINKY_SESSION_SECRET", raising=False)

    assert resolve_secret() == ""
