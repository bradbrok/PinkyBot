"""Application registry bindings must not escape a test's lifetime."""

from pinky_daemon.api import create_api
from pinky_daemon.pricing import lookup_rate
from tests._model_roster_local import unused_model_id


def test_app_registry_can_close_at_test_end(tmp_path):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    app.state.agents.close()


def test_pricing_after_closed_app_uses_unbound_catalog():
    # Runs after the app test above; no new app may hide a stale catalog binding.
    assert lookup_rate(unused_model_id("unlisted-isolation-test-model")) is None
