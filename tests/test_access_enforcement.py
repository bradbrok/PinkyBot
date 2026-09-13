"""Tests for the pure-logic access enforcement layer (task #346).

These pin the FAIL-CLOSED policy of the agent tool/data layer: staff must not
reach a tool or system they were not granted just by asking an agent. The model
here is an inline fixture that mirrors the real ``permissions.json`` shape
(departments -> groups of {name,email}; ``systems`` and ``access`` items with
``{scopes, individuals}`` rosters, a nested per-person ``edit`` roster, and a
``public`` item).
"""

from __future__ import annotations

import json

import pytest

from pinky_daemon.access_enforcement import (
    ADMINS,
    Decision,
    groups_for_email,
    is_allowed,
    is_tool_allowed,
    load_model,
    resolve_allowed_emails,
    tool_keys,
)


@pytest.fixture
def model() -> dict:
    return {
        "departments": {
            "Support": [
                {"name": "Support One", "email": "support1@example.com"},
                {"name": "Support Two", "email": "SUPPORT2@example.com"},
            ],
            "Helpdesk": [
                {"name": "Helpdesk One", "email": "helpdesk1@example.com"},
            ],
            "Sales": [
                {"name": "Sales One", "email": "sales1@example.com"},
            ],
            "SuperAdmin": [
                {"name": "Owner One", "email": "owner1@example.com"},
                {"name": "Owner Two", "email": "owner2@example.com"},
            ],
        },
        "systems": {
            "public_form": {
                "type": "use",
                "public": True,
                "use": {"scopes": [], "individuals": []},
            },
            "support_ops": {
                "type": "use",
                "use": {"scopes": [], "individuals": ["owner1@example.com"]},
            },
            "build_team": {
                "type": "editable",
                "edit_mode": "per_person",
                "view": {"scopes": ["Support"], "individuals": []},
                "edit": {
                    "all": {"scopes": [], "individuals": ["owner1@example.com"]},
                    "own": {"scopes": [], "individuals": ["support1@example.com"]},
                },
            },
        },
        "access": {
            "ops_tool": {"use": {"scopes": ["Support", "Helpdesk"], "individuals": []}},
            "restricted_report": {"use": {"scopes": ["SuperAdmin"], "individuals": []}},
            "edit_tool": {"use": {"scopes": ["Support"], "individuals": ["helpdesk1@example.com"]}},
            "empty_tool": {"use": {"scopes": [], "individuals": []}},
        },
    }


# --- admins always allowed ---------------------------------------------------


@pytest.mark.parametrize("admin", sorted(ADMINS))
def test_admins_always_allowed_even_for_empty_tool(model, admin):
    d = is_tool_allowed(model, admin, "empty_tool")
    assert d.allow is True
    assert d.reason.startswith("admin:")


def test_dev_automation_identity_is_admin(model):
    assert is_tool_allowed(model, "automation@example.com", "restricted_report").allow


def test_admin_allowed_regardless_of_case(model):
    assert is_tool_allowed(model, "Owner1@Example.com", "empty_tool").allow


# --- scope expansion + individuals -------------------------------------------


def test_scope_member_allowed(model):
    d = is_tool_allowed(model, "support1@example.com", "ops_tool")
    assert d.allow is True
    assert d.reason == "allowed:access/ops_tool/use"


def test_scope_member_case_insensitive(model):
    # support2 is stored uppercase in the fixture; a lowercase request still matches.
    assert is_tool_allowed(model, "support2@example.com", "ops_tool").allow


def test_individual_on_top_of_scope_allowed(model):
    # helpdesk1 (Helpdesk) is not in edit_tool's Support scope but is an individual.
    assert is_tool_allowed(model, "helpdesk1@example.com", "edit_tool").allow


def test_out_of_scope_member_denied(model):
    # sales1 (Sales) is in no roster for ops_tool.
    d = is_tool_allowed(model, "sales1@example.com", "ops_tool")
    assert d.allow is False
    assert d.reason == "denied:access/ops_tool/use"


def test_superadmin_only_tool_denies_support(model):
    assert not is_tool_allowed(model, "support1@example.com", "restricted_report").allow


# --- fail-closed edges -------------------------------------------------------


def test_unknown_tool_denied(model):
    d = is_tool_allowed(model, "support1@example.com", "does_not_exist")
    assert d.allow is False
    assert d.reason.startswith("unknown:")


def test_empty_identity_denied(model):
    for bad in ("", "   ", None):
        d = is_tool_allowed(model, bad, "ops_tool")
        assert d.allow is False
        assert d.reason.startswith("no-identity:")


def test_unknown_person_denied(model):
    assert not is_tool_allowed(model, "stranger@example.com", "ops_tool").allow


def test_empty_roster_tool_admits_only_admins(model):
    # No group => no access except public (the owner's rule). Non-admins denied.
    assert not is_tool_allowed(model, "support1@example.com", "empty_tool").allow
    assert is_tool_allowed(model, "owner1@example.com", "empty_tool").allow


# --- public items ------------------------------------------------------------


def test_public_item_allows_anyone(model):
    d = is_allowed(model, "stranger@example.com", "public_form", section="systems")
    assert d.allow is True
    assert d.reason.startswith("public:")


def test_public_item_allows_even_empty_identity(model):
    assert is_allowed(model, "", "public_form", section="systems").allow


# --- systems section + nested per-person edit rosters ------------------------


def test_systems_view_field(model):
    assert is_allowed(
        model, "support1@example.com", "build_team", section="systems", field="view"
    ).allow
    assert not is_allowed(
        model, "sales1@example.com", "build_team", section="systems", field="view"
    ).allow


def test_nested_edit_roster_union(model):
    # Both the `all` and `own` sub-rosters can edit at all.
    assert is_allowed(
        model, "owner1@example.com", "build_team", section="systems", field="edit"
    ).allow
    assert is_allowed(
        model, "support1@example.com", "build_team", section="systems", field="edit"
    ).allow
    # Someone in neither sub-roster cannot edit.
    assert not is_allowed(
        model, "helpdesk1@example.com", "build_team", section="systems", field="edit"
    ).allow


def test_support_ops_individual_only(model):
    assert is_allowed(model, "owner1@example.com", "support_ops", section="systems").allow
    assert not is_allowed(model, "support1@example.com", "support_ops", section="systems").allow


# --- resolve_allowed_emails + helpers ----------------------------------------


def test_resolve_allowed_emails_includes_admins_and_configured_flag(model):
    allowed, configured = resolve_allowed_emails(model, "access", "ops_tool", "use")
    assert configured is True
    assert {a.lower() for a in ADMINS} <= allowed
    assert "support1@example.com" in allowed
    assert "support2@example.com" in allowed  # normalized from uppercase


def test_resolve_empty_roster_not_configured_but_has_admins(model):
    allowed, configured = resolve_allowed_emails(model, "access", "empty_tool", "use")
    assert configured is False
    assert allowed == {a.lower() for a in ADMINS}


def test_groups_for_email(model):
    assert groups_for_email(model, "support1@example.com") == ["Support"]
    assert groups_for_email(model, "owner1@example.com") == ["SuperAdmin"]
    assert groups_for_email(model, "nobody@example.com") == []


def test_tool_keys(model):
    keys = tool_keys(model)
    assert "ops_tool" in keys and "restricted_report" in keys
    assert keys == sorted(keys)


# --- Decision shape ----------------------------------------------------------


def test_decision_is_namedtuple(model):
    d = is_tool_allowed(model, "owner1@example.com", "ops_tool")
    assert isinstance(d, Decision)
    allow, reason = d
    assert allow is True and isinstance(reason, str)


# --- load_model round-trip against the real model shape ----------------------


def test_load_model_reads_json(tmp_path, model):
    p = tmp_path / "perms.json"
    p.write_text(json.dumps(model))
    loaded = load_model(str(p))
    assert loaded["access"]["ops_tool"] == model["access"]["ops_tool"]
