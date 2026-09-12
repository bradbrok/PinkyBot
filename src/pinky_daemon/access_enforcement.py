"""Pure-logic access enforcement for the agent tool/data layer (task #346).

Ryan's standing security rule (2026-09-12): staff must never gain tools they
should not have, or restricted information, simply by asking an agent. Today the
company access model (``geordi_permissions.json``) is enforced ONLY by the
external Apps Script Tools Page; it has zero code consumers in the daemon, so a
staffer who free-texts Geordi in Slack is bounded only by prose in his soul,
which is socially engineerable. This module is the first, self-contained piece
of moving that check to the tool/data layer, keyed on the *verified* requester.

It is deliberately import-free of the rest of the daemon and has NO side effects:
it loads the model and answers "may this verified email use this tool/system?".
Wiring it into the MCP choke point is a later increment; nothing imports this yet.

Model shape (``geordi_permissions.json``):
  * ``departments`` maps each of the six groups (Sales, Project Managers,
    Helpdesk, Support, Admin, SuperAdmin) to a list of ``{name, email}`` members.
  * ``systems`` are the web apps/pages; ``access`` is Geordi's Tool Kit (its 17
    tools), every one a ``use``-gated item.
  * Each item carries one or more rosters. A roster is ``{scopes, individuals}``:
    ``scopes`` names whole groups (expanded through ``departments``),
    ``individuals`` names extra emails on top. An ``edit`` roster on a
    ``per_person`` item is nested ``{all:{...}, own:{...}}`` instead — a person
    who can edit at all appears in one of the two, so "may edit" is their union.
  * An item marked ``public: true`` (e.g. client_rebook) is open to anyone.

Enforcement policy (differs from the Tools-page resolver on purpose):
  * ADMINS are ALWAYS allowed, independent of the model, so a bad or empty model
    can never lock the owners (or the dev@ automation identity) out.
  * The AGENT path is FAIL-CLOSED, per Ryan's rule "no group = no access except
    public". An unknown tool key, an unknown/empty requester identity, or an
    email the model does not name for that roster all resolve to DENY. This is
    the opposite of ``acl_resolver.resolve_field`` (the publish step), which
    fails OPEN to domain-wide so a publish glitch never locks the company out of
    a web page. The agent tool layer has no such "open by default" — reaching a
    credentialed tool you were not granted is exactly the gap being closed.
"""

from __future__ import annotations

import json
from collections import namedtuple
from typing import Iterable

# Owners + the internal automation identity. Always allowed, model-independent.
ADMINS = frozenset(
    {
        "ryan@posspecialists.com",
        "alex@posspecialists.com",
        "dev@posspecialists.com",
    }
)

# The two model sections that gate access.
SECTION_SYSTEMS = "systems"  # web apps / pages
SECTION_ACCESS = "access"  # Geordi's Tool Kit (the individual tools)

# A single allow/deny answer plus a short machine-readable reason (for logging in
# the eventual log-only rollout, and for the deny message once enforcement is on).
Decision = namedtuple("Decision", ["allow", "reason"])


def _norm(email: object) -> str:
    """Lowercase/strip an email; return '' for anything non-string/empty."""
    if isinstance(email, str):
        return email.strip().lower()
    return ""


def _email_of(member: object) -> str:
    """Extract a lowercased email from a ``{name, email}`` dict or a raw string."""
    if isinstance(member, dict):
        return _norm(member.get("email"))
    return _norm(member)


def _admins() -> set[str]:
    return {a.lower() for a in ADMINS}


def load_model(path: str) -> dict:
    """Load the permissions model JSON from disk."""
    with open(path) as f:
        return json.load(f)


def _iter_rosters(field_value: object) -> Iterable[dict]:
    """Yield the flat ``{scopes, individuals}`` rosters inside a field value.

    A plain roster yields itself. A nested per-person ``edit`` roster
    (``{all:{...}, own:{...}}``) yields each sub-roster, so the caller gets the
    union of everyone who can act at all.
    """
    if not isinstance(field_value, dict):
        return
    if "scopes" in field_value or "individuals" in field_value:
        yield field_value
        return
    for sub in field_value.values():
        if isinstance(sub, dict) and ("scopes" in sub or "individuals" in sub):
            yield sub


def resolve_allowed_emails(model: dict, section: str, key: str, field: str):
    """Return ``(allowed_emails, configured)`` for one roster.

    ``allowed_emails`` is a lowercased set that ALWAYS includes ADMINS, formed
    from the union of the roster's scopes (expanded through ``departments``) and
    its explicit individuals. ``configured`` is True iff the model named at least
    one non-admin member for this roster; it is informational (the agent path is
    fail-closed either way — an unconfigured roster simply admits only admins).
    """
    departments = model.get("departments") or {}
    items = model.get(section) or {}
    item = items.get(key) or {}
    field_value = item.get(field)

    model_emails: set[str] = set()
    for roster in _iter_rosters(field_value):
        for scope in roster.get("scopes") or []:
            for member in departments.get(scope) or []:
                e = _email_of(member)
                if e:
                    model_emails.add(e)
        for ind in roster.get("individuals") or []:
            e = _email_of(ind)
            if e:
                model_emails.add(e)

    configured = len(model_emails) > 0
    allowed = _admins() | model_emails
    return allowed, configured


def groups_for_email(model: dict, email: str) -> list[str]:
    """Return the department/group names the email belongs to (may be empty)."""
    e = _norm(email)
    if not e:
        return []
    departments = model.get("departments") or {}
    return sorted(
        group
        for group, members in departments.items()
        if any(_email_of(m) == e for m in (members or []))
    )


def is_allowed(
    model: dict,
    email: str,
    key: str,
    section: str = SECTION_ACCESS,
    field: str = "use",
) -> Decision:
    """Decide whether ``email`` may use ``key`` in ``section`` (FAIL-CLOSED).

    Order of checks:
      1. Unknown item key -> DENY (fail-closed; never invent access).
      2. Item marked ``public: true`` -> ALLOW.
      3. Empty/unknown requester identity -> DENY (cannot verify -> no access).
      4. ADMIN email -> ALLOW.
      5. Email in the resolved roster -> ALLOW.
      6. Otherwise -> DENY.
    """
    items = model.get(section) or {}
    item = items.get(key)
    if item is None:
        return Decision(False, f"unknown:{section}/{key}")

    if isinstance(item, dict) and item.get("public"):
        return Decision(True, f"public:{section}/{key}")

    e = _norm(email)
    if not e:
        return Decision(False, f"no-identity:{section}/{key}")

    if e in _admins():
        return Decision(True, f"admin:{section}/{key}")

    allowed, _configured = resolve_allowed_emails(model, section, key, field)
    if e in allowed:
        return Decision(True, f"allowed:{section}/{key}/{field}")

    return Decision(False, f"denied:{section}/{key}/{field}")


def is_tool_allowed(model: dict, email: str, tool_key: str) -> Decision:
    """Convenience wrapper for a Geordi Tool Kit item (``access`` section, use)."""
    return is_allowed(model, email, tool_key, section=SECTION_ACCESS, field="use")


def tool_keys(model: dict) -> list[str]:
    """Return the sorted keys of Geordi's Tool Kit (the ``access`` section)."""
    return sorted((model.get(SECTION_ACCESS) or {}).keys())
