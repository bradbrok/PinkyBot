"""Tests for PluginManager lifecycle: enable() guards and validation."""

from __future__ import annotations

from pinky_daemon.plugin_manager import PluginManager, PluginState


def _write_plugin(root, name: str, *, requires: list[str] | None = None) -> None:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    manifest = f"name: {name}\ndescription: test plugin\n"
    if requires:
        manifest += "requires:\n" + "".join(f"  - {r}\n" for r in requires)
    (plugin_dir / "plugin.yaml").write_text(manifest)
    (plugin_dir / "__init__.py").write_text(
        "from pathlib import Path\n"
        "\n"
        "def setup(ctx):\n"
        "    marker = Path(__file__).parent / 'setup_calls.txt'\n"
        "    with open(marker, 'a') as f:\n"
        "        f.write('x')\n"
    )


def _setup_calls(root, name: str) -> int:
    marker = root / name / "setup_calls.txt"
    if not marker.exists():
        return 0
    return len(marker.read_text())


def _manager(tmp_path) -> PluginManager:
    return PluginManager(db_path=str(tmp_path / "plugins.db"), working_dir=str(tmp_path))


class TestEnable:
    def test_enable_runs_setup_once(self, tmp_path):
        root = tmp_path / "plugins"
        _write_plugin(root, "myplug")
        mgr = _manager(tmp_path)
        mgr.discover(root)

        assert mgr.enable("myplug") is True
        assert mgr.get("myplug").state == PluginState.ACTIVE
        assert _setup_calls(root, "myplug") == 1

    def test_enable_on_active_plugin_is_a_noop(self, tmp_path):
        # Re-enabling an ACTIVE plugin must not re-exec the module / re-run
        # setup(ctx) (duplicate hooks, tools, schedules) nor replace the live
        # PluginContext (leaking its open sqlite connection).
        root = tmp_path / "plugins"
        _write_plugin(root, "myplug")
        mgr = _manager(tmp_path)
        mgr.discover(root)

        assert mgr.enable("myplug") is True
        ctx = mgr._contexts["myplug"]
        module = mgr.get("myplug").module

        assert mgr.enable("myplug") is True
        assert _setup_calls(root, "myplug") == 1
        assert mgr._contexts["myplug"] is ctx
        assert mgr.get("myplug").module is module

    def test_enable_revalidates_error_state(self, tmp_path):
        # A plugin that failed validation (missing dependency) must not load
        # until validation actually passes.
        root = tmp_path / "plugins"
        _write_plugin(root, "needy", requires=["base"])
        mgr = _manager(tmp_path)
        mgr.discover(root)

        assert mgr.validate("needy") is False
        assert mgr.get("needy").state == PluginState.ERROR

        assert mgr.enable("needy") is False
        assert mgr.get("needy").state == PluginState.ERROR
        assert "needy" not in mgr._contexts
        assert _setup_calls(root, "needy") == 0

    def test_enable_recovers_once_error_cause_is_fixed(self, tmp_path):
        root = tmp_path / "plugins"
        more = tmp_path / "plugins-more"
        _write_plugin(root, "needy", requires=["base"])
        _write_plugin(more, "base")
        mgr = _manager(tmp_path)
        mgr.discover(root)

        assert mgr.enable("needy") is False  # dependency missing -> ERROR

        mgr.discover(more)  # dependency appears; enable re-validates
        assert mgr.enable("needy") is True
        assert mgr.get("needy").state == PluginState.ACTIVE
        assert _setup_calls(root, "needy") == 1

    def test_disable_then_enable_runs_setup_again(self, tmp_path):
        root = tmp_path / "plugins"
        _write_plugin(root, "myplug")
        mgr = _manager(tmp_path)
        mgr.discover(root)

        assert mgr.enable("myplug") is True
        assert mgr.disable("myplug") is True
        assert mgr.get("myplug").state == PluginState.DISABLED
        assert mgr.enable("myplug") is True
        assert _setup_calls(root, "myplug") == 2
