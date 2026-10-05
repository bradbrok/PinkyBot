"""Explicit historical fixtures cover every loaded production table alias."""

import sys
from types import ModuleType

import pytest


def test_reference_pricing_replaces_all_live_module_captures(request):
    from pinky_daemon import pricing
    from pinky_daemon.routes import providers

    live = pricing.RATE_TABLE
    assert providers.RATE_TABLE is live
    request.getfixturevalue("reference_pricing")
    captures = sorted(
        f"{name}.{attribute}"
        for name, module in list(sys.modules.items())
        if name.startswith("pinky_daemon.") and module is not None
        for attribute, value in list(vars(module).items())
        if value is live
    )
    assert captures == [], f"LIVE_RATE_TABLE_CAPTURE_REMAINS: {captures}"


@pytest.mark.parametrize("raises", [False, True])
def test_reference_table_captures_restore_existing_and_later_aliases(monkeypatch, raises):
    from tests._model_roster_local import reference_table_captures

    live, reference = {}, {}
    existing = ModuleType("pinky_daemon.reference_existing_test")
    later = ModuleType("pinky_daemon.reference_later_test")
    existing.renamed_table = live
    monkeypatch.setitem(sys.modules, existing.__name__, existing)
    try:
        with reference_table_captures(live, reference):
            assert existing.renamed_table is reference
            later.renamed_table = existing.renamed_table
            monkeypatch.setitem(sys.modules, later.__name__, later)
            if raises:
                raise ValueError("fixture teardown")
    except ValueError:
        assert raises
    assert existing.renamed_table is live
    assert later.renamed_table is live
