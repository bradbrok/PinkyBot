"""Pure reasoning helpers for proactive KG surfacing (#682, PR2).

Given the raw, read-only outputs of ``ReflectionStore.kg_find_contradictions()``
and ``ReflectionStore.kg_what_changed()``, decide which insights are MATERIAL
(worth a human's attention), suppress repeats by a stable fingerprint, cap the
volume, and format a short advisory digest.

Everything here is a PURE function: no DB, no I/O, no globals, no clock. That
keeps the materiality policy unit-testable in isolation and lets the dream
runner own all the side effects (querying the store, persisting fingerprints,
delivering the digest).

Materiality policy (designed with Murzik):
- A functional CONTRADICTION (one subject+predicate with >1 live value) is
  material if its predicate is in a high-value domain (location, timezone,
  employment, ownership, deploy target) OR it touches a high-value entity.
- A CHANGE that ENDED/superseded is material if it is a high-value domain or
  touches a high-value entity (a fact ending is inherently notable).
- A CHANGE that was ADDED (still live) is material only if it touches a
  high-value entity AND its confidence is >= 0.7 (ignore low-confidence noise).
- Surfacing is advisory only — no auto-fix is ever applied to the graph.
"""

from __future__ import annotations

# Functional predicates whose contradiction/change is ALWAYS material — the
# domains where a stale value would actively mislead the agent. A subset of
# kg_extractor.FUNCTIONAL_PREDICATES plus ownership variants. The softer
# functional predicates (primary_language/editor/model, married_to, dating)
# are material only when they touch a high-value entity.
MATERIAL_PREDICATES = frozenset({
    "lives_in", "located_in", "timezone",
    "works_at", "employed_by", "current_role", "current_title",
    "reports_to", "managed_by", "owns", "owned_by",
    "runs_on", "hosted_on", "deployed_to",
})

# Additions below this confidence are treated as noise unless they touch a
# high-value entity at or above it.
LOW_CONF = 0.7

# Default cap on insights surfaced in a single digest.
DEFAULT_CAP = 5


def _norm(s: object) -> str:
    """Case-folded, stripped string for stable comparison/fingerprinting."""
    return str(s or "").strip().casefold()


# ── Fingerprints (repeat-suppression keys) ──────────────────────────────────

def contradiction_fingerprint(c: dict) -> str:
    """Stable key for a contradiction: subject|predicate|sorted-live-values.

    Independent of value ordering and of the advisory winner, so the same
    standing contradiction is suppressed across nights until it actually
    changes (a value is added/removed/resolved).
    """
    subj = _norm(c.get("subject"))
    pred = _norm(c.get("predicate"))
    vals = "|".join(sorted(_norm(v) for v in c.get("active_values", [])))
    return f"contradiction:{subj}|{pred}|{vals}"


def change_fingerprint(change: dict, bucket: str) -> str:
    """Stable key for a change: bucket + triple id.

    A given triple can legitimately surface once as ``added`` and later once as
    ``ended``; the bucket keeps those distinct while preventing the same
    triple/bucket from re-surfacing every night.
    """
    return f"change:{bucket}:{change.get('id')}"


# ── Materiality ─────────────────────────────────────────────────────────────

def is_material_contradiction(c: dict, high_value: set[str]) -> bool:
    pred = _norm(c.get("predicate"))
    if pred in MATERIAL_PREDICATES:
        return True
    ents = {_norm(c.get("subject"))}
    ents |= {_norm(v) for v in c.get("active_values", [])}
    return bool(ents & high_value)


def is_material_change(change: dict, bucket: str, high_value: set[str]) -> bool:
    pred = _norm(change.get("predicate"))
    touches_hv = (
        _norm(change.get("subject")) in high_value
        or _norm(change.get("object")) in high_value
    )
    if bucket == "ended":
        return pred in MATERIAL_PREDICATES or touches_hv
    # bucket == "added": still-live fact. Ignore low-confidence noise; only
    # surface when it touches a high-value entity.
    try:
        conf = float(change.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < LOW_CONF:
        return False
    return touches_hv


# ── Selection (filter + dedup + cap) ────────────────────────────────────────

def select_insights(
    contradictions: list[dict] | None,
    changes: dict | None,
    seen_fingerprints,
    high_value,
    cap: int = DEFAULT_CAP,
) -> dict:
    """Pick material, not-yet-seen insights up to ``cap`` total.

    Contradictions are considered first (highest signal), then ENDED changes,
    then ADDED changes. Returns a dict with the selected contradictions, the
    selected changes (each tagged with ``_bucket``), and the list of NEW
    fingerprints to persist for repeat-suppression.
    """
    seen = set(seen_fingerprints or ())
    hv = {_norm(e) for e in (high_value or ()) if _norm(e)}
    cap = max(0, int(cap))

    sel_contra: list[dict] = []
    sel_changes: list[dict] = []
    new_fps: list[str] = []

    def _full() -> bool:
        return (len(sel_contra) + len(sel_changes)) >= cap

    for c in contradictions or []:
        if _full():
            break
        fp = contradiction_fingerprint(c)
        if fp in seen or fp in new_fps:
            continue
        if not is_material_contradiction(c, hv):
            continue
        sel_contra.append(c)
        new_fps.append(fp)

    changes = changes or {}
    for bucket in ("ended", "added"):
        for ch in changes.get(bucket, []) or []:
            if _full():
                break
            fp = change_fingerprint(ch, bucket)
            if fp in seen or fp in new_fps:
                continue
            if not is_material_change(ch, bucket, hv):
                continue
            sel_changes.append({**ch, "_bucket": bucket})
            new_fps.append(fp)
        if _full():
            break

    return {
        "contradictions": sel_contra,
        "changes": sel_changes,
        "fingerprints": new_fps,
    }


# ── Formatting ──────────────────────────────────────────────────────────────

def format_digest(selected: dict, *, min_items: int = 1) -> str:
    """Render the selected insights as a short advisory digest.

    Returns "" when there is nothing material (fewer than ``min_items``),
    which the caller treats as "surface nothing this cycle".
    """
    contra = selected.get("contradictions", []) if selected else []
    changes = selected.get("changes", []) if selected else []
    if (len(contra) + len(changes)) < max(1, min_items):
        return ""

    lines = [
        "🧠 KG insights from last night's consolidation "
        "(read-only — no auto-fix applied):",
    ]

    if contra:
        lines.append("")
        lines.append(f"Live contradictions ({len(contra)}):")
        for c in contra:
            vals = " vs ".join(str(v) for v in c.get("active_values", []))
            winner = (c.get("suggested_winner") or {}).get("object", "")
            line = f"  • {c.get('subject')} — {c.get('predicate')}: {vals}"
            if winner:
                line += f"  (advisory winner: {winner})"
            lines.append(line)

    if changes:
        lines.append("")
        lines.append(f"Recent changes ({len(changes)}):")
        for ch in changes:
            verb = "ended" if ch.get("_bucket") == "ended" else "added"
            obj = ch.get("object", "")
            lines.append(
                f"  • [{verb}] {ch.get('subject')} {ch.get('predicate')} {obj}".rstrip()
            )

    lines.append("")
    lines.append(
        "If any of these matter to Brad or an active project, surface it; "
        "otherwise note and move on."
    )
    return "\n".join(lines)
