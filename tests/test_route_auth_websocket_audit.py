"""WebSocket handler authentication is separate from HTTP middleware policy."""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audit_route_auth_coverage as audit_mod  # noqa: E402

VOICE = "/ws/voice/{call_session_id}"
SETS = {
    "public_exact": set(),
    "public_prefixes": ("/assets/",),
    "protected_html": set(),
    "protected_api_prefixes": ("/system",),
}


@pytest.mark.parametrize(
    "kind,path,label",
    [
        ("WS", VOICE, "WS-handler-auth"),
        ("WS", "/ws/unknown", "UNCLASSIFIED"),
        ("HTTP", VOICE, "UNCLASSIFIED"),
        ("HTTP", "/system/health", "PROTECTED-API"),
        ("MOUNT", "/assets/", "PUBLIC-prefix"),
    ],
)
def test_route_kind_preserves_auth_boundary(kind, path, label):
    assert audit_mod.classifier(SETS)(path, kind=kind) == label


def _app(*, include_unknown):
    app = FastAPI()
    app.state.auth_route_sets = SETS

    async def handler(websocket):
        await websocket.close()

    app.add_api_websocket_route(VOICE, handler)
    if include_unknown:
        app.add_api_websocket_route("/ws/unknown", handler)
    return app


def test_audit_retains_unknown_websocket_routes(monkeypatch):
    monkeypatch.setattr(audit_mod, "build_app", lambda: _app(include_unknown=True))
    assert audit_mod.audit() == [("WS", "/ws/unknown")]


def test_cli_uses_the_same_kind_aware_classification(monkeypatch, capsys):
    monkeypatch.setattr(audit_mod, "build_app", lambda: _app(include_unknown=False))
    assert audit_mod.main() == 0
    assert "WS-handler-auth" in capsys.readouterr().out
