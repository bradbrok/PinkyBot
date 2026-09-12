"""Tests for the pure-logic access enforcement layer (task #346).

These pin the FAIL-CLOSED policy of the agent tool/data layer: staff must not
reach a tool or system they were not granted just by asking an agent. The model
here is an inline fixture that mirrors the real ``geordi_permissions.json`` shape
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
                {"name": "Daniel Martinez", "email": "daniel@posspecialists.com"},
                {"name": "Aldo Huerta", "email": "ALDO@posspecialists.com"},
            ],
            "Helpdesk": [
                {"name": "Pete", "email": "pete@posspecialists.com"},
            ],
            "Sales": [
                {"name": "Kevin Love", "email": "kevin@posspecialists.com"},
            ],
            "SuperAdmin": [
                {"name": "Ryan Martin", "email": "ryan@posspecialists.com"},
                {"name": "Alex Ugrin", "email": "alex@posspecialists.com"},
            ],
        },
        "systems": {
            "client_rebook": {
                "type": "use",
                "public": True,
                "use": {"scopes": [], "individuals": []},
            },
            "support_ops": {
                "type": "use",
                "use": {"scopes": [], "individuals": ["ryan@posspecialists.com"]},
            },
            "build_team": {
                "type": "editable",
                "edit_mode": "per_person",
                "view": {"scopes": ["Support"], "individuals": []},
                "edit": {
                    "all": {"scopes": [], "individuals": ["ryan@posspecialists.com"]},
                    "own": {"scopes": [], "individuals": ["daniel@posspecialists.com"]},
                },
            },
        },
        "access": {
            "rma": {"use": {"scopes": ["Support", "Helpdesk"], "individuals": []}},
            "gift_card_liability_report": {
                "use": {"scopes": ["SuperAdmin"], "individuals": []}
            },
            "add_to_menu": {
                "use": {"scopes": ["Support"], "individuals": ["pete@posspecialists.com"]}
            },
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
    assert is_tool_allowed(model, "dev@posspecialists.com", "gift_card_liability_report").allow


def test_admin_allowed_regardless_of_case(model):
    assert is_tool_allowed(model, "Ryan@PosSpecialists.com", "empty_tool").allow


# --- scope expansion + individuals -------------------------------------------


def test_scope_member_allowed(model):
    d = is_tool_allowed(model, "daniel@posspecialists.com", "rma")
    assert d.allow is True
    assert d.reason == "allowed:access/rma/use"


def test_scope_member_case_insensitive(model):
    # Aldo is stored uppercase in the fixture; a lowercase request still matches.
    assert is_tool_allowed(model, "aldo@posspecialists.com", "rma").allow


def test_individual_on_top_of_scope_allowed(model):
    # Pete (Helpdesk) is not in add_to_menu's Support scope but is an individual.
    assert is_tool_allowed(model, "pete@posspecialists.com", "add_to_menu").allow


def test_out_of_scope_member_denied(model):
    # Kevin (Sales) is in no roster for rma.
    d = is_tool_allowed(model, "kevin@posspecialists.com", "rma")
    assert d.allow is False
    assert d.reason == "denied:access/rma/use"


def test_superadmin_only_tool_denies_support(model):
    assert not is_tool_allowed(model, "daniel@posspecialists.com", "gift_card_liability_report").allow


# --- fail-closed edges -------------------------------------------------------


def test_unknown_tool_denied(model):
    d = is_tool_allowed(model, "daniel@posspecialists.com", "does_not_exist")
    assert d.allow is False
    assert d.reason.startswith("unknown:")


def test_empty_identity_denied(model):
    for bad in ("", "   ", None):
        d = is_tool_allowed(model, bad, "rma")
        assert d.allow is False
        assert d.reason.startswith("no-identity:")


def test_unknown_person_denied(model):
    assert not is_tool_allowed(model, "stranger@example.com", "rma").allow


def test_empty_roster_tool_admits_only_admins(model):
    # No group => no access except public (Ryan's rule). Non-admins denied.
    assert not is_tool_allowed(model, "daniel@posspecialists.com", "empty_tool").allow
    assert is_tool_allowed(model, "ryan@posspecialists.com", "empty_tool").allow


# --- public items ------------------------------------------------------------


def test_public_item_allows_anyone(model):
    d = is_allowed(model, "stranger@example.com", "client_rebook", section="systems")
    assert d.allow is True
    assert d.reason.startswith("public:")


def test_public_item_allows_even_empty_identity(model):
    assert is_allowed(model, "", "client_rebook", section="systems").allow


# --- systems section + nested per-person edit rosters ------------------------


def test_systems_view_field(model):
    assert is_allowed(model, "daniel@posspecialists.com", "build_team",
                      section="systems", field="view").allow
    assert not is_allowed(model, "kevin@posspecialists.com", "build_team",
                          section="systems", field="view").allow


def test_nested_edit_roster_union(model):
    # Both the `all` and `own` sub-rosters can edit at all.
    assert is_allowed(model, "ryan@posspecialists.com", "build_team",
                      section="systems", field="edit").allow
    assert is_allowed(model, "daniel@posspecialists.com", "build_team",
                      section="systems", field="edit").allow
    # Someone in neither sub-roster cannot edit.
    assert not is_allowed(model, "pete@posspecialists.com", "build_team",
                          section="systems", field="edit").allow


def test_support_ops_individual_only(model):
    assert is_allowed(model, "ryan@posspecialists.com", "support_ops", section="systems").allow
    assert not is_allowed(model, "daniel@posspecialists.com", "support_ops", section="systems").allow


# --- resolve_allowed_emails + helpers ----------------------------------------


def test_resolve_allowed_emails_includes_admins_and_configured_flag(model):
    allowed, configured = resolve_allowed_emails(model, "access", "rma", "use")
    assert configured is True
    assert {a.lower() for a in ADMINS} <= allowed
    assert "daniel@posspecialists.com" in allowed
    assert "aldo@posspecialists.com" in allowed  # normalized from uppercase


def test_resolve_empty_roster_not_configured_but_has_admins(model):
    allowed, configured = resolve_allowed_emails(model, "access", "empty_tool", "use")
    assert configured is False
    assert allowed == {a.lower() for a in ADMINS}


def test_groups_for_email(model):
    assert groups_for_email(model, "daniel@posspecialists.com") == ["Support"]
    assert groups_for_email(model, "ryan@posspecialists.com") == ["SuperAdmin"]
    assert groups_for_email(model, "nobody@example.com") == []


def test_tool_keys(model):
    keys = tool_keys(model)
    assert "rma" in keys and "gift_card_liability_report" in keys
    assert keys == sorted(keys)


# --- Decision shape ----------------------------------------------------------


def test_decision_is_namedtuple(model):
    d = is_tool_allowed(model, "ryan@posspecialists.com", "rma")
    assert isinstance(d, Decision)
    allow, reason = d
    assert allow is True and isinstance(reason, str)


# --- load_model round-trip against the real model shape ----------------------


def test_load_model_reads_json(tmp_path, model):
    p = tmp_path / "perms.json"
    p.write_text(json.dumps(model))
    loaded = load_model(str(p))
    assert loaded["access"]["rma"] == model["access"]["rma"]
