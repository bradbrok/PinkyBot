"""PDF export names stay directly inside their configured directory."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pinky_daemon import research_export
from pinky_daemon.routes.presentations import router


@pytest.fixture
def renderer(tmp_path, monkeypatch):
    export = tmp_path / 'exports'
    export.mkdir()
    writes = []

    class PDF:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self, path):
            target = Path(path)
            writes.append(target)
            # Actual file creation exposes a write through an existing symlink.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'%PDF synthetic')

    monkeypatch.setattr(research_export, 'EXPORT_DIR', str(export))
    monkeypatch.setitem(sys.modules, 'weasyprint', SimpleNamespace(HTML=PDF))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, export=export, writes=writes, root=tmp_path)
    finally:
        client.close()


def render(renderer, filename):
    return renderer.client.post('/render/pdf', json={'content': 'Example', 'filename': filename})


@pytest.mark.parametrize('filename,normalized', [
    ('report', 'report.pdf'), ('a/b/c.pdf', 'c.pdf'), ('../x', 'x.pdf'),
    ('report.pdf.pdf', 'report.pdf'), (' report.PDF ', 'report.pdf'),
])
def test_pdf_normalizes_filename(renderer, filename, normalized):
    response = render(renderer, filename)
    assert response.status_code == 200, response.text
    assert response.json()['filename'] == normalized
    expected = (renderer.export / normalized).resolve()
    assert Path(response.json()['path']) == expected
    assert renderer.writes == [expected]
    assert expected.read_bytes() == b'%PDF synthetic'


def test_pdf_absolute_filename_stays_in_export_directory(renderer):
    outside = renderer.root / 'outside.pdf'
    response = render(renderer, str(outside))
    assert response.status_code in (200, 400), response.text
    assert not outside.exists()
    if response.status_code == 200:
        assert response.json()['filename'] == 'outside.pdf'
        assert renderer.writes == [(renderer.export / 'outside.pdf').resolve()]
    else:
        assert renderer.writes == []


@pytest.mark.parametrize('filename', ['', ' ', '.', '..', 'a/', 'a/..', 'a/   ', '.pdf', None, 7])
def test_pdf_rejects_empty_or_invalid_name(renderer, filename):
    response = render(renderer, filename)
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.writes == []


def test_pdf_rejects_export_symlink_to_outside(renderer):
    outside = renderer.root / 'outside.pdf'
    outside.write_bytes(b'preserve original')
    (renderer.export / 'linked.pdf').symlink_to(outside)
    response = render(renderer, 'linked.pdf')
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert outside.read_bytes() == b'preserve original'
    assert renderer.writes == []
