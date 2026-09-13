"""Tests for the pure-logic access enforcement layer (task #346).

These pin the FAIL-CLOSED policy of the agent tool/data layer: a requester must
not reach a tool or system they were not granted. The model here is an inline
fixture that mirrors the real permissions model shape (a top-level ``admins``
list; ``departments`` -> groups of ``{name, email}``; ``systems`` and ``access``
items with ``{scopes, individuals}`` rosters, a nested per-person ``edit``
roster, and ``public`` items).
"""

from __future__ import annotations

import json

import pytest

from pinky_daemon.access_enforcement import (
    Decision,
    _email_of,
    groups_for_email,
    is_allowed,
    is_tool_allowed,
    load_model,
    resolve_allowed_emails,
    tool_keys,
)

# Lowercase admin identities the fixture recognizes (owners + automation). The
# fixture also stores one mixed-case admin to pin case-insensitive matching.
ADMIN_EMAILS = ("owner1@example.com", "owner2@example.com", "automation@example.com")
MIXED_CASE_ADMIN = "MixedCaseOwner@Example.com"


@pytest.fixture
def model() -> dict:
    return {
        "admins": [*ADMIN_EMAILS, MIXED_CASE_ADMIN],
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
            # 'public' set to a truthy value that is NOT the boolean True: must
            # not be treated as public (guards `is True` and `"public" in item`).
            "public_falsey": {
                "type": "use",
                "public": "false",
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
            # Same as build_team but with a stray sub-key: only all/own count,
            # pending_requests must NOT grant and marks the roster misconfigured.
            "build_team_pending": {
                "type": "editable",
                "edit_mode": "per_person",
                "edit": {
                    "all": {"scopes": [], "individuals": ["owner1@example.com"]},
                    "own": {"scopes": [], "individuals": ["support1@example.com"]},
                    "pending_requests": {"scopes": [], "individuals": ["intruder@example.com"]},
                },
            },
        },
        "access": {
            "ops_tool": {"use": {"scopes": ["Support", "Helpdesk"], "individuals": []}},
            "restricted_report": {"use": {"scopes": ["SuperAdmin"], "individuals": []}},
            "edit_tool": {"use": {"scopes": ["Support"], "individuals": ["helpdesk1@example.com"]}},
            "empty_tool": {"use": {"scopes": [], "individuals": []}},
            # 'public' is meaningless outside systems: stays fail-closed.
            "public_in_access": {"public": True, "use": {"scopes": [], "individuals": []}},
            # A dict-shaped ('map') roster must never grant by iterating keys.
            "maptool": {"use": {"scopes": [], "individuals": {"sneaky@example.com": False}}},
            # An ASCII roster entry to pin the Unicode case-fold / prefix guard.
            "kelvin_tool": {"use": {"scopes": [], "individuals": ["kelvin@example.com"]}},
            # A mixed roster: one valid individual plus a dict-shaped scope
            # element. The valid member still resolves; the roster is flagged.
            "mixed_tool": {
                "use": {"scopes": [{"name": "Support"}], "individuals": ["support1@example.com"]},
            },
            # A scope element that is a list (valid JSON) must not reach
            # departments.get() as a key.
            "listscope_tool": {"use": {"scopes": [["Support"]], "individuals": []}},
            # A roster whose individuals value is a bare string (not a list).
            "strroster_tool": {"use": {"individuals": "owner1@example.com"}},
            # Flat rosters carrying only one of the two keys (for or->and).
            "indiv_only_tool": {"use": {"individuals": ["indivonly@example.com"]}},
            "scope_only_tool": {"use": {"scopes": ["Support"]}},
        },
    }


# --- admins always allowed ---------------------------------------------------


def test_fixture_defines_admins(model):
    assert model["admins"]


def test_admins_always_allowed_even_for_empty_tool(model):
    for admin in ADMIN_EMAILS:
        d = is_tool_allowed(model, admin, "empty_tool")
        assert d.allow is True
        assert d.reason.startswith("admin:")


def test_dev_automation_identity_is_admin(model):
    assert is_tool_allowed(model, "automation@example.com", "restricted_report").allow


def test_admin_allowed_regardless_of_query_case(model):
    assert is_tool_allowed(model, "Owner1@Example.com", "empty_tool").allow


def test_admin_stored_mixed_case_matched_case_insensitively(model):
    # The admins list stores MixedCaseOwner@Example.com; a lowercase request
    # must still match (guards case-sensitive admin matching).
    d = is_tool_allowed(model, "mixedcaseowner@example.com", "empty_tool")
    assert d.allow is True
    assert d.reason.startswith("admin:")


def test_admin_entry_may_be_name_email_object():
    m = {"admins": [{"name": "Owner Nine", "email": "owner9@example.com"}], "access": {}}
    d = is_tool_allowed(m, "owner9@example.com", "anything")
    # unknown item, but an admin is allowed model-structure-independently.
    assert d.allow is True
    assert d.reason.startswith("admin:")


# --- scope expansion + individuals -------------------------------------------


def test_scope_member_allowed(model):
    d = is_tool_allowed(model, "support1@example.com", "ops_tool")
    assert d.allow is True
    assert d.reason == "allowed:access/ops_tool/use"


def test_scope_member_case_insensitive(model):
    # support2 is stored uppercase in the fixture; a lowercase request matches.
    assert is_tool_allowed(model, "support2@example.com", "ops_tool").allow


def test_individual_on_top_of_scope_allowed(model):
    assert is_tool_allowed(model, "helpdesk1@example.com", "edit_tool").allow


def test_out_of_scope_member_denied(model):
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
    # No group named -> only admins pass; a named group member is denied.
    assert not is_tool_allowed(model, "support1@example.com", "empty_tool").allow
    assert is_tool_allowed(model, "owner1@example.com", "empty_tool").allow


def test_unconfigured_roster_deny_reason_flagged(model):
    # An empty (not mistyped) roster's deny reason carries ':unconfigured'.
    d = is_tool_allowed(model, "support1@example.com", "empty_tool")
    assert d.allow is False
    assert d.reason.endswith(":unconfigured")


# --- caller argument discipline (P2-B) ---------------------------------------


def test_non_string_key_denied_without_raising(model):
    d = is_tool_allowed(model, "owner1@example.com", ["x"])
    assert d.allow is False
    assert d.reason == "denied:malformed_key"


def test_non_string_section_or_field_denied(model):
    assert is_allowed(model, "owner1@example.com", "ops_tool", section=["a"]).reason == (
        "denied:malformed_key"
    )
    assert is_allowed(model, "owner1@example.com", "ops_tool", field=123).reason == (
        "denied:malformed_key"
    )


# --- public items ------------------------------------------------------------


def test_public_item_allows_anyone(model):
    d = is_allowed(model, "stranger@example.com", "public_form", section="systems")
    assert d.allow is True
    assert d.reason.startswith("public:")


def test_public_item_allows_even_empty_identity(model):
    assert is_allowed(model, "", "public_form", section="systems").allow


def test_public_only_honored_for_literal_true(model):
    # 'public': "false" is truthy but not the boolean True -> not public.
    # Kills mutants `item.get("public")` (truthy) and `"public" in item` (key).
    d = is_allowed(model, "stranger@example.com", "public_falsey", section="systems")
    assert d.allow is False
    assert not d.reason.startswith("public:")


def test_public_ignored_outside_systems(model):
    d = is_tool_allowed(model, "stranger@example.com", "public_in_access")
    assert d.allow is False
    assert not d.reason.startswith("public:")


# --- systems section + nested per-person edit rosters ------------------------


def test_systems_view_field(model):
    assert is_allowed(
        model, "support1@example.com", "build_team", section="systems", field="view"
    ).allow
    assert not is_allowed(
        model, "sales1@example.com", "build_team", section="systems", field="view"
    ).allow


def test_nested_edit_roster_union(model):
    assert is_allowed(
        model, "owner1@example.com", "build_team", section="systems", field="edit"
    ).allow
    assert is_allowed(
        model, "support1@example.com", "build_team", section="systems", field="edit"
    ).allow
    assert not is_allowed(
        model, "helpdesk1@example.com", "build_team", section="systems", field="edit"
    ).allow


def test_nested_edit_only_known_subkeys_grant(model):
    # all/own still grant; a stray pending_requests sub-key must not, and the
    # roster is flagged misconfigured.
    assert is_allowed(
        model, "owner1@example.com", "build_team_pending", section="systems", field="edit"
    ).allow
    assert is_allowed(
        model, "support1@example.com", "build_team_pending", section="systems", field="edit"
    ).allow
    d = is_allowed(
        model, "intruder@example.com", "build_team_pending", section="systems", field="edit"
    )
    assert d.allow is False
    assert d.reason.endswith(":misconfigured")


def test_support_ops_individual_only(model):
    assert is_allowed(model, "owner1@example.com", "support_ops", section="systems").allow
    assert not is_allowed(model, "support1@example.com", "support_ops", section="systems").allow


# --- malformed model: never raise, fail closed, admin still allowed ----------


def test_model_none_denies_without_raising():
    d = is_tool_allowed(None, "support1@example.com", "ops_tool")
    assert d.allow is False
    assert d.reason == "denied:malformed_model:model"


def test_access_section_as_list():
    m = {"admins": ["owner1@example.com"], "access": ["ops_tool"]}
    d = is_tool_allowed(m, "support1@example.com", "ops_tool")
    assert d.allow is False
    assert d.reason == "denied:malformed_model:section"
    assert is_tool_allowed(m, "owner1@example.com", "ops_tool").allow is True


def test_wrong_section(model):
    d = is_allowed(model, "support1@example.com", "ops_tool", section="bogus")
    assert d.allow is False
    assert d.reason == "denied:malformed_model:section"
    assert is_allowed(model, "owner1@example.com", "ops_tool", section="bogus").allow is True


def test_malformed_item_value():
    for bad in ("use", 123, ["use"]):
        m = {"admins": ["owner1@example.com"], "access": {"bad": bad}}
        d = is_tool_allowed(m, "support1@example.com", "bad")
        assert d.allow is False
        assert d.reason == "denied:malformed_model:item"
        assert is_tool_allowed(m, "owner1@example.com", "bad").allow is True


def test_departments_not_dict_never_raises():
    m = {
        "admins": ["owner1@example.com"],
        "departments": "x",
        "access": {"t": {"use": {"scopes": ["Support"], "individuals": []}}},
    }
    assert is_tool_allowed(m, "support1@example.com", "t").allow is False
    assert is_tool_allowed(m, "owner1@example.com", "t").allow is True


# --- scope element-type discipline (P1-A): a value must never be a dict key ---


def test_dict_shaped_scope_element_denies_without_raising(model):
    # scopes: [{"name": "Support"}] -> the element must never index departments.
    d = is_tool_allowed(model, "sales1@example.com", "mixed_tool")
    assert d.allow is False
    assert d.reason.endswith(":misconfigured")
    # the valid individual in the same roster still resolves...
    assert is_tool_allowed(model, "support1@example.com", "mixed_tool").allow is True
    # ...and an admin short-circuits to allow on the same model.
    assert is_tool_allowed(model, "owner1@example.com", "mixed_tool").allow is True


def test_list_shaped_scope_element_denies_without_raising(model):
    # scopes: [["Support"]] -> a list element must never index departments.
    d = is_tool_allowed(model, "sales1@example.com", "listscope_tool")
    assert d.allow is False
    assert d.reason.endswith(":misconfigured")
    assert is_tool_allowed(model, "owner1@example.com", "listscope_tool").allow is True


# --- misconfigured containers cannot grant (P1-2 / P3) -----------------------


def test_map_shaped_roster_cannot_grant(model):
    d = is_tool_allowed(model, "sneaky@example.com", "maptool")
    assert d.allow is False
    allowed, configured, misconfigured = resolve_allowed_emails(model, "access", "maptool", "use")
    assert "sneaky@example.com" not in allowed
    assert configured is False
    assert misconfigured is True


def test_str_shaped_roster_flagged_misconfigured(model):
    # individuals is a bare string, not a list. Kills a mutant that treats a
    # str as a valid sequence (which would yield :unconfigured, not flagged).
    d = is_tool_allowed(model, "sales1@example.com", "strroster_tool")
    assert d.allow is False
    assert d.reason.endswith(":misconfigured")


def test_dict_shaped_department_cannot_grant():
    m = {
        "admins": ["owner1@example.com"],
        "departments": {"BadDept": {"x@example.com": True}},
        "access": {"t": {"use": {"scopes": ["BadDept"], "individuals": []}}},
    }
    assert is_tool_allowed(m, "x@example.com", "t").allow is False
    allowed, configured, misconfigured = resolve_allowed_emails(m, "access", "t", "use")
    assert "x@example.com" not in allowed
    assert configured is False
    assert misconfigured is True


def test_mixed_roster_configured_and_misconfigured_are_independent(model):
    # One valid individual (configured True) plus a dict-shaped scope element
    # (misconfigured True). Kills a mutant that forces configured=False when
    # misconfigured.
    allowed, configured, misconfigured = resolve_allowed_emails(
        model, "access", "mixed_tool", "use"
    )
    assert "support1@example.com" in allowed
    assert configured is True
    assert misconfigured is True


# --- identity normalization (P2-4 / P2-D) ------------------------------------


def test_non_string_identity_is_no_identity(model):
    for bad in (123, b"support1@example.com", {"email": "owner1@example.com"}, object()):
        d = is_tool_allowed(model, bad, "ops_tool")
        assert d.allow is False
        assert d.reason.startswith("no-identity:")


def test_unicode_kelvin_identity_denied(model):
    assert is_tool_allowed(model, "kelvin@example.com", "kelvin_tool").allow is True
    # U+212A KELVIN SIGN case-folds to ASCII 'k' but is not ASCII -> rejected.
    kelvin_variant = "\u212aelvin@example.com"
    assert kelvin_variant.lower() == "kelvin@example.com"
    assert not kelvin_variant.isascii()
    d = is_tool_allowed(model, kelvin_variant, "kelvin_tool")
    assert d.allow is False
    assert d.reason.startswith("no-identity:")


def test_unicode_whitespace_prefixed_identity_denied(model):
    # NBSP / U+2000 / U+3000 would be trimmed by a bare str.strip(); the raw
    # ASCII check rejects them so a prefixed admin cannot slip through.
    for prefix in ("\u00a0", "\u2000", "\u3000"):
        ident = prefix + "owner1@example.com"
        d = is_tool_allowed(model, ident, "empty_tool")
        assert d.allow is False
        assert d.reason.startswith("no-identity:")


# --- admins list container discipline (P2-E) ---------------------------------


def test_admins_as_dict_grants_no_admin():
    # A dict admins value is not a list -> no admins (kills a mutant that would
    # iterate any iterable and pick up the dict keys).
    m = {"admins": {"owner1@example.com": True}, "access": {"empty_tool": {"use": {}}}}
    assert is_tool_allowed(m, "owner1@example.com", "empty_tool").allow is False


def test_admins_as_str_grants_no_admin():
    m = {"admins": "owner1@example.com", "access": {"empty_tool": {"use": {}}}}
    assert is_tool_allowed(m, "owner1@example.com", "empty_tool").allow is False


# --- resolve_allowed_emails + helpers ----------------------------------------


def test_resolve_allowed_emails_includes_admins_and_flags(model):
    admins = {e.lower() for e in model["admins"]}
    allowed, configured, misconfigured = resolve_allowed_emails(model, "access", "ops_tool", "use")
    assert configured is True
    assert misconfigured is False
    assert admins <= allowed
    assert "support1@example.com" in allowed
    assert "support2@example.com" in allowed  # normalized from uppercase


def test_resolve_empty_roster_flags(model):
    admins = {e.lower() for e in model["admins"]}
    allowed, configured, misconfigured = resolve_allowed_emails(
        model, "access", "empty_tool", "use"
    )
    assert configured is False
    assert misconfigured is False
    assert allowed == admins


def test_groups_for_email(model):
    assert groups_for_email(model, "support1@example.com") == ["Support"]
    assert groups_for_email(model, "owner1@example.com") == ["SuperAdmin"]
    assert groups_for_email(model, "nobody@example.com") == []


def test_groups_for_email_guards_non_dict_departments():
    assert groups_for_email({"departments": "x"}, "support1@example.com") == []
    assert groups_for_email(None, "support1@example.com") == []


def test_tool_keys(model):
    keys = tool_keys(model)
    assert "ops_tool" in keys and "restricted_report" in keys
    assert keys == sorted(keys)


def test_tool_keys_tolerates_non_str_keys():
    m = {"access": {"a_tool": {}, 5: {}, "b_tool": {}}}
    assert tool_keys(m) == ["a_tool", "b_tool"]


def test_tool_keys_malformed_model():
    assert tool_keys(None) == []
    assert tool_keys({"access": "x"}) == []


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


def test_load_model_rejects_non_object(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ValueError):
        load_model(str(p))


# --- round 4: list-valued edit sub-rosters (M7) ------------------------------


def test_list_valued_edit_subrosters_denied_without_raising():
    # A list-shaped `all` (or `own`) must not be accepted as a roster: it denies
    # with :misconfigured and never reaches a dict-key/attr access that raises.
    for slot in ("all", "own"):
        edit = {
            "all": {"scopes": [], "individuals": ["owner1@example.com"]},
            "own": {"scopes": [], "individuals": ["support1@example.com"]},
        }
        edit[slot] = [{"individuals": ["listgrant@example.com"]}]
        m = {"admins": [], "departments": {}, "systems": {"bt": {"edit": edit}}}
        d = is_allowed(m, "listgrant@example.com", "bt", section="systems", field="edit")
        assert d.allow is False
        assert d.reason.endswith(":misconfigured")
        # the sibling proper-dict sub-roster still resolves.
        good = "support1@example.com" if slot == "all" else "owner1@example.com"
        assert is_allowed(m, good, "bt", section="systems", field="edit").allow is True


# --- round 4: identity trim charset (M22) ------------------------------------


def test_admin_with_control_char_suffix_not_admin(model):
    # Vertical tab / form feed are ASCII but outside the " \t\r\n" trim set, so
    # an admin email carrying one is NOT the admin.
    for suffix in ("\x0b", "\x0c"):
        d = is_tool_allowed(model, "owner1@example.com" + suffix, "empty_tool")
        assert d.allow is False
        assert not d.reason.startswith("admin:")


# --- round 4: flat-roster detection is `or`, not `and` (M24) -----------------


def test_flat_roster_only_individuals_grants(model):
    assert is_tool_allowed(model, "indivonly@example.com", "indiv_only_tool").allow is True


def test_flat_roster_only_scopes_grants(model):
    assert is_tool_allowed(model, "support1@example.com", "scope_only_tool").allow is True


# --- round 4: admin match normalizes the query too (M16) ---------------------


def test_mixed_case_admin_query_on_broken_model_allowed():
    m = {"admins": ["owner1@example.com"], "access": ["x"]}
    d = is_tool_allowed(m, "Owner1@Example.com", "x")
    assert d.allow is True
    assert d.reason.startswith("admin:")


# --- round 4: element guards + no str() coercion (M9 / M18 / M19) ------------


def test_email_of_non_str_returns_empty():
    # _email_of never coerces a non-string via str().
    assert _email_of(123) == ""
    assert _email_of(["x@example.com"]) == ""
    assert _email_of(None) == ""
    assert _email_of(object()) == ""


def test_department_member_bad_element_flagged_not_granted():
    m = {
        "admins": [],
        "departments": {"Mix": [{"name": "Valid", "email": "valid@example.com"}, 123]},
        "access": {"t": {"use": {"scopes": ["Mix"], "individuals": []}}},
    }
    allowed, configured, misconfigured = resolve_allowed_emails(m, "access", "t", "use")
    assert "valid@example.com" in allowed
    assert configured is True
    assert misconfigured is True
    assert "123" not in allowed  # a coerced non-str member must never appear


def test_individuals_bad_element_flagged_not_granted():
    m = {
        "admins": [],
        "access": {"t": {"use": {"scopes": [], "individuals": ["valid2@example.com", 456]}}},
    }
    allowed, configured, misconfigured = resolve_allowed_emails(m, "access", "t", "use")
    assert "valid2@example.com" in allowed
    assert configured is True
    assert misconfigured is True
    assert "456" not in allowed


# --- round 4: unresolvable rosters are misconfigured, not unconfigured (P3-1) -


def test_unresolvable_rosters_flagged_misconfigured():
    m = {
        "admins": [],
        "departments": {"Real": [{"name": "No Email"}]},  # member without an email
        "access": {
            "ghost": {"use": {"scopes": ["Nope"], "individuals": []}},  # dangling group
            "noemail": {"use": {"scopes": ["Real"], "individuals": []}},  # member w/o email
            "badindiv": {
                "use": {"scopes": [], "individuals": ["\u00e9x@example.com"]}
            },  # non-ASCII
        },
    }
    for key in ("ghost", "noemail", "badindiv"):
        d = is_tool_allowed(m, "someone@example.com", key)
        assert d.allow is False
        assert d.reason.endswith(":misconfigured"), key


# --- round 4: flat wins over sibling nested keys, but is flagged (P3-2) -------


def test_flat_and_nested_mixed_flat_wins_but_flagged():
    m = {
        "admins": [],
        "departments": {"Support": [{"email": "support1@example.com"}]},
        "systems": {
            "mix": {
                "view": {
                    "scopes": ["Support"],
                    "individuals": [],
                    "all": {"individuals": ["extra@example.com"]},
                }
            }
        },
    }
    # flat scopes win -> the Support member is allowed.
    assert (
        is_allowed(m, "support1@example.com", "mix", section="systems", field="view").allow is True
    )
    # the sibling nested 'all' does not grant, and the roster is flagged.
    d = is_allowed(m, "extra@example.com", "mix", section="systems", field="view")
    assert d.allow is False
    assert d.reason.endswith(":misconfigured")
    allowed, configured, misconfigured = resolve_allowed_emails(m, "systems", "mix", "view")
    assert "support1@example.com" in allowed
    assert "extra@example.com" not in allowed
    assert misconfigured is True


# --- round 4: identifier charset on caller args (P3-4) -----------------------


def test_key_with_newline_denied_malformed_key(model):
    d = is_tool_allowed(model, "owner1@example.com", "ops\ntool")
    assert d.allow is False
    assert d.reason == "denied:malformed_key"


def test_section_or_field_with_newline_denied(model):
    assert is_allowed(model, "owner1@example.com", "ops_tool", section="a\nb").reason == (
        "denied:malformed_key"
    )
    assert is_allowed(model, "owner1@example.com", "ops_tool", field="u\nse").reason == (
        "denied:malformed_key"
    )


# --- round 5: trailing newline must not slip past the anchor (P2) ------------


def test_trailing_newline_key_section_field_denied(model):
    # Python '$' matches before a final newline; the anchors must be \A..\Z so a
    # trailing "\n" cannot reach the reason string.
    assert is_tool_allowed(model, "owner1@example.com", "ops_tool\n").reason == (
        "denied:malformed_key"
    )
    assert is_allowed(model, "owner1@example.com", "ops_tool", section="access\n").reason == (
        "denied:malformed_key"
    )
    assert is_allowed(model, "owner1@example.com", "ops_tool", field="use\n").reason == (
        "denied:malformed_key"
    )


# --- round 5: identifier length boundaries (P3) ------------------------------


@pytest.mark.parametrize(
    "n,expect_malformed",
    [(0, True), (1, False), (128, False), (129, True)],
)
def test_key_length_boundaries(model, n, expect_malformed):
    # Pins {1,128}: 0 and 129 are rejected (kills {0,128} and {1,4096}); 1 and
    # 128 are accepted (they reach the model, so the reason is not malformed).
    key = "a" * n
    reason = is_tool_allowed(model, "owner1@example.com", key).reason
    if expect_malformed:
        assert reason == "denied:malformed_key"
    else:
        assert reason != "denied:malformed_key"


# --- round 5: a null-valued group reads like a missing group (P3 nit) --------


def test_null_valued_group_reads_like_missing_group():
    m = {
        "admins": [],
        "departments": {"NullGroup": None},
        "access": {
            "nullg": {"use": {"scopes": ["NullGroup"], "individuals": []}},
            "missg": {"use": {"scopes": ["NopeNotThere"], "individuals": []}},
        },
    }
    r_null = is_tool_allowed(m, "someone@example.com", "nullg").reason
    r_miss = is_tool_allowed(m, "someone@example.com", "missg").reason
    # Same classification (the reasons differ only by their distinct key segment).
    assert r_null.endswith(":misconfigured")
    assert r_miss.endswith(":misconfigured")
    assert r_null.split("/", 1)[0] == r_miss.split("/", 1)[0] == "denied:access"
