"""Tests for the opt-in access-enforcement gate (task #346, inc2a).

These pin the deployment policy around the pure decision layer: default off is
zero behavior (and never even loads the model), log-only logs but never blocks,
enforce blocks, owners are recognized from a configured admin scope even when the
model has no top-level ``admins``, an unknown requester is public-only, the
armed startup line carries the required fields, and every decision is one
fixed-schema record whose identity is a field value and never free text. The
unwired FastMCP wrapper primitive is covered too.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pinky_daemon.access_gate import (
    KIND_EMAIL,
    KIND_MIXED,
    KIND_NONE,
    KIND_UNKNOWN,
    MODE_ENFORCE,
    MODE_LOG,
    MODE_OFF,
    AccessDeniedError,
    AccessGate,
    GateConfig,
    Requester,
    make_guarded_call_tool,
    normalize_mode,
    requester_email,
    requester_unknown,
    wrap_fastmcp_tool_manager,
)

OWNER = "owner1@example.com"
STAFF = "staff1@example.com"
STRANGER = "stranger@example.com"


def _model() -> dict:
    # A model with no top-level ``admins`` list; owners are resolved from an admin
    # scope (SuperAdmin); a public system; and a tool with an empty roster that
    # therefore only admins may reach.
    return {
        "admins": None,
        "departments": {
            "SuperAdmin": [
                {"name": "Owner One", "email": OWNER},
                {"name": "Owner Two", "email": "owner2@example.com"},
            ],
            "Support": [{"name": "Staff One", "email": STAFF}],
        },
        "systems": {
            "public_form": {
                "type": "use",
                "public": True,
                "use": {"scopes": [], "individuals": []},
            },
            "support_ops": {"type": "use", "use": {"scopes": [], "individuals": [OWNER]}},
        },
        "access": {
            "rma": {"use": {"scopes": ["Support"], "individuals": []}},
            "secret_tool": {"use": {"scopes": [], "individuals": []}},
        },
    }


@pytest.fixture
def model_path(tmp_path) -> str:
    p = tmp_path / "perms.json"
    p.write_text(json.dumps(_model()))
    return str(p)


class RecordingLoader:
    """A model loader that records how many times it was called."""

    def __init__(self, model: dict) -> None:
        self.model = model
        self.calls = 0

    def __call__(self, path: str) -> dict:
        self.calls += 1
        return json.loads(json.dumps(self.model))  # fresh copy each load


class FakeLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)


def _gate(mode, model_path, **kw) -> tuple[AccessGate, FakeLogger, RecordingLoader]:
    logger = FakeLogger()
    loader = RecordingLoader(_model())
    gate = AccessGate(
        agent_name="agent-a",
        config=GateConfig(mode=mode, model_path=model_path),
        loader=loader,
        logger=logger,
        **kw,
    )
    return gate, logger, loader


# --- default off: zero behavior, and never loads the model ------------------


def test_off_never_calls_loader_and_never_blocks(model_path):
    gate, logger, loader = _gate(MODE_OFF, model_path)
    # Even a stranger reaching an admin-only tool is allowed and unlogged.
    r = gate.is_tool_allowed(STRANGER, "secret_tool")
    assert r == (MODE_OFF, False, True, "off", False)
    assert loader.calls == 0  # the model was never consulted
    gate.startup_log()
    assert logger.lines == []  # off is silent, including at startup
    assert gate.armed is False


def test_off_with_a_broken_path_is_still_inert():
    gate, logger, loader = _gate(MODE_OFF, "/no/such/file.json")
    assert gate.is_tool_allowed(STRANGER, "anything").blocked is False
    assert loader.calls == 0
    assert logger.lines == []


# --- log mode: decides and logs, never blocks -------------------------------


def test_log_mode_deny_logs_but_does_not_block(model_path):
    gate, logger, _ = _gate(MODE_LOG, model_path)
    r = gate.is_tool_allowed(STRANGER, "secret_tool")
    assert r.allow is False
    assert r.blocked is False  # log-only never blocks
    assert r.checked is True
    assert len(logger.lines) == 1
    rec = logger.lines[0]
    assert rec.startswith("access-gate DECISION ")
    assert "decision=deny" in rec
    assert "subject_kind=email" in rec
    assert f"email={STRANGER}" in rec
    assert "tool=access/secret_tool/use" in rec
    assert "mode=log" in rec


def test_log_mode_allow_logged_by_default_and_suppressible(model_path):
    # log_allows defaults on: a log-only rollout needs the allow baseline to tell
    # "nobody reached this" from "the gate is dark".
    gate, logger, _ = _gate(MODE_LOG, model_path)
    r = gate.is_tool_allowed(STAFF, "rma")  # Support may use rma
    assert r.allow is True and r.blocked is False
    assert len(logger.lines) == 1
    assert "decision=allow" in logger.lines[0]

    # It can be turned off for a noisier deployment that only wants denials.
    gate2, logger2, _ = _gate(MODE_LOG, model_path, log_allows=False)
    gate2.is_tool_allowed(STAFF, "rma")
    assert logger2.lines == []


# --- enforce mode: blocks a denial ------------------------------------------


def test_enforce_mode_blocks_denials_and_allows_grants(model_path):
    gate, logger, _ = _gate(MODE_ENFORCE, model_path)
    denied = gate.is_tool_allowed(STRANGER, "secret_tool")
    assert denied.blocked is True
    assert "decision=deny" in logger.lines[-1]

    allowed = gate.is_tool_allowed(STAFF, "rma")
    assert allowed.allow is True and allowed.blocked is False


# --- admins synthesized from the configured scope ---------------------------


def test_owner_from_admin_scope_allowed_even_for_empty_roster(model_path):
    # secret_tool has an empty roster: only admins may reach it. The model has no
    # top-level admins, so the owner is recognized purely via the SuperAdmin scope.
    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    assert gate.is_tool_allowed(OWNER, "secret_tool").allow is True
    assert gate.is_tool_allowed(OWNER, "secret_tool").blocked is False
    # A non-owner is still denied.
    assert gate.is_tool_allowed(STRANGER, "secret_tool").blocked is True


def test_custom_admin_scope(model_path):
    logger = FakeLogger()
    loader = RecordingLoader(_model())
    # Point admin scope at Support instead: now STAFF is an owner everywhere.
    gate = AccessGate(
        "agent-a",
        GateConfig(mode=MODE_ENFORCE, model_path=model_path, admin_scopes=("Support",)),
        loader=loader,
        logger=logger,
    )
    assert gate.is_tool_allowed(STAFF, "secret_tool").allow is True
    # ...and OWNER, no longer in the admin scope, is denied the empty-roster tool.
    assert gate.is_tool_allowed(OWNER, "secret_tool").blocked is True


# --- unknown requester -> public-only ---------------------------------------


def test_unknown_requester_is_public_only(model_path):
    gate, logger, _ = _gate(MODE_ENFORCE, model_path)
    # A public system is reachable by an unknown requester.
    pub = gate.check(None, "public_form", section="systems")
    assert pub.allow is True and pub.blocked is False
    # A non-public system is not.
    priv = gate.check(None, "support_ops", section="systems")
    assert priv.blocked is True
    # And a normal tool is not.
    assert gate.is_tool_allowed(None, "rma").blocked is True
    # The record marks the subject unknown with an empty email field.
    rec = logger.lines[-1]
    assert "subject_kind=unknown" in rec
    assert "email= " in rec + " "  # empty email field


def test_requester_struct_and_bare_string_agree(model_path):
    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    via_str = gate.is_tool_allowed(STAFF, "rma")
    via_struct = gate.is_tool_allowed(requester_email(STAFF), "rma")
    assert via_str.allow == via_struct.allow is True
    # An explicit unknown struct is public-only, same as None.
    assert gate.is_tool_allowed(requester_unknown(), "rma").blocked is True
    assert gate.check(Requester(KIND_UNKNOWN, ""), "public_form", section="systems").allow is True


# --- model load failure ------------------------------------------------------


def test_model_unavailable_enforce_fails_closed():
    # Real loader + a missing file, so the model genuinely fails to load.
    logger = FakeLogger()
    gate = AccessGate(
        "agent-a", GateConfig(mode=MODE_ENFORCE, model_path="/no/such/file.json"), logger=logger
    )
    r = gate.is_tool_allowed(OWNER, "rma")
    assert r.blocked is True
    assert r.reason == "denied:model_unavailable"
    assert "decision=deny" in logger.lines[-1]


def test_model_unavailable_log_does_not_block():
    logger = FakeLogger()
    gate = AccessGate(
        "agent-a", GateConfig(mode=MODE_LOG, model_path="/no/such/file.json"), logger=logger
    )
    r = gate.is_tool_allowed(OWNER, "rma")
    assert r.blocked is False
    assert r.reason == "model_unavailable"
    assert "decision=allow" in logger.lines[-1]


# --- config coercion ---------------------------------------------------------


@pytest.mark.parametrize(
    "value,expect",
    [
        ("off", MODE_OFF),
        ("log", MODE_LOG),
        ("enforce", MODE_ENFORCE),
        ("LOG", MODE_LOG),
        ("  Enforce ", MODE_ENFORCE),
        ("", MODE_OFF),
        ("bogus", MODE_OFF),
        (None, MODE_OFF),
        (5, MODE_OFF),
    ],
)
def test_normalize_mode(value, expect):
    assert normalize_mode(value) == expect


def test_gateconfig_from_dict():
    assert GateConfig.from_dict(None).mode == MODE_OFF
    assert GateConfig.from_dict("nope").mode == MODE_OFF
    c = GateConfig.from_dict(
        {"mode": "enforce", "model_path": "/p.json", "admin_scopes": ["A", 3, "", "B"]}
    )
    assert c.mode == MODE_ENFORCE
    assert c.model_path == "/p.json"
    assert c.admin_scopes == ("A", "B")
    # Bad mode falls to off; missing scopes fall to the default owner scope.
    d = GateConfig.from_dict({"mode": "sudo", "admin_scopes": "notalist"})
    assert d.mode == MODE_OFF
    assert d.admin_scopes == ("SuperAdmin",)


# --- armed startup line ------------------------------------------------------


def test_startup_log_armed_fields(model_path):
    gate, logger, _ = _gate(MODE_LOG, model_path)
    gate.startup_log()
    assert len(logger.lines) == 1
    line = logger.lines[0]
    assert "access-gate[agent-a]" in line  # agent
    assert "mode=log" in line  # mode
    assert f"model={model_path}" in line  # model path
    assert "admin_scopes=SuperAdmin" in line  # admin scope
    assert "mtime=" in line and "mtime=n/a" not in line  # model mtime
    assert "status=loaded" in line


def test_startup_log_load_failed_is_loud():
    logger = FakeLogger()
    gate = AccessGate(
        "agent-a", GateConfig(mode=MODE_ENFORCE, model_path="/no/such/file.json"), logger=logger
    )
    gate.startup_log()
    line = logger.lines[0]
    assert "status=LOAD_FAILED" in line
    assert "ENFORCE will DENY all" in line


# --- log record is a single line and identity cannot forge one --------------


def test_decision_record_is_one_sanitized_line(model_path):
    gate, logger, _ = _gate(MODE_LOG, model_path)
    # A control character that _norm keeps (ascii, not in its trim set): it must
    # be scrubbed so the email field cannot inject a second record line.
    nasty = f"a\x0c{STRANGER}"
    gate.is_tool_allowed(requester_email(nasty), "secret_tool")
    assert len(logger.lines) == 1
    rec = logger.lines[0]
    assert "\n" not in rec and "\x0c" not in rec
    assert rec.count("access-gate DECISION") == 1
    assert "subject_kind=email" in rec


# --- the unwired FastMCP wrapper primitive ----------------------------------


def test_make_guarded_call_tool_blocks_and_passes(model_path):
    calls = []

    async def inner(name, *a, **k):
        calls.append(name)
        return f"ran:{name}"

    enforce_gate, _, _ = _gate(MODE_ENFORCE, model_path)
    guarded = make_guarded_call_tool(inner, enforce_gate, lambda: requester_unknown())

    # Unknown requester + admin-only tool -> blocked, inner never runs.
    with pytest.raises(AccessDeniedError):
        asyncio.run(guarded("secret_tool"))
    assert calls == []

    # Owner -> allowed, inner runs and the result flows through.
    guarded_owner = make_guarded_call_tool(inner, enforce_gate, lambda: requester_email(OWNER))
    assert asyncio.run(guarded_owner("secret_tool", 1, x=2)) == "ran:secret_tool"
    assert calls == ["secret_tool"]


def test_log_mode_wrapper_never_raises(model_path):
    async def inner(name, *a, **k):
        return "ok"

    log_gate, _, _ = _gate(MODE_LOG, model_path)
    guarded = make_guarded_call_tool(inner, log_gate, lambda: requester_unknown())
    # Would be denied, but log-only never blocks, so the call proceeds.
    assert asyncio.run(guarded("secret_tool")) == "ok"


def test_wrap_fastmcp_tool_manager_wraps_and_unwraps(model_path):
    class FakeTM:
        def __init__(self):
            self.ran = []

        async def call_tool(self, name, *a, **k):
            self.ran.append(name)
            return "inner"

    class FakeMCP:
        def __init__(self):
            self._tool_manager = FakeTM()

    mcp = FakeMCP()
    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    unwrap = wrap_fastmcp_tool_manager(mcp, gate, lambda: requester_unknown())

    # While wrapped, a blocked tool raises before the inner call runs.
    with pytest.raises(AccessDeniedError):
        asyncio.run(mcp._tool_manager.call_tool("secret_tool"))
    assert mcp._tool_manager.ran == []

    # After unwrap the original behavior is restored: the same call now runs the
    # inner method and does not block (identity is not asserted because bound
    # methods are recreated on each attribute access).
    unwrap()
    assert asyncio.run(mcp._tool_manager.call_tool("secret_tool")) == "inner"
    assert mcp._tool_manager.ran == ["secret_tool"]


def test_wrap_fastmcp_tool_manager_refuses_bad_shape():
    class NoTM:
        pass

    gate = AccessGate("agent-a", GateConfig(mode=MODE_ENFORCE))
    with pytest.raises(AttributeError):
        wrap_fastmcp_tool_manager(NoTM(), gate, lambda: requester_unknown())


# --- P2/P3 hardening: kind guard, mode strictness, fold, reload, firewall -----


def test_non_email_kinds_are_public_only(model_path):
    # M6: the kind==email guard keeps a non-email subject public-only even when an
    # email happens to ride along on the requester struct.
    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    for kind in (KIND_NONE, KIND_MIXED, KIND_UNKNOWN):
        r = gate.is_tool_allowed(Requester(kind, OWNER), "secret_tool")
        assert r.blocked is True  # OWNER's email is ignored for a non-email kind
    # A public system is still reachable regardless of (non-email) kind...
    assert gate.check(Requester(KIND_NONE, OWNER), "public_form", section="systems").allow is True
    # ...and the email kind, by contrast, is honored.
    assert gate.is_tool_allowed(Requester(KIND_EMAIL, OWNER), "secret_tool").blocked is False


def test_record_subject_kind_is_validated_and_sanitized(model_path):
    # A crafted kind must neither forge a second record line nor appear verbatim;
    # an out-of-set kind is coerced to 'unknown'.
    gate, logger, _ = _gate(MODE_LOG, model_path)
    gate.is_tool_allowed(Requester("email\ndecision=allow reason=forged", STRANGER), "secret_tool")
    rec = logger.lines[-1]
    assert "\n" not in rec
    assert rec.count("access-gate DECISION") == 1
    assert "subject_kind=unknown" in rec  # crafted kind coerced


def test_uppercase_mode_is_normalized_and_fully_armed(model_path):
    # M12: a mode differing only by case must not read the model and log denials
    # while never blocking and printing no ARMED line. Normalizing at construction
    # keeps check/armed/startup_log in agreement.
    gate, logger, _ = _gate("ENFORCE", model_path)
    assert gate.mode == MODE_ENFORCE and gate.armed is True
    assert gate.is_tool_allowed(STRANGER, "secret_tool").blocked is True
    gate.startup_log()
    assert any("ARMED mode=enforce" in ln for ln in logger.lines)


def test_gateconfig_rejects_out_of_set_mode():
    assert GateConfig(mode="  Enforce ").mode == MODE_ENFORCE  # case/space normalized
    for bad in ("sudo", "on", "", "offf"):
        with pytest.raises(ValueError):
            GateConfig(mode=bad)


def test_admin_scope_fold_does_not_mutate_loaded_model(model_path):
    # The loader's returned object must be left unmutated so two gates over the
    # same loaded model cannot leak admins into each other or grow the set.
    raw = _model()

    class SharedLoader:
        def __call__(self, path):
            return raw  # deliberately returns THE SAME object each call

    gate = AccessGate(
        "a1", GateConfig(mode=MODE_ENFORCE, model_path=model_path), loader=SharedLoader()
    )
    assert gate.is_tool_allowed(OWNER, "secret_tool").blocked is False  # fold worked on a copy
    assert raw.get("admins") in (None, [])  # ...but the loader's object is untouched


def test_model_reload_on_mtime_change_revokes(tmp_path):
    import json as _json
    import os as _os
    import time as _time

    p = tmp_path / "perms.json"
    p.write_text(_json.dumps(_model()))
    logger = FakeLogger()
    gate = AccessGate("agent-a", GateConfig(mode=MODE_ENFORCE, model_path=str(p)), logger=logger)
    assert gate.is_tool_allowed(STAFF, "rma").blocked is False  # Support may use rma
    # Revoke on disk: rma now has an empty roster (admins only); bump mtime.
    m2 = _model()
    m2["access"]["rma"]["use"]["scopes"] = []
    p.write_text(_json.dumps(m2))
    future = _time.time() + 2
    _os.utime(p, (future, future))
    assert gate.is_tool_allowed(STAFF, "rma").blocked is True  # revocation took effect
    assert any("RELOAD" in ln and "status=reloaded" in ln for ln in logger.lines)


def test_reload_failure_fails_closed_under_enforce(tmp_path):
    import json as _json
    import os as _os
    import time as _time

    p = tmp_path / "perms.json"
    p.write_text(_json.dumps(_model()))
    logger = FakeLogger()
    gate = AccessGate("agent-a", GateConfig(mode=MODE_ENFORCE, model_path=str(p)), logger=logger)
    assert gate.is_tool_allowed(OWNER, "secret_tool").blocked is False  # loaded fine
    p.write_text("{ not valid json")  # corrupt, then bump mtime so reload is tried
    future = _time.time() + 2
    _os.utime(p, (future, future))
    r = gate.is_tool_allowed(OWNER, "secret_tool")
    assert r.blocked is True and r.reason == "denied:model_unavailable"
    assert any("RELOAD" in ln and "reload_failed" in ln for ln in logger.lines)


def test_off_wrapper_returns_before_resolving_requester(model_path):
    ran, resolved = [], []

    async def inner(name, *a, **k):
        ran.append(name)
        return "ran"

    def resolver():
        resolved.append(1)
        return requester_unknown()

    gate, _, loader = _gate(MODE_OFF, model_path)
    guarded = make_guarded_call_tool(inner, gate, resolver)
    assert asyncio.run(guarded("secret_tool")) == "ran"
    assert resolved == []  # off never resolves the requester
    assert loader.calls == 0  # ...and never loads the model
    assert ran == ["secret_tool"]


def test_wrapper_firewall_enforce_denies_on_gate_error(model_path):
    async def inner(name, *a, **k):
        return "ran"

    def boom():
        raise RuntimeError("resolver down")

    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    guarded = make_guarded_call_tool(inner, gate, boom)
    with pytest.raises(AccessDeniedError) as ei:
        asyncio.run(guarded("secret_tool"))
    assert ei.value.reason == "denied:gate_error:RuntimeError"  # type name only, no message


def test_wrapper_firewall_log_allows_on_gate_error(model_path):
    ran = []

    async def inner(name, *a, **k):
        ran.append(name)
        return "ran"

    def boom():
        raise RuntimeError("resolver down")

    gate, _, _ = _gate(MODE_LOG, model_path)
    guarded = make_guarded_call_tool(inner, gate, boom)
    assert asyncio.run(guarded("secret_tool")) == "ran"  # log never blocks
    assert ran == ["secret_tool"]


def test_dead_logger_never_breaks_a_decision(model_path):
    def dead(msg):
        raise BrokenPipeError("stderr closed")

    loader = RecordingLoader(_model())
    gate = AccessGate(
        "agent-a",
        GateConfig(mode=MODE_ENFORCE, model_path=model_path),
        loader=loader,
        logger=dead,
    )
    r = gate.is_tool_allowed(STRANGER, "secret_tool")
    assert r.blocked is True  # the denial still returns though every log call raises


def test_startup_log_emits_armed_line_once(model_path):
    gate, logger, _ = _gate(MODE_LOG, model_path)
    gate.startup_log()
    gate.startup_log()
    assert sum("ARMED" in ln for ln in logger.lines) == 1


def test_wrap_fastmcp_tool_manager_refuses_double_wrap(model_path):
    class FakeTM:
        async def call_tool(self, name, *a, **k):
            return "inner"

    class FakeMCP:
        def __init__(self):
            self._tool_manager = FakeTM()

    mcp = FakeMCP()
    gate, _, _ = _gate(MODE_ENFORCE, model_path)
    wrap_fastmcp_tool_manager(mcp, gate, lambda: requester_unknown())
    with pytest.raises(RuntimeError):
        wrap_fastmcp_tool_manager(mcp, gate, lambda: requester_unknown())


# --- r3 receipts: each reddens if its fix is reverted -----------------------


def test_safe_never_raises_on_a_raising_str():
    # Receipt for fix 1: _safe builds every log line, including the gate-error
    # record emitted from inside an except; if str() is un-wrapped, a value whose
    # __str__ raises makes _safe raise and defeats the firewalls that call it.
    from pinky_daemon.access_gate import _safe

    class Boom:
        def __str__(self):
            raise RuntimeError("no str for you")

    assert _safe(Boom()) == "<Boom>"  # type-name fallback, and crucially no raise


def test_check_firewall_enforce_denies_when_pure_layer_raises(model_path, monkeypatch):
    # Receipt for fix 2: check() (the documented choke point) has its own firewall,
    # not only the FastMCP wrapper. A raise out of the pure layer must become a
    # fail-closed deny, never propagate out of check().
    def boom(*a, **k):
        raise RuntimeError("pure layer down")

    monkeypatch.setattr("pinky_daemon.access_gate.is_allowed", boom)
    gate, logger, _ = _gate(MODE_ENFORCE, model_path)
    r = gate.is_tool_allowed(OWNER, "secret_tool")  # called directly, not via the wrapper
    assert r.blocked is True
    assert r.reason == "denied:gate_error:RuntimeError"  # type name only
    assert any("GATE-ERROR" in ln for ln in logger.lines)


def test_check_firewall_log_allows_when_pure_layer_raises(model_path, monkeypatch):
    # Same firewall, log side: a raise is non-blocking and recorded, never raised.
    def boom(*a, **k):
        raise RuntimeError("pure layer down")

    monkeypatch.setattr("pinky_daemon.access_gate.is_allowed", boom)
    gate, logger, _ = _gate(MODE_LOG, model_path)
    r = gate.is_tool_allowed(OWNER, "secret_tool")
    assert r.blocked is False and r.reason == "denied:gate_error:RuntimeError"
    assert any("GATE-ERROR" in ln for ln in logger.lines)


def test_subsecond_reload_revokes(tmp_path):
    # Receipt for fix 3: detection keys on (st_mtime_ns, st_size, st_ino), so a
    # revocation whose mtime lands in the SAME whole second as the last load is
    # still reloaded. A second-resolution key would serve the stale model.
    import json as _json
    import os as _os

    p = tmp_path / "perms.json"
    p.write_text(_json.dumps(_model()))
    base = 1_600_000_000  # a fixed whole second
    _os.utime(p, (base, base))
    logger = FakeLogger()
    gate = AccessGate("agent-a", GateConfig(mode=MODE_ENFORCE, model_path=str(p)), logger=logger)
    assert gate.is_tool_allowed(STAFF, "rma").blocked is False  # Support may use rma

    m2 = _model()
    m2["access"]["rma"]["use"]["scopes"] = []  # revoke on disk
    p.write_text(_json.dumps(m2))
    _os.utime(p, (base + 0.4, base + 0.4))  # same whole second, sub-second delta only
    assert gate.is_tool_allowed(STAFF, "rma").blocked is True  # detected and reloaded
    assert any("RELOAD" in ln and "status=reloaded" in ln for ln in logger.lines)


def test_reload_record_emitted_outside_lock(tmp_path):
    # Receipt for fix 4: the RELOAD record is emitted AFTER the lock is released,
    # so a re-entrant or slow logger cannot deadlock or serialize behind it. If it
    # were emitted while the lock is held, the non-blocking acquire below returns
    # False (threading.Lock is not reentrant) and the assertion reddens.
    import json as _json
    import os as _os
    import time as _time

    p = tmp_path / "perms.json"
    p.write_text(_json.dumps(_model()))
    gate = AccessGate("agent-a", GateConfig(mode=MODE_ENFORCE, model_path=str(p)))
    lock_free_during_emit: list[bool] = []

    def logger(msg):
        if "RELOAD" in msg:
            got = gate._lock.acquire(blocking=False)
            lock_free_during_emit.append(got)
            if got:
                gate._lock.release()

    gate.logger = logger
    gate.is_tool_allowed(OWNER, "secret_tool")  # initial load, no RELOAD yet
    m2 = _model()
    m2["access"]["rma"]["use"]["scopes"] = []
    p.write_text(_json.dumps(m2))
    future = _time.time() + 2
    _os.utime(p, (future, future))
    gate.is_tool_allowed(OWNER, "secret_tool")  # triggers the reload + RELOAD record
    assert lock_free_during_emit == [True]  # the lock was free when the record was emitted
