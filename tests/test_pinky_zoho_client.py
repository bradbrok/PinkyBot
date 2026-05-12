from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

from pinky_zoho.client import ZohoClient


class FakeResponse:
    def __init__(self, body: dict[str, Any] | list[Any] | str = "") -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if isinstance(self.body, str):
            return self.body.encode()
        return json.dumps(self.body).encode()


def test_client_falls_back_and_caches_first_working_host(monkeypatch) -> None:
    seen: list[str] = []

    def fake_urlopen(req: urllib.request.Request, timeout: int) -> FakeResponse:
        seen.append(req.full_url)
        if req.full_url.startswith("http://10.0.0.32"):
            raise urllib.error.URLError("down")
        return FakeResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = ZohoClient(agent_name="lera", secret="secret", timeout=1)

    assert client.get("/whoami") == {"ok": True}
    assert client.get("/health") == {"ok": True}
    assert seen == [
        "http://10.0.0.32:9100/whoami",
        "http://10.0.0.209:9100/whoami",
        "http://10.0.0.209:9100/health",
    ]


def test_client_uses_api_url_before_env_and_fallback(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setenv("ZOHO_API_URL", "http://env-host:9100")

    def fake_urlopen(req: urllib.request.Request, timeout: int) -> FakeResponse:
        seen.append(req.full_url)
        return FakeResponse({"ok": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = ZohoClient(agent_name="barsik", api_url="http://cli-host:9100", secret="secret")

    assert client.get("/crm/Contacts", limit=5) == {"ok": True}
    assert seen == ["http://cli-host:9100/crm/Contacts?limit=5"]


def test_client_builds_json_body_query_and_signed_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(req: urllib.request.Request, timeout: int) -> FakeResponse:
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = req.headers
        return FakeResponse({"created": True})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = ZohoClient(agent_name="sasha", api_url="http://zoho:9100", secret="secret")

    assert client.post("/crm/Tasks", {"data": {"Subject": "Follow up"}}) == {"created": True}
    assert captured["url"] == "http://zoho:9100/crm/Tasks"
    assert json.loads(captured["body"]) == {"data": {"Subject": "Follow up"}}
    headers = {key.lower(): value for key, value in captured["headers"].items()}
    assert headers["x-pinky-agent"] == "sasha"
    assert len(headers["x-pinky-signature"]) == 64
    assert headers["content-type"] == "application/json"


def test_client_returns_http_error_envelope_without_secret_leak(monkeypatch) -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: int) -> FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b'{"detail":"denied"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ZohoClient(agent_name="daria", api_url="http://zoho:9100", secret="top-secret")

    result = client.get("/crm/Deals")

    assert result == {"detail": "denied", "status": 403}
    assert "top-secret" not in json.dumps(result)


def test_client_sanitizes_transport_errors(monkeypatch) -> None:
    def fake_urlopen(req: urllib.request.Request, timeout: int) -> FakeResponse:
        raise urllib.error.URLError("top-secret unavailable")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ZohoClient(agent_name="olga", api_url="http://zoho:9100", secret="top-secret")

    result = client.get("/whoami")

    assert result["status"] == 0
    assert "[redacted]" in result["error"]
    assert "top-secret" not in result["error"]
