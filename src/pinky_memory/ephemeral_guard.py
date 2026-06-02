"""Write-time ephemeral-entity guard for the knowledge graph (#153 phase-2).

Single source of truth for detecting PURE-IDENTIFIER "entities" — bare tracker
IDs and version stamps (``PR #635``, ``issue #501``, ``release 26.05.094``,
``26.05.070``, ``bradbrok/PinkyBot#336``) that are point-in-time events, not
durable graph nodes. The detection logic lifted here was soak-validated against
barsik's live ~392-entity graph by ``scripts/kg_ephemeral_sweep.py``: 49 true
positives, ZERO false positives.

This module is the ROOT-CAUSE half of the #153 plan: ``store.kg_add`` calls
``is_ephemeral_entity`` to reject these names at write time so they never enter
the graph. The recurring sweep (``scripts/kg_ephemeral_sweep.py``) imports the
SAME ``is_ephemeral`` from here, so the regex lives in exactly one place and the
two halves can never drift.

Design principles (hard constraints):
  * Anchored FULL-NAME match only (``re.fullmatch``). "PR #124 period
    foundation" is a DESCRIPTIVE node and MUST be kept — misses are intentional
    (miss-and-recatch by the sweep beats a false-block at write time).
  * Reversible + flag-gated: ``PINKY_KG_EPHEMERAL_GUARD`` env kill-switch,
    read per-call so a daemon restart flips it with no code change.
  * Every rejection is logged to JSONL (mirrors the sweep's regrowth log), and
    a log-write failure must NEVER break a memory write.
"""
import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Verbatim from scripts/kg_ephemeral_sweep.py — each must match the WHOLE
# normalized entity name. Deliberately tight: tracker IDs and release stamps
# with no descriptive payload.
EPHEMERAL_PATTERNS = [
    r"PR\s*#\s*\d+",                       # PR #635
    r"(?:issue|task)\s*#\s*\d+",           # issue #501, Task #70
    r"release\s+\d[\w.]*",                 # release 26.05.094, Release 26.05.089
    r"\d{2}\.\d{2}\.\d{3}",                # 26.05.070  (PinkyBot YY.MM.NNN stamp)
    r"[\w.-]+/[\w.-]+#\d+",                # bradbrok/PinkyBot#336
    r"[\w.-]+\s+(?:issue|PR)\s*#\s*\d+",   # tod-dashboard Issue #66, GitHub issue #567
    r"PinkyBot\s+#\d+(?:-\d+)?",           # PinkyBot #472-479
    r"PinkyBot\s+\d{2}\.\d{2}\.\d{3}",     # PinkyBot 26.05.093
]
_RX = re.compile(
    "(?:%s)" % "|".join(f"(?:{p})" for p in EPHEMERAL_PATTERNS),
    re.IGNORECASE,
)

# Default ON: the patterns are production-soak-validated (49 TP / 0 FP). Shipping
# off-by-default would just let the sweep keep deleting regrowth the guard could
# have stopped at the door. The env kill-switch gives instant reversibility.
EPHEMERAL_GUARD_DEFAULT = True


def _normalize(name: str) -> str:
    """Canonicalize a name the same way kg_extractor.normalize_entity_name does:
    strip + collapse internal whitespace. Inlined (not imported) to keep this
    module dependency-free and avoid a store -> kg_extractor import edge."""
    return re.sub(r"\s+", " ", (name or "").strip())


def is_ephemeral_entity(name: str) -> bool:
    """True iff the entire normalized name is a pure-ID / version-stamp ephemeral."""
    return bool(_RX.fullmatch(_normalize(name)))


# Back-compat alias so scripts/kg_ephemeral_sweep.py's import is a drop-in.
is_ephemeral = is_ephemeral_entity


def guard_enabled() -> bool:
    """Whether the write-time guard is active. Read per-call so flipping the env
    var + restarting the MCP reverts behavior with no code change."""
    v = os.environ.get("PINKY_KG_EPHEMERAL_GUARD")
    if v is None:
        return EPHEMERAL_GUARD_DEFAULT
    return v.strip().lower() not in ("0", "false", "off", "no")


def log_rejection(db_path, *, role, name, subject, predicate, obj, source):
    """Append one JSONL record per write-time rejection. Mirrors the sweep's log
    so the write-time counterpart and the regrowth log together measure
    before/after. A log-write failure degrades to a warning — it must never
    break the memory write that triggered it."""
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": "write_rejected",
        "db": db_path,
        "role": role,
        "name": name,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "source": source,
        "reason": "ephemeral_entity",
    }
    path = os.environ.get("PINKY_KG_EPHEMERAL_GUARD_LOG") or os.path.join(
        os.path.dirname(db_path) or ".", "ephemeral_guard_log.jsonl"
    )
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # pragma: no cover - defensive; log path may be unwritable
        logger.warning("ephemeral guard log write failed (%s); rejection still applied", e)
    logger.info("kg_add rejected ephemeral entity %r (role=%s, source=%s)", name, role, source)
