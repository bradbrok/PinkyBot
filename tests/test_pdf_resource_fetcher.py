"""PDF rendering loads only inline data: resources."""
from __future__ import annotations

import base64
import http.server
import re
import sys
import threading
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pinky_daemon import research_export
from pinky_daemon.routes.presentations import router

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
DATA_PNG = "data:image/png;base64," + base64.b64encode(PNG).decode()


class _NewFetcher:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _fake_weasyprint(monkeypatch, urls):
    built = []

    class PDF:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def write_pdf(self, path):
            Path(path).write_bytes(b"%PDF synthetic")

    monkeypatch.setitem(sys.modules, "weasyprint", SimpleNamespace(HTML=PDF))
    monkeypatch.setitem(sys.modules, "weasyprint.urls", urls)
    return built


def test_current_weasyprint_gets_a_data_only_fetcher_without_redirects(monkeypatch):
    _fake_weasyprint(monkeypatch, SimpleNamespace(URLFetcher=_NewFetcher))
    fetcher = research_export._pdf_url_fetcher()
    assert isinstance(fetcher, _NewFetcher)
    assert fetcher.kwargs == {"allowed_protocols": {"data"}, "allow_redirects": False}


@pytest.mark.parametrize("url", [
    "file:///etc/hosts",
    "FILE:///etc/hosts",
    "http://127.0.0.1:8888/agents",
    "https://example.com/logo.png",
    "ftp://example.com/x",
    "/relative/path.png",
    "",
])
def test_older_weasyprint_refuses_everything_but_data(monkeypatch, url):
    calls = []
    _fake_weasyprint(monkeypatch, SimpleNamespace(
        default_url_fetcher=lambda *a, **k: calls.append(a) or {"string": b""}))
    fetch = research_export._pdf_url_fetcher()
    with pytest.raises(ValueError):
        fetch(url)
    assert calls == []


def test_older_weasyprint_passes_data_uris_through(monkeypatch):
    calls = []
    _fake_weasyprint(monkeypatch, SimpleNamespace(
        default_url_fetcher=lambda url, *a, **k: calls.append(url) or {"string": PNG}))
    fetch = research_export._pdf_url_fetcher()
    assert fetch(DATA_PNG, timeout=5) == {"string": PNG}
    assert calls == [DATA_PNG]


def test_missing_weasyprint_raises_import_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    monkeypatch.setitem(sys.modules, "weasyprint.urls", None)
    with pytest.raises(ImportError):
        research_export._pdf_url_fetcher()


def test_render_pdf_route_uses_the_restricted_fetcher(tmp_path, monkeypatch):
    built = _fake_weasyprint(monkeypatch, SimpleNamespace(URLFetcher=_NewFetcher))
    monkeypatch.setattr(research_export, "EXPORT_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/render/pdf", json={"content": "# Report", "filename": "r"})
    assert response.status_code == 200, response.text
    assert len(built) == 1
    assert built[0]["url_fetcher"].kwargs["allowed_protocols"] == {"data"}


def test_render_pdf_route_still_reports_missing_weasyprint(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    monkeypatch.setitem(sys.modules, "weasyprint.urls", None)
    export = tmp_path / "exports"
    monkeypatch.setattr(research_export, "EXPORT_DIR", str(export))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/render/pdf", json={"content": "# Report", "filename": "r"})
    assert response.status_code == 503
    assert not export.exists() or list(export.iterdir()) == []


def test_research_export_uses_the_restricted_fetcher(tmp_path, monkeypatch):
    built = _fake_weasyprint(monkeypatch, SimpleNamespace(URLFetcher=_NewFetcher))
    monkeypatch.setattr(research_export, "EXPORT_DIR", str(tmp_path))
    topic = {"id": 7, "title": "Topic", "status": "published"}
    brief = {"content": "Body", "summary": "", "key_findings": "", "sources": ""}
    path = research_export.export_brief_pdf(topic, brief)
    assert Path(path).exists()
    assert len(built) == 1
    assert built[0]["url_fetcher"].kwargs["allowed_protocols"] == {"data"}


# Real rendering, where WeasyPrint and its system libraries load (optional; skipped otherwise).

def _weasyprint_urls():
    try:
        import weasyprint.urls as urls
    except (ImportError, OSError) as exc:  # OSError: WeasyPrint present, pango missing
        pytest.skip(f"WeasyPrint unavailable: {type(exc).__name__}")
    return urls


@pytest.fixture
def local_server():
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            body = PNG if self.path.endswith(".png") else b"body { color: red }"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base=f"http://127.0.0.1:{server.server_port}", hits=hits)
    finally:
        server.shutdown()
        server.server_close()


def _content(base, local_file):
    # Raw HTML: the markdown converter passes it through (with or without the markdown package).
    return (
        "# Report\n\n"
        f'<link rel="stylesheet" href="{base}/style.css">\n\n'
        f'<p><img src="{base}/remote.png"></p>\n\n'
        f'<p><img src="{local_file.as_uri()}"></p>\n\n'
        f'<p><a rel="attachment" href="{base}/notes.txt">notes</a></p>\n\n'
        f'<p><a rel="attachment" href="{local_file.as_uri()}">local</a></p>\n\n'
        f'<p><img src="{DATA_PNG}"></p>\n'
    )


def _pdf_objects(pdf):
    """The PDF bytes plus every Flate stream decompressed (object streams hide dictionaries)."""
    parts = [pdf]
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            parts.append(zlib.decompress(match.group(1)))
        except zlib.error:
            pass
    return b"\n".join(parts)


def _render(content, url_fetcher, out):
    from weasyprint import HTML

    html = research_export._HTML_TEMPLATE.format(
        title="Report", body=research_export._markdown_to_html(content))
    HTML(string=html, url_fetcher=url_fetcher).write_pdf(str(out))
    return out.read_bytes()


def _render_restricted(tmp_path, local_server):
    local_file = tmp_path / "local.txt"
    local_file.write_text("local file body\n")
    pdf = _render(_content(local_server.base, local_file),
                  research_export._pdf_url_fetcher(), tmp_path / "out.pdf")
    objects = _pdf_objects(pdf)
    assert pdf.startswith(b"%PDF")
    assert local_server.hits == []
    assert b"/EmbeddedFile" not in objects
    assert b"local file body" not in objects
    images = objects.count(b"/Subtype /Image")
    assert images > 0  # the data: image still renders
    return images


def test_real_render_loads_inline_images_only(tmp_path, local_server):
    _weasyprint_urls()
    _render_restricted(tmp_path, local_server)


def test_real_render_control_default_fetcher_does_load_them(tmp_path, local_server):
    """Positive control: the same document with WeasyPrint's default fetcher loads them all."""
    urls = _weasyprint_urls()
    (tmp_path / "restricted").mkdir()
    restricted_images = _render_restricted(tmp_path / "restricted", local_server)
    local_file = tmp_path / "local.txt"
    local_file.write_text("local file body\n")
    default = urls.URLFetcher() if hasattr(urls, "URLFetcher") else None
    pdf = _render(_content(local_server.base, local_file), default, tmp_path / "control.pdf")
    objects = _pdf_objects(pdf)
    assert sorted(local_server.hits) == ["/notes.txt", "/remote.png", "/style.css"]
    assert b"/EmbeddedFile" in objects
    assert b"local file body" in objects
    # Twice the pictures: the remote PNG renders too (each PNG also writes its alpha mask).
    assert objects.count(b"/Subtype /Image") == 2 * restricted_images
