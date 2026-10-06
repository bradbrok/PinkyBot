"""Run fixture-only hooks inside the standalone attachment copier."""

from pathlib import Path

from pinky_daemon import isolated_files


def child_hook(monkeypatch, tmp_path, code):
    original = getattr(
        isolated_files, "_COPY_CHILD",
        Path(isolated_files.__file__).with_name("isolated_media_copy.py"),
    )
    wrapper = tmp_path / "fixture_copy.py"
    wrapper.write_text(
        "import runpy\n"
        f"module = runpy.run_path({str(original)!r}, run_name='fixture_copy')\n"
        "env = module['main'].__globals__\n"
        + code + "\nmodule['main']()\n",
    )
    monkeypatch.setattr(isolated_files, "_COPY_CHILD", wrapper, raising=False)


def before_child(monkeypatch, callback):
    original = getattr(isolated_files, "_copy_media", None)

    async def copy(payload):
        callback(payload)
        return await original(payload)

    monkeypatch.setattr(isolated_files, "_copy_media", copy, raising=False)
