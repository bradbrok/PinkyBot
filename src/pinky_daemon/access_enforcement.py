"""Pure-logic access enforcement for the agent tool/data layer (task #346).

Answers one question with no side effects: may a *verified* requester use a
given tool or system, according to the company permissions model? The model is
loaded from disk; the module is self-contained and imports nothing from the
rest of the daemon, so it can be reasoned about and tested in isolation.

Access decisions belong at the tool/data layer, keyed on the verified
requester's identity, so that what a requester may reach is decided by the
permissions model rather than by any instruction handed to the agent. Deciding
it here, in code, is what lets the guarantee hold regardless of how the request
arrives.

The contract assumes JSON-derived values: the model and its nested pieces are
plain dicts, lists, and strings. Every other shape is treated as malformed and
denied, never trusted.

Model shape:
  * ``admins`` is a top-level list of members (each an email string or a
    ``{name, email}`` object) that are always allowed, independent of the rest
    of the model, so a structurally broken model can never lock the owners (or
    the automation identity) out.
  * ``departments`` maps each group (illustrative: Support, Helpdesk, Sales,
    Admin, SuperAdmin) to a list of ``{name, email}`` members.
  * ``systems`` are the web apps/pages; ``access`` is the agent's tool kit,
    every item ``use``-gated.
  * Each item carries one or more rosters. A roster is ``{scopes, individuals}``:
    ``scopes`` names whole groups (expanded through ``departments``),
    ``individuals`` names extra emails on top. An ``edit`` roster on a
    per-person item is nested ``{all:{...}, own:{...}}`` instead; a person who
    can edit at all appears in one of those two known sub-rosters, so "may edit"
    is their union. Any other sub-key is ignored and marks the roster
    misconfigured.
  * A ``systems`` item whose ``public`` value is the literal boolean ``true``
    (illustrative: a public booking form) is open to anyone. ``public`` is
    honored only in the ``systems`` section and only for the literal boolean.

Enforcement policy:
  * ADMINS (from the model's ``admins`` list) are always allowed.
  * Every other path is FAIL-CLOSED: an unknown tool key, an empty or
    non-string requester identity, a malformed model, or an identity the model
    does not name for that roster all resolve to DENY. There is no "open by
    default": reaching a credentialed tool that was not granted is the case this
    layer exists to stop.
  * The layer never raises. A malformed model or a non-identifier caller
    argument yields a deny carrying a machine-readable reason. A deny records
    whether the roster was empty (``:unconfigured``) or mistyped/unresolvable
    (``:misconfigured``), which lets a log-only deployment mode tell those apart
    from a genuine denial.
"""

from __future__ import annotations

import json
import re
from collections import namedtuple

# The two model sections that gate access.
SECTION_SYSTEMS = "systems"  # web apps / pages
SECTION_ACCESS = "access"  # the agent's tool kit (the individual tools)

# The only sub-rosters recognized inside a nested per-person edit roster.
_EDIT_SUBKEYS = ("all", "own")

# Caller-supplied key/section/field must look like an identifier. This keeps a
# reason string free of injected control characters (e.g. a newline that could
# forge a second log line in a log-only deployment mode). The anchors are
# ``\A``/``\Z`` rather than ``^``/``$``: Python's ``$`` also matches just before
# a trailing newline, which would let "value\n" slip past.
_IDENT_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,128}\Z")

# A single allow/deny answer plus a short machine-readable reason (used for a
# log-only deployment mode, and as the deny message it returns).
Decision = namedtuple("Decision", ["allow", "reason"])


def _norm(email: object) -> str:
    """Normalize an identity to a lowercased email.

    Returns '' for anything that is not a plain ASCII string. The identity must
    be a ``str`` whose *raw* value (before any stripping) is ASCII, then only
    ASCII spaces/tabs/newlines are trimmed. The ASCII check runs on the raw
    value on purpose: ``str.strip()`` with no arguments removes Unicode
    whitespace (NBSP, U+2000, U+3000, ...), which would let a Unicode-prefixed
    identity trim down to an ASCII roster entry; and some non-ASCII characters
    case-fold to ASCII (e.g. U+212A KELVIN SIGN lowercases to 'k'). The trim set
    is exactly " \\t\\r\\n": other control characters (vertical tab, form feed)
    are deliberately left in place so they cannot silently equal a clean entry.
    """
    if not isinstance(email, str):
        return ""
    if not email.isascii():
        return ""
    return email.strip(" \t\r\n").lower()


def _email_of(member: object) -> str:
    """Extract a lowercased email from a ``{name, email}`` dict or a raw string.

    Returns '' for any other type; it never coerces a non-string via ``str()``,
    so a stray int/list/object can never become a bogus allowed email.
    """
    if isinstance(member, dict):
        return _norm(member.get("email"))
    return _norm(member)


def _admins(model: object) -> set[str]:
    """Return the admin email set from the model's top-level ``admins`` list.

    Reads only that one list, so admins are recognized even when the rest of the
    model is structurally broken. A non-dict model or a non-list ``admins``
    value yields an empty set. Each entry may be an email string or a
    ``{name, email}`` object; any other entry type is skipped.
    """
    if not isinstance(model, dict):
        return set()
    raw = model.get("admins")
    if not isinstance(raw, (list, tuple)):
        return set()
    out: set[str] = set()
    for a in raw:
        if isinstance(a, (str, dict)):
            e = _email_of(a)
            if e:
                out.add(e)
    return out


def _valid_seq(value: object) -> tuple[list | tuple, bool]:
    """Return ``(sequence, ok)`` for a roster container.

    A list or tuple is a valid container; an absent value (``None``) is a valid
    empty container. Anything else (a dict, a bare string, an int, ...) is
    misconfigured: it is treated as empty and flagged, so neither a dict-shaped
    roster such as ``{"user@example.com": false}`` nor a bare-string roster can
    grant by being iterated.
    """
    if isinstance(value, (list, tuple)):
        return value, True
    if value is None:
        return [], True
    return [], False


def _rosters_and_flag(field_value: object) -> tuple[list, bool]:
    """Return ``(rosters, misconfigured)`` for one item field value.

    A flat roster (one carrying ``scopes`` or ``individuals``) wins; if a nested
    ``all`` / ``own`` sub-key sits alongside it, that mix is flagged
    misconfigured. Otherwise the value is treated as a nested per-person edit
    roster and only its ``all`` / ``own`` sub-rosters (which must be objects)
    are taken; any other sub-key, or a non-object sub-roster, is ignored and
    marks the value misconfigured (so a stray key such as ``pending_requests``,
    or a list-shaped ``all``, can never grant). A non-dict field value is itself
    misconfigured.
    """
    if field_value is None:
        return [], False
    if not isinstance(field_value, dict):
        return [], True
    if "scopes" in field_value or "individuals" in field_value:
        mixed = any(k in _EDIT_SUBKEYS for k in field_value)
        return [field_value], mixed
    rosters: list = []
    misconfigured = False
    for k, sub in field_value.items():
        if k in _EDIT_SUBKEYS and isinstance(sub, dict):
            rosters.append(sub)
        else:
            misconfigured = True
    return rosters, misconfigured


def load_model(path: str) -> dict:
    """Load the permissions model JSON from disk; reject a non-object top level."""
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("permissions model must be a JSON object")
    return data


def resolve_allowed_emails(model, section, key, field, admins=None):
    """Return ``(allowed_emails, configured, misconfigured)`` for one roster.

    ``allowed_emails`` is a lowercased set that always includes the model's
    admins, formed from the union of the roster's scopes (expanded through
    ``departments``, string scope names naming existing groups only) and its
    explicit individuals. ``configured`` is True iff at least one non-admin
    member resolved to a usable email. ``misconfigured`` is True iff any
    container or element had a bad shape, a scope named a group that does not
    exist (including a group whose value is null or is not a member list, such as
    a bare string or object), or a named member/individual could not be resolved
    to an email; it is independent of ``configured``. Never raises: a value is
    used as a dict key only when it is a string. Pass ``admins`` to avoid
    recomputing it.
    """
    if admins is None:
        admins = _admins(model)
    departments = model.get("departments") if isinstance(model, dict) else None
    if not isinstance(departments, dict):
        departments = {}
    items = model.get(section) if (isinstance(model, dict) and isinstance(section, str)) else None
    if not isinstance(items, dict):
        items = {}
    item = items.get(key) if isinstance(key, str) else None
    if not isinstance(item, dict):
        item = {}
    field_value = item.get(field) if isinstance(field, str) else None

    rosters, misconfigured = _rosters_and_flag(field_value)
    model_emails: set[str] = set()
    for roster in rosters:
        scopes, ok = _valid_seq(roster.get("scopes"))
        misconfigured = misconfigured or not ok
        for scope in scopes:
            if not isinstance(scope, str):
                misconfigured = True
                continue
            members = departments.get(scope)
            if not isinstance(members, (list, tuple)):
                # A scope naming a group that does not exist, or whose value is
                # null / not a member list, is misconfigured either way.
                misconfigured = True
                continue
            for member in members:
                if not isinstance(member, (str, dict)):
                    misconfigured = True
                    continue
                e = _email_of(member)
                if e:
                    model_emails.add(e)
                else:
                    misconfigured = True  # a member with no usable email
        individuals, ok = _valid_seq(roster.get("individuals"))
        misconfigured = misconfigured or not ok
        for ind in individuals:
            if not isinstance(ind, (str, dict)):
                misconfigured = True
                continue
            e = _email_of(ind)
            if e:
                model_emails.add(e)
            else:
                misconfigured = True  # an individual that normalizes to empty

    configured = bool(model_emails)
    allowed = set(admins) | model_emails
    return allowed, configured, misconfigured


def groups_for_email(model: dict, email: str) -> list[str]:
    """Return the department/group names the email belongs to (may be empty)."""
    e = _norm(email)
    if not e:
        return []
    departments = model.get("departments") if isinstance(model, dict) else None
    if not isinstance(departments, dict):
        return []
    groups = []
    for group, members in departments.items():
        if not isinstance(group, str):
            continue
        if not isinstance(members, (list, tuple)):
            continue
        if any(_email_of(m) == e for m in members):
            groups.append(group)
    return sorted(groups)


def is_allowed(
    model,
    email,
    key,
    section: str = SECTION_ACCESS,
    field: str = "use",
) -> Decision:
    """Decide whether ``email`` may use ``key`` in ``section`` (FAIL-CLOSED).

    Never raises. Order of checks:
      1. ``key`` / ``section`` / ``field`` must be identifier-shaped strings ->
         else DENY ``malformed_key`` (also keeps the reason free of injected
         control characters).
      2. Admins (from the model's ``admins`` list) are recognized up front,
         independent of the rest of the model's structure.
      3. A malformed model (non-dict model, non-dict section, non-dict item)
         resolves to DENY ``malformed_model:<where>`` for non-admins.
      4. Unknown item key -> DENY (never invent access).
      5. A ``systems`` item with ``public is True`` -> ALLOW.
      6. Empty / non-string / non-ASCII / unknown requester identity -> DENY.
      7. Admin -> ALLOW; email in the resolved roster -> ALLOW; otherwise DENY
         (the reason carries ``:misconfigured`` for a mistyped/unresolvable
         roster, else ``:unconfigured`` for an empty one).
    """
    if not (isinstance(key, str) and isinstance(section, str) and isinstance(field, str)):
        return Decision(False, "denied:malformed_key")
    if not (_IDENT_RE.match(key) and _IDENT_RE.match(section) and _IDENT_RE.match(field)):
        return Decision(False, "denied:malformed_key")

    admins = _admins(model)
    e = _norm(email)
    is_admin = bool(e) and e in admins

    if not isinstance(model, dict):
        return Decision(False, "denied:malformed_model:model")

    items = model.get(section)
    if not isinstance(items, dict):
        if is_admin:
            return Decision(True, f"admin:{section}/{key}")
        return Decision(False, "denied:malformed_model:section")

    item = items.get(key)
    if item is None:
        if is_admin:
            return Decision(True, f"admin:{section}/{key}")
        return Decision(False, f"unknown:{section}/{key}")

    if not isinstance(item, dict):
        if is_admin:
            return Decision(True, f"admin:{section}/{key}")
        return Decision(False, "denied:malformed_model:item")

    if section == SECTION_SYSTEMS and item.get("public") is True:
        return Decision(True, f"public:{section}/{key}")

    if not e:
        return Decision(False, f"no-identity:{section}/{key}")

    if is_admin:
        return Decision(True, f"admin:{section}/{key}")

    allowed, configured, misconfigured = resolve_allowed_emails(
        model, section, key, field, admins=admins
    )
    if e in allowed:
        return Decision(True, f"allowed:{section}/{key}/{field}")

    reason = f"denied:{section}/{key}/{field}"
    if misconfigured:
        reason += ":misconfigured"
    elif not configured:
        reason += ":unconfigured"
    return Decision(False, reason)


def is_tool_allowed(model, email, tool_key) -> Decision:
    """Convenience wrapper for a tool-kit item (``access`` section, use)."""
    return is_allowed(model, email, tool_key, section=SECTION_ACCESS, field="use")


def tool_keys(model) -> list[str]:
    """Return the sorted string keys of the agent's tool kit (``access`` section).

    Tolerates a malformed model and non-string keys: a non-dict model or
    ``access`` section yields ``[]``, and any non-string key is skipped.
    """
    access = model.get(SECTION_ACCESS) if isinstance(model, dict) else None
    if not isinstance(access, dict):
        return []
    return sorted(k for k in access if isinstance(k, str))
