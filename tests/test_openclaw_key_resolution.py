"""Key resolution for the OpenClaw voice pipeline.

The daemon does not always go through __main__._load_dotenv() on restart, so the
API keys must be resolvable at call time by reading ~/.pinkybot/.env directly —
not only from os.environ at module-import time.
"""

import pytest

from pinky_daemon import openclaw_gateway as gw


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setattr(gw, "_ENV_FILES", [str(path)])
    return path


def test_prefers_environ(env_file, monkeypatch):
    env_file.write_text("DEEPGRAM_API_KEY=from-file\n")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from-environ")
    assert gw._load_deepgram_key() == "from-environ"


def test_falls_back_to_env_file(env_file, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    env_file.write_text("PINKY_SESSION_SECRET=x\nDEEPGRAM_API_KEY=dg-secret\n")
    assert gw._load_deepgram_key() == "dg-secret"


def test_strips_quotes_and_whitespace(env_file, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    env_file.write_text('DEEPGRAM_API_KEY="dg-quoted"  \n')
    assert gw._load_deepgram_key() == "dg-quoted"


def test_missing_key_returns_empty(env_file, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    env_file.write_text("OPENAI_API_KEY=other\n")
    assert gw._load_deepgram_key() == ""


def test_missing_env_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(gw, "_ENV_FILES", [str(tmp_path / "nope.env")])
    assert gw._load_deepgram_key() == ""


def test_openai_key_uses_same_resolver(env_file, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env_file.write_text("OPENAI_API_KEY=oa-secret\n")
    assert gw._load_openai_key() == "oa-secret"


@pytest.mark.asyncio
async def test_stt_raises_when_no_key_anywhere(tmp_path, monkeypatch):
    """_stt_deepgram must not depend on the module-level constant alone."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(gw, "_ENV_FILES", [str(tmp_path / "nope.env")])
    monkeypatch.setattr(gw, "DEEPGRAM_API_KEY", "")
    with pytest.raises(RuntimeError, match="DEEPGRAM_API_KEY not configured"):
        await gw._stt_deepgram(b"\x00\x00")


@pytest.mark.asyncio
async def test_stt_recovers_key_from_env_file(env_file, monkeypatch):
    """Import-time constant empty (restart path skipped _load_dotenv) → still works."""
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(gw, "DEEPGRAM_API_KEY", "")
    env_file.write_text("DEEPGRAM_API_KEY=dg-secret\n")

    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"results": {"channels": [{"alternatives": [{"transcript": "ciao"}]}]}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            sent["headers"] = headers
            return _Resp()

    monkeypatch.setattr(gw.httpx, "AsyncClient", lambda **kw: _Client())
    assert await gw._stt_deepgram(b"\x00\x00") == "ciao"
    assert sent["headers"]["Authorization"] == "Token dg-secret"
