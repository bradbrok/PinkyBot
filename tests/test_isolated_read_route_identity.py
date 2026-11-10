"""Read ownership follows the resolved parameterized route."""

import pytest

from tests.isolated_policy_support import daemon as daemon
from tests.test_isolated_read_enforce import _read

pytestmark = pytest.mark.real_auth


@pytest.mark.parametrize("name", ["retired", "presence"])
def test_literal_caller_name_cannot_authorize_a_static_collection(daemon, monkeypatch, name):
    instance = daemon()
    work = instance.root / name
    work.mkdir()
    instance.agents.register(name, isolated=True, working_dir=str(work))
    instance.agents.update("peer", soul="private-retired-fixture")
    if name == "retired":
        assert instance.agents.retire("peer")
    monkeypatch.setenv("PINKY_ISOLATED_READ_POLICY_MODE", "enforce")
    response = _read(instance, "/agents/" + name, name=name)
    assert response.status_code == 403
