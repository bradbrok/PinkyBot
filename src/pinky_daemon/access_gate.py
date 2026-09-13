"""Opt-in, per-agent deployment policy around the pure access decision (#346).

The pure-logic layer in :mod:`pinky_daemon.access_enforcement` answers one
question with no side effects: may a *verified* requester email use a given tool
or system key, according to the permissions model? This module is the thin,
opt-in policy that a deployment wraps around that answer:

  * **Always opt-in, default off.** Each agent carries a mode of ``off`` /
    ``log`` / ``enforce`` (default ``off``). ``off`` never consults the model and
    never blocks, so a fleet that has not opted in behaves exactly as before.
  * **Private model, loaded from a configured path.** The roster is not bundled
    in the repo; the gate loads it from ``model_path`` at the agent's request,
    and re-loads it when the file changes on disk so a revocation takes effect
    without a restart.
  * **Owner set synthesized from a scope.** A model may carry no top-level
    ``admins`` list; in that case admins are synthesized from configured admin
    scope(s) (default ``SuperAdmin``). ``admin_scopes`` names the group(s) whose
    members are always allowed, and the gate folds them into a *copy* of the
    model's ``admins`` before deciding, so such a model does not lock its owners
    out and two gates never leak admins into each other's loaded model.
  * **Log-only first.** In ``log`` mode a would-be denial is logged but never
    blocks; only ``enforce`` blocks. This is how a deployment shakes out model or
    identity mismatches before turning enforcement on.
  * **A loud armed line.** When armed (``log`` or ``enforce``) the gate emits a
    startup log line (once) so an armed agent is never a surprise.
  * **A fixed decision-record schema.** Every logged decision is one record with
    the same fields from day one (agent, tool key, mode, subject_kind, email,
    decision, reason). Every value is sanitized to one line, and the requester
    identity (kind and email) is only ever a field value, never interpolated into
    free text, so a later increment can add subject kinds without breaking the
    log schema or a parser built on it.
  * **A firewall around the envelope.** The pure decision is fail-closed on its
    own; the policy around it must never fail *open* nor break a tool call. Off
    short-circuits before touching anything; an unexpected error inside the gate
    denies under ``enforce`` (type name only in the reason) and allows-with-a-loud
    record under ``log``; and the logger itself can never raise out.

Scope of this module (inc2a): the decision + logging policy, plus an *unwired*
thin FastMCP wrapper primitive. It performs no identity resolution and is not
called from anywhere yet. The caller supplies the requester as a
:class:`Requester` (kind + email) or a bare email string / ``None``; ``None`` or
an unresolvable email is treated as ``unknown`` and yields public-only access.
Resolving the requester from the message layer, threading it to the tool layer,
and choosing the choke point are later increments (2b/2c). The core (this module
plus ``access_enforcement``) imports only the standard library, so it can be
vendored into another MCP package (e.g. the private zoho-mcp) against the same
model file.
"""

from __future__ import annotations

import datetime
import os
import threading
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable

from pinky_daemon.access_enforcement import (
    SECTION_ACCESS,
    SECTION_SYSTEMS,
    _admins,
    _norm,
    is_allowed,
    load_model,
)

# Enforcement modes, in increasing strength. ``off`` is the default and the only
# state in which the gate consults nothing and can never block.
MODE_OFF = "off"
MODE_LOG = "log"
MODE_ENFORCE = "enforce"
VALID_MODES = (MODE_OFF, MODE_LOG, MODE_ENFORCE)
# The one shared whitelist of "the gate will act" modes. ``armed``, ``check`` and
# ``startup_log`` all read from this so they can never disagree about whether a
# given mode is live.
ARMED_MODES = frozenset({MODE_LOG, MODE_ENFORCE})

# Subject kinds for the requester. inc2a only ever emits ``email`` or
# ``unknown``; ``none`` (autonomous/cron, no author) and ``mixed`` (a turn whose
# messages came from more than one sender) are reserved for inc2b so the log
# schema and the wrapper signature do not change when they arrive.
KIND_EMAIL = "email"
KIND_UNKNOWN = "unknown"
KIND_NONE = "none"
KIND_MIXED = "mixed"
SUBJECT_KINDS = (KIND_EMAIL, KIND_UNKNOWN, KIND_NONE, KIND_MIXED)

# The default group scope treated as always-allowed owners when a model carries
# no top-level ``admins`` list. Configurable per agent via GateConfig.
DEFAULT_ADMIN_SCOPES = ("SuperAdmin",)

# The verified requester of a decision: a kind plus the raw email (empty unless
# kind is ``email``). A typed struct from day one so inc2b can add kinds without
# changing any signature. Build via the module factories below.
Requester = namedtuple("Requester", ["kind", "email"])


def requester_email(email: str) -> Requester:
    """A resolved requester. Falls back to ``unknown`` if the email is unusable."""
    return Requester(KIND_EMAIL, email) if _norm(email) else Requester(KIND_UNKNOWN, "")


def requester_unknown() -> Requester:
    """Identity unavailable or ambiguous: yields public-only access."""
    return Requester(KIND_UNKNOWN, "")


def _log(msg: str) -> None:
    """Default logger: one line to stderr, matching the daemon convention."""
    import sys

    print(msg, file=sys.stderr, flush=True)


def _safe(value: object, limit: int = 200) -> str:
    """Render a value for one log field without letting it forge a line.

    Non-printable characters (newline included) are replaced so a crafted email
    or reason cannot inject a second, fake record into a log-only deployment, and
    the value is length-capped. Returns ``""`` for an empty value (an empty field
    is meaningful, e.g. no email for an ``unknown`` subject).

    ``str(value)`` is itself guarded: a value whose ``__str__`` raises falls back
    to its type name. This function must never raise, because it builds every log
    line including the gate-error record, and a raise here would defeat the
    firewalls that call it from inside an ``except``.
    """
    if value is None:
        return ""
    try:
        s = str(value)
    except Exception:
        s = f"<{type(value).__name__}>"
    if not s:
        return ""
    out = "".join(ch if ch.isprintable() else "?" for ch in s)
    return out[:limit]


def _safe_kind(kind: object) -> str:
    """Coerce a subject kind to a known constant; anything else is ``unknown``.

    The record's ``subject_kind`` is the one field whose value comes straight off
    a caller-built :class:`Requester`, so it is both validated against
    :data:`SUBJECT_KINDS` (an unexpected value can never appear in the log) and
    still passed through :func:`_safe` at the call site (belt and suspenders).
    """
    return kind if kind in SUBJECT_KINDS else KIND_UNKNOWN


def _iso_mtime(path: str) -> str:
    """Return the file's mtime as a UTC ISO-8601 second-resolution string."""
    ts = os.path.getmtime(path)
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec="seconds")


def normalize_mode(value: object) -> str:
    """Coerce any stored value to a valid mode; unknown/garbage becomes ``off``.

    Fail-safe on the *config* axis when reading persisted config (see
    :meth:`GateConfig.from_dict`): a corrupt or unexpected stored mode must never
    silently arm enforcement, so anything not exactly ``log`` or ``enforce`` (case
    and surrounding space aside) resolves to ``off``. Programmatic construction of
    a :class:`GateConfig` is stricter and rejects garbage outright.
    """
    if not isinstance(value, str):
        return MODE_OFF
    v = value.strip().lower()
    return v if v in VALID_MODES else MODE_OFF


def _as_requester(r: object) -> Requester:
    """Coerce a caller value to a :class:`Requester`.

    Accepts a :class:`Requester` (used as-is), a bare email string (``email`` kind,
    or ``unknown`` if it does not normalize), or ``None``/any other type
    (``unknown``). Fail-closed: anything unrecognized becomes ``unknown``, which
    is public-only.
    """
    if isinstance(r, Requester):
        return r
    if isinstance(r, str):
        return requester_email(r)
    return requester_unknown()


@dataclass(frozen=True)
class GateConfig:
    """Per-agent enforcement config (the opt-in knob).

    ``mode`` is off/log/enforce (default off). ``model_path`` is the permissions
    model JSON on disk. ``admin_scopes`` names the group(s) whose members are
    always allowed (default ``SuperAdmin``).

    Programmatic construction is strict: ``mode`` is normalized (case and
    surrounding space) once at construction and anything outside
    ``{off, log, enforce}`` raises :class:`ValueError`, so a typo like
    ``"ENFORCE"`` can never yield a config that ``check`` treats as live while
    ``armed`` treats as off. Reading persisted config that may be corrupt goes
    through :meth:`from_dict`, which coerces a bad mode to ``off`` instead.
    """

    mode: str = MODE_OFF
    model_path: str = ""
    admin_scopes: tuple[str, ...] = DEFAULT_ADMIN_SCOPES

    def __post_init__(self) -> None:
        mode = self.mode
        norm = mode.strip().lower() if isinstance(mode, str) else mode
        if norm not in VALID_MODES:
            raise ValueError(f"invalid gate mode {mode!r}; expected one of {VALID_MODES}")
        if norm != mode:
            object.__setattr__(self, "mode", norm)

    @classmethod
    def from_dict(cls, data: object) -> "GateConfig":
        """Build a config from a stored JSON blob, tolerating a bad shape.

        A non-dict blob yields the all-default (``off``) config. A bad ``mode`` is
        coerced to ``off`` (not raised) because persisted config may be corrupt
        and must fail safe rather than crash startup. ``admin_scopes`` accepts a
        list/tuple of strings and drops non-strings; an absent or malformed value
        falls back to the default owner scope.
        """
        if not isinstance(data, dict):
            return cls()
        mode = normalize_mode(data.get("mode"))
        raw_path = data.get("model_path")
        model_path = raw_path if isinstance(raw_path, str) else ""
        raw_scopes = data.get("admin_scopes")
        if isinstance(raw_scopes, (list, tuple)):
            scopes = tuple(s for s in raw_scopes if isinstance(s, str) and s)
            admin_scopes = scopes if scopes else DEFAULT_ADMIN_SCOPES
        else:
            admin_scopes = DEFAULT_ADMIN_SCOPES
        return cls(mode=mode, model_path=model_path, admin_scopes=admin_scopes)


# One gate answer. ``checked`` is whether the model was actually consulted;
# ``allow`` is the underlying permission decision (True when not checked);
# ``blocked`` is the only field a caller must act on: True means refuse the call.
GateResult = namedtuple("GateResult", ["mode", "checked", "allow", "reason", "blocked"])


class AccessDeniedError(Exception):
    """Raised by the FastMCP wrapper when an enforced decision blocks a call."""

    def __init__(self, tool_key: object, reason: str) -> None:
        self.tool_key = tool_key
        self.reason = reason
        super().__init__(f"access denied: {tool_key} ({reason})")


@dataclass
class AccessGate:
    """A per-agent gate: model + mode + logging around the pure decision.

    Construct one per agent from its :class:`GateConfig`. The model is loaded
    lazily on first use (and by :meth:`startup_log`), cached, and reloaded when
    the file's mtime changes; a load or reload failure is captured, never raised,
    and fails closed under ``enforce``. Call :meth:`check` at the tool choke
    point. Load and reload are serialized under a lock so concurrent checks are
    safe.
    """

    agent_name: str
    config: GateConfig
    loader: Callable[[str], dict] = load_model
    logger: Callable[[str], None] = _log
    # Log allow decisions too, not only denials. Default on: a log-only rollout
    # needs the allow baseline to tell "nobody hit this" from "the gate is dark".
    log_allows: bool = True

    _loaded: bool = field(default=False, init=False, repr=False)
    _model: dict | None = field(default=None, init=False, repr=False)
    _load_error: str = field(default="", init=False, repr=False)
    # Reload detection key: (st_mtime_ns, st_size, st_ino), which changes on a
    # same-second rewrite that a second-resolution mtime would miss. The ISO
    # ``_model_mtime`` beside it is for DISPLAY in log records only, never the key.
    _model_key: object = field(default="", init=False, repr=False)
    _model_mtime: str = field(default="", init=False, repr=False)
    _armed_logged: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def mode(self) -> str:
        return self.config.mode

    @property
    def armed(self) -> bool:
        """True iff the gate will act (log or enforce). ``off`` is not armed."""
        return self.config.mode in ARMED_MODES

    def _emit(self, msg: str) -> None:
        """Emit one log line. A dead or raising logger must never break a decision.

        The default logger prints to stderr, which raises on a closed pipe; and a
        custom logger is caller code. Neither may propagate out of a gate call, so
        every log line goes through here.
        """
        try:
            self.logger(msg)
        except Exception:
            pass

    def _stat_key(self, path: str) -> object:
        """The reload-detection key: ``(st_mtime_ns, st_size, st_ino)``.

        Sub-second and content-aware, so a same-second rewrite or an in-place
        edit that leaves the second-resolution mtime unchanged is still detected
        and reloaded (a second-resolution mtime would serve the stale model). A
        stable ``"MISSING"`` sentinel (rather than a raise) so detection compares
        two values and treats a vanished file as one distinct, once-recorded state.
        """
        try:
            st = os.stat(path)
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except Exception:
            return "MISSING"

    def _stat(self, path: str) -> str:
        """The file's mtime as an ISO string for DISPLAY, or ``"MISSING"``.

        Used only in log records (never for reload detection, which keys on
        :meth:`_stat_key`); a vanished file renders as the same sentinel.
        """
        try:
            return _iso_mtime(path)
        except Exception:
            return "MISSING"

    def _load_locked(self) -> None:
        """Load the model from disk. Caller must hold ``self._lock``.

        Folds admin scopes onto a copy, records the source mtime, and captures any
        failure (missing file, bad JSON, non-object top level) instead of raising.
        """
        path = self.config.model_path
        if not path:
            self._model = None
            self._load_error = "no model_path configured"
            self._model_key = ""
            self._model_mtime = ""
            return
        try:
            model = self.loader(path)
        except Exception as e:
            self._model = None
            self._load_error = f"{type(e).__name__}: {e}"
        else:
            self._model = self._apply_admin_scopes(model)
            self._load_error = ""
        # Detection key and display string are read together so a reload that
        # updates one always updates the other.
        self._model_key = self._stat_key(path)
        self._model_mtime = self._stat(path)

    def _ensure_loaded(self) -> None:
        """Load the model once, thread-safely; capture any failure, never raise."""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            self._load_locked()
            self._loaded = True

    def _maybe_reload(self) -> None:
        """Reload if the model file changed on disk since the last load.

        So a permissions edit or revocation takes effect without a restart, even
        a same-second one (detection keys on :meth:`_stat_key`, not the
        second-resolution display mtime). A reload failure fails closed: the model
        becomes unavailable and ``enforce`` then denies. Emits exactly one RELOAD
        record per file-state change (checked once cheaply, then re-checked under
        the lock). The record is built under the lock but emitted AFTER releasing
        it, so a re-entrant or slow logger cannot deadlock or serialize behind it.
        """
        path = self.config.model_path
        if not path:
            return
        if self._stat_key(path) == self._model_key:
            return
        log_line = None
        with self._lock:
            if self._stat_key(path) == self._model_key:
                return
            prev = self._model_mtime
            self._load_locked()
            if self._model is None:
                log_line = (
                    f"access-gate RELOAD agent={_safe(self.agent_name)} "
                    f"model={_safe(self.config.model_path)} mode={self.config.mode} "
                    f"status=reload_failed mtime={_safe(prev)}->{_safe(self._model_mtime)} "
                    f"error={_safe(self._load_error)}"
                )
            else:
                log_line = (
                    f"access-gate RELOAD agent={_safe(self.agent_name)} "
                    f"model={_safe(self.config.model_path)} mode={self.config.mode} "
                    f"status=reloaded mtime={_safe(prev)}->{_safe(self._model_mtime)} "
                    f"admins={len(_admins(self._model))}"
                )
        self._emit(log_line)

    def _apply_admin_scopes(self, model: dict) -> dict:
        """Return a copy of ``model`` with ``admin_scopes`` folded into ``admins``.

        Never mutates the loader's object, so two gates sharing a loaded model (or
        a reload of the same file) cannot leak admins into each other or grow the
        set on repeat. Extends any existing ``admins`` (never shrinks it) with the
        raw members of each named department, leaving normalization/dedup to the
        pure layer's ``_admins``. A scope naming a missing or non-list group
        contributes nothing. Returns ``model`` unchanged only when it is not a
        dict or there are no scopes to fold.
        """
        if not isinstance(model, dict) or not self.config.admin_scopes:
            return model
        departments = model.get("departments")
        if not isinstance(departments, dict):
            departments = {}
        existing = model.get("admins")
        merged = list(existing) if isinstance(existing, (list, tuple)) else []
        for scope in self.config.admin_scopes:
            members = departments.get(scope)
            if isinstance(members, (list, tuple)):
                merged.extend(members)
        new_model = dict(model)
        new_model["admins"] = merged
        return new_model

    def startup_log(self) -> None:
        """Emit the loud armed line once (and any load problem). No-op when off."""
        if not self.armed or self._armed_logged:
            return
        self._armed_logged = True
        self._ensure_loaded()
        scopes = ",".join(self.config.admin_scopes) or "<none>"
        if self._model is None:
            tail = (
                "ENFORCE will DENY all until the model loads"
                if self.config.mode == MODE_ENFORCE
                else "log-only, decisions cannot be evaluated"
            )
            self._emit(
                f"access-gate[{_safe(self.agent_name)}]: ARMED mode={self.config.mode} "
                f"model={_safe(self.config.model_path)} status=LOAD_FAILED "
                f"admin_scopes={_safe(scopes)} mtime=n/a "
                f"error={_safe(self._load_error)} -- {tail}"
            )
            return
        self._emit(
            f"access-gate[{_safe(self.agent_name)}]: ARMED mode={self.config.mode} "
            f"model={_safe(self.config.model_path)} status=loaded "
            f"admin_scopes={_safe(scopes)} mtime={_safe(self._model_mtime)} "
            f"admins={len(_admins(self._model))}"
        )

    def check(
        self,
        requester: object,
        key: object,
        section: str = SECTION_ACCESS,
        field: str = "use",  # noqa: A002 - matches the pure layer's parameter name
    ) -> GateResult:
        """Decide, log one fixed-schema record, and report whether to block.

        ``requester`` is a :class:`Requester`, a bare email string, or ``None``
        (unknown -> public-only). ``off`` short-circuits to allow without touching
        the model. Otherwise the model is loaded (and reloaded if it changed on
        disk) and the pure layer decides using the requester's email only when its
        kind is ``email``; a denial is logged (WOULD-BLOCK in ``log``, BLOCKED in
        ``enforce``) and only ``enforce`` sets ``blocked``. A model that could not
        load or reload denies under ``enforce`` (fail-closed) and is a
        non-blocking, logged error under ``log``.

        This documented choke point carries its own firewall: any unexpected
        raise (a requester or key whose ``__str__`` raises, a raise out of the
        pure layer or a reload) is caught, denied under ``enforce`` and allowed
        non-blocking under ``log`` with one GATE-ERROR record, so ``check`` never
        raises. The FastMCP wrapper keeps its own firewall as belt-and-suspenders.
        """
        mode = self.config.mode
        if mode == MODE_OFF:
            return GateResult(MODE_OFF, False, True, "off", False)

        try:
            req = _as_requester(requester)
            effective_email = req.email if req.kind == KIND_EMAIL else ""
            log_email = _norm(effective_email)
            tool_key = f"{section}/{key}/{field}"

            self._ensure_loaded()
            self._maybe_reload()
            if self._model is None:
                if mode == MODE_ENFORCE:
                    reason = "denied:model_unavailable"
                    self._record(req.kind, tool_key, log_email, "deny", reason)
                    return GateResult(mode, False, False, reason, True)
                self._record(req.kind, tool_key, log_email, "allow", "model_unavailable")
                return GateResult(mode, False, True, "model_unavailable", False)

            decision = is_allowed(self._model, effective_email, key, section=section, field=field)
            blocked = (mode == MODE_ENFORCE) and not decision.allow
            if not decision.allow:
                self._record(req.kind, tool_key, log_email, "deny", decision.reason)
            elif self.log_allows:
                self._record(req.kind, tool_key, log_email, "allow", decision.reason)
            return GateResult(mode, True, decision.allow, decision.reason, blocked)
        except Exception as e:
            # Fail closed under enforce, non-blocking under log; type name only so
            # a crafted __str__ cannot forge or bloat the record.
            reason = f"denied:gate_error:{type(e).__name__}"
            blocked = mode == MODE_ENFORCE
            self.record_gate_error(key, reason, blocked)
            return GateResult(mode, False, not blocked, reason, blocked)

    def is_tool_allowed(self, requester: object, tool_key: object) -> GateResult:
        """Convenience wrapper for a tool-kit item (``access`` section, use)."""
        return self.check(requester, tool_key, section=SECTION_ACCESS, field="use")

    def _record(self, subject_kind, tool_key, email, decision, reason) -> None:
        """Emit one fixed-schema decision record. Identity is only a field value."""
        self._emit(
            f"access-gate DECISION agent={_safe(self.agent_name)} "
            f"tool={_safe(tool_key)} mode={self.config.mode} "
            f"subject_kind={_safe(_safe_kind(subject_kind))} email={_safe(email)} "
            f"decision={decision} reason={_safe(reason)}"
        )

    def record_gate_error(self, tool_key: object, reason: str, blocked: bool) -> None:
        """One loud record when a firewall (``check`` or the wrapper) caught an
        unexpected failure. ``tool_key`` is rendered through :func:`_safe`, so a
        raw key whose ``__str__`` raises is still recorded without re-raising."""
        self._emit(
            f"access-gate GATE-ERROR agent={_safe(self.agent_name)} "
            f"tool={_safe(tool_key)} mode={self.config.mode} "
            f"reason={_safe(reason)} action={'blocked' if blocked else 'allowed'}"
        )


def make_guarded_call_tool(
    inner_call: Callable,
    gate: AccessGate,
    get_requester: Callable[[], object],
) -> Callable:
    """Wrap a FastMCP tool-manager ``call_tool`` with a gate check.

    Returns an ``async`` callable with the same ``(name, *args, **kwargs)`` shape.
    ``off`` short-circuits to the inner call before resolving the requester or
    touching the gate. Otherwise it resolves the requester via ``get_requester``
    (which returns a :class:`Requester`), runs the gate, and raises
    :class:`AccessDeniedError` only when the decision is ``blocked`` (enforce +
    deny). One firewall wraps the whole gate evaluation: an unexpected error
    (a raising resolver, a key whose ``__str__`` raises, a raise inside the pure
    layer) denies under ``enforce`` with a type-name-only reason and allows under
    ``log`` with one loud record, so the gate can never fail open nor break a
    tool call. Pure and testable: pass any ``inner_call`` and a fake gate. Not
    wired anywhere in inc2a.
    """

    async def guarded(name, *args, **kwargs):
        # Off never consults anything, not even the requester resolver.
        if not gate.armed:
            return await inner_call(name, *args, **kwargs)
        try:
            requester = get_requester() if callable(get_requester) else get_requester
            result = gate.is_tool_allowed(requester, name)
            blocked, reason = result.blocked, result.reason
        except Exception as e:
            reason = f"denied:gate_error:{type(e).__name__}"
            blocked = gate.mode == MODE_ENFORCE
            gate.record_gate_error(name, reason, blocked)
        if blocked:
            raise AccessDeniedError(name, reason)
        return await inner_call(name, *args, **kwargs)

    guarded._access_gate_wrapped = True
    return guarded


def wrap_fastmcp_tool_manager(
    mcp: object,
    gate: AccessGate,
    get_requester: Callable[[], object],
) -> Callable[[], None]:
    """Install :func:`make_guarded_call_tool` over ``mcp._tool_manager.call_tool``.

    The single narrowest per-server seam through which every registered
    ``@mcp.tool()`` call passes. Returns an ``unwrap()`` that restores the
    original. Raises ``AttributeError`` if the manager shape is not present, so a
    silent no-op can never leave a server it was asked to guard unguarded, and
    ``RuntimeError`` if ``call_tool`` is already gate-wrapped, so a double wrap
    can never stack two gates or capture a guarded callable as the "original".
    Thin and generic: usable by any FastMCP server (PinkyBot or the private
    zoho-mcp). Not called anywhere in inc2a; wiring is inc2c.
    """
    tm = getattr(mcp, "_tool_manager", None)
    if tm is None or not callable(getattr(tm, "call_tool", None)):
        raise AttributeError("mcp._tool_manager.call_tool not found; cannot wrap")
    original = tm.call_tool
    if getattr(original, "_access_gate_wrapped", False):
        raise RuntimeError("mcp._tool_manager.call_tool is already access-gate wrapped")
    tm.call_tool = make_guarded_call_tool(original, gate, get_requester)

    def unwrap() -> None:
        tm.call_tool = original

    return unwrap


__all__ = [
    "MODE_OFF",
    "MODE_LOG",
    "MODE_ENFORCE",
    "VALID_MODES",
    "ARMED_MODES",
    "KIND_EMAIL",
    "KIND_UNKNOWN",
    "KIND_NONE",
    "KIND_MIXED",
    "SUBJECT_KINDS",
    "DEFAULT_ADMIN_SCOPES",
    "SECTION_ACCESS",
    "SECTION_SYSTEMS",
    "Requester",
    "requester_email",
    "requester_unknown",
    "GateConfig",
    "GateResult",
    "AccessGate",
    "AccessDeniedError",
    "normalize_mode",
    "make_guarded_call_tool",
    "wrap_fastmcp_tool_manager",
]
