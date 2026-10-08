"""PDF export names stay directly inside their configured directory."""
from __future__ import annotations

import errno
import os
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
    attempts = []

    class PDF:
        def __init__(self, **kwargs):
            pass

        def write_pdf(self, path):
            target = Path(path)
            attempts.append(target)
            # Actual file creation exposes a write through an existing symlink.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'%PDF synthetic')
            writes.append(target)

    monkeypatch.setattr(research_export, 'EXPORT_DIR', str(export))
    monkeypatch.setitem(sys.modules, 'weasyprint', SimpleNamespace(HTML=PDF))
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, export=export, writes=writes,
                              attempts=attempts, root=tmp_path)
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


@pytest.mark.parametrize('filename', ['a' * 256, '測' * 256, 'a' * 8192],
                         ids=['ascii-component', 'unicode-component', 'whole-path'])
def test_pdf_rejects_oversized_filename(renderer, filename):
    response = render(renderer, filename)
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.writes == []
    assert list(renderer.export.iterdir()) == []


def test_pdf_rejects_self_referential_symlink(renderer):
    target = renderer.export / 'report.pdf'
    target.symlink_to('report.pdf')
    response = render(renderer, 'report')
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.writes == []
    assert target.is_symlink()
    assert os.readlink(target) == 'report.pdf'
    assert list(renderer.export.iterdir()) == [target]


@pytest.mark.parametrize('stem', ['測' * 251, 'é' * 125, 'e\u0301' * 125, '☀️' * 80],
                         ids=['long-cjk', 'nfc', 'nfd', 'emoji'])
def test_pdf_unicode_follows_filesystem(renderer, stem, record_property):
    probe = renderer.root / 'unicode-probe'
    probe.mkdir()
    filename = stem + '.pdf'
    try:
        (probe / filename).write_bytes(b'probe')
    except OSError as error:
        assert error.errno == errno.ENAMETOOLONG
        expected_status = 400
    else:
        expected_status = 200
    record_property('filesystem_expected_status', expected_status)
    response = render(renderer, stem)
    assert response.status_code == expected_status, response.text
    if expected_status == 200:
        expected = os.path.abspath(os.path.join(str(renderer.export), filename))
        assert response.json() == {'success': True, 'path': expected, 'filename': filename}
        assert renderer.writes == [Path(expected)]
        assert Path(expected).read_bytes() == b'%PDF synthetic'
    else:
        assert response.json() == {'detail': 'invalid filename'}
        assert renderer.writes == []
        assert list(renderer.export.iterdir()) == []


def test_pdf_accepts_in_directory_case_alias(renderer, record_property):
    probe = renderer.root / 'case-probe'
    probe.write_bytes(b'case detection')
    alias = renderer.root / 'CASE-PROBE'
    insensitive = alias.exists() and os.path.samefile(probe, alias)
    record_property('filesystem_case_insensitive', insensitive)
    if not insensitive:
        pytest.skip(f'scratch filesystem case-insensitive detection: {insensitive}')
    original = renderer.export / 'original.pdf'
    original.write_bytes(b'original')
    target = renderer.export / 'report.pdf'
    target.symlink_to(renderer.export.with_name('EXPORTS') / original.name)
    assert target.resolve().parent != renderer.export.resolve()
    assert os.path.samefile(target.resolve().parent, renderer.export)
    response = render(renderer, 'report')
    expected = os.path.abspath(os.path.join(str(renderer.export), 'report.pdf'))
    assert response.status_code == 200, response.text
    assert response.json() == {'success': True, 'path': expected, 'filename': 'report.pdf'}
    assert renderer.writes == [Path(expected)]
    assert original.read_bytes() == b'%PDF synthetic'


def test_pdf_preserves_in_directory_symlink_path(renderer):
    original = renderer.export / 'original.pdf'
    original.write_bytes(b'original')
    target = renderer.export / 'report.pdf'
    target.symlink_to(original)
    response = render(renderer, 'report')
    expected = os.path.abspath(os.path.join(str(renderer.export), 'report.pdf'))
    assert response.status_code == 200, response.text
    assert response.json() == {'success': True, 'path': expected, 'filename': 'report.pdf'}
    assert renderer.writes == [Path(expected)]
    assert original.read_bytes() == b'%PDF synthetic'


@pytest.mark.parametrize('relative', [False, True], ids=['absolute', 'relative'])
def test_pdf_preserves_symlinked_parent_spelling(renderer, monkeypatch, relative):
    parent = renderer.root / 'parent-alias'
    parent.symlink_to(renderer.root, target_is_directory=True)
    monkeypatch.chdir(renderer.root)
    configured = os.path.join('parent-alias' if relative else str(parent), 'exports')
    monkeypatch.setattr(research_export, 'EXPORT_DIR', configured)
    response = render(renderer, 'report')
    expected = os.path.abspath(os.path.join(configured, 'report.pdf'))
    assert response.status_code == 200, response.text
    assert response.json() == {'success': True, 'path': expected, 'filename': 'report.pdf'}
    assert renderer.writes == [Path(expected)]
    assert Path(expected).read_bytes() == b'%PDF synthetic'


def test_pdf_creates_export_directory(renderer, monkeypatch):
    export = renderer.root / 'new' / 'exports'
    monkeypatch.setattr(research_export, 'EXPORT_DIR', str(export))
    response = render(renderer, 'report')
    assert response.status_code == 200, response.text
    assert renderer.writes == [export / 'report.pdf']
    assert (export / 'report.pdf').read_bytes() == b'%PDF synthetic'


def test_pdf_rejects_symlink_into_nested_directory(renderer):
    nested = renderer.export / 'nested'
    nested.mkdir()
    original = nested / 'original.pdf'
    original.write_bytes(b'preserve original')
    (renderer.export / 'report.pdf').symlink_to(original)
    response = render(renderer, 'report')
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.writes == []
    assert original.read_bytes() == b'preserve original'


def test_pdf_other_write_errors_remain_server_errors(renderer):
    target = renderer.export / 'report.pdf'
    target.mkdir()
    response = render(renderer, 'report')
    assert response.status_code == 500, response.text
    assert response.json()['detail'].startswith('PDF rendering failed: ')
    assert renderer.attempts == [target]
    assert renderer.writes == []
    assert list(target.iterdir()) == []


def test_pdf_rejects_oversized_export_directory(renderer, monkeypatch):
    monkeypatch.setattr(research_export, 'EXPORT_DIR', str(renderer.root / ('a' * 256)))
    response = render(renderer, 'report')
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.writes == []
    assert renderer.attempts == []


def test_pdf_rejects_symlink_into_missing_outside_directory(renderer):
    outside = renderer.root / 'missing' / 'original.pdf'
    target = renderer.export / 'report.pdf'
    target.symlink_to(outside)
    response = render(renderer, 'report')
    assert response.status_code == 400, response.text
    assert response.json() == {'detail': 'invalid filename'}
    assert renderer.attempts == []
    assert renderer.writes == []
    assert not outside.parent.exists()
    assert target.is_symlink()
    assert os.readlink(target) == str(outside)
