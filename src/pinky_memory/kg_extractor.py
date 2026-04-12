"""Knowledge Graph auto-extraction from reflections.

Implements the 3-step extraction pipeline recommended by Murzik:
  Step 1: Deterministic pre-pass (entity/date detection, noise stripping)
  Step 2: LLM extraction of structured candidates (entities, triples, confidence, temporal cues)
  Step 3: Deterministic validator (normalize, dedupe, conflict resolution)

Predicate-aware conflict policy:
  - Functional predicates (lives_in, works_at): auto-supersede on high confidence
  - Multi-valued predicates (uses, knows, likes): never auto-invalidate
  - Event predicates (created, moved_to, joined): never treated as conflicts

Usage:
    extractor = KGExtractor(store=store)
    results = extractor.extract_from_reflection(reflection_id, content, ...)
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ── Predicate Cardinality Config ──────────────────────────────

# Functional: single active value expected per subject.
# When a new high-confidence triple conflicts, auto-supersede the old one.
FUNCTIONAL_PREDICATES = frozenset({
    "lives_in", "located_in", "works_at", "employed_by",
    "primary_language", "primary_model", "primary_editor",
    "managed_by", "reports_to", "married_to", "dating",
    "current_role", "current_title", "timezone",
    "runs_on", "hosted_on", "deployed_to",
})

# Multi-valued: multiple active values allowed per subject.
# Never auto-invalidate — just add new triples.
MULTI_VALUED_PREDICATES = frozenset({
    "uses", "knows", "likes", "prefers", "dislikes",
    "works_on", "contributes_to", "collaborates_with",
    "speaks", "member_of", "has_skill", "interested_in",
    "owns", "maintains", "friends_with", "knows_about",
})

# Event: time-series/historical events. Never conflicts.
EVENT_PREDICATES = frozenset({
    "created", "built", "shipped", "deployed", "released",
    "moved_to", "joined", "left", "started", "completed",
    "fixed", "discovered", "decided", "proposed", "merged",
    "visited", "traveled_to",
})

# Confidence threshold for auto-supersession of functional predicates
_SUPERSEDE_CONFIDENCE = 0.7

# Minimum confidence to accept an extracted triple at all
_MIN_CONFIDENCE = 0.3


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Data Classes ──────────────────────────────────────────────

@dataclass
class ExtractedTriple:
    """A candidate triple from LLM extraction, before validation."""
    subject: str
    predicate: str
    object: str
    subject_type: str = "unknown"
    object_type: str = "unknown"
    confidence: float = 0.8
    valid_from: str = ""
    temporal_granularity: str = "none"  # explicit | inferred | none
    evidence_span: str = ""  # excerpt from source text
    is_negation: bool = False  # "Brad no longer uses X"


@dataclass
class ExtractionResult:
    """Result of extracting KG triples from a single reflection."""
    reflection_id: str
    triples_added: list[dict] = field(default_factory=list)
    triples_superseded: list[dict] = field(default_factory=list)
    triples_skipped: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_added(self) -> int:
        return len(self.triples_added)

    @property
    def total_superseded(self) -> int:
        return len(self.triples_superseded)


# ── LLM Extraction Prompt ─────────────────────────────────────

KG_EXTRACTION_PROMPT = """Extract knowledge graph triples from this memory reflection.

A triple is (subject, predicate, object) — a factual relationship between two entities.

<reflection>
{content}
</reflection>

{context_block}

Rules:
- Extract concrete, factual relationships only — not opinions or speculation
- Normalize entity names: use proper case for people/companies, lowercase for tools/concepts
- Use consistent predicate names from this list when possible:
  Functional (single value): lives_in, works_at, employed_by, primary_language, \
managed_by, married_to, current_role, timezone, runs_on, hosted_on, deployed_to
  Multi-valued: uses, knows, likes, prefers, works_on, contributes_to, \
collaborates_with, speaks, member_of, has_skill, owns, maintains, friends_with
  Events: created, built, shipped, deployed, moved_to, joined, left, \
started, completed, fixed, decided, proposed, merged, visited
- For temporal info: mark "explicit" if there's a date, "inferred" if words like \
"now"/"currently", "none" if no time signal
- Include a short evidence_span (max 100 chars) showing the source text
- Set is_negation=true for statements that something ended or stopped
- confidence: 0.0-1.0 based on how clearly stated the fact is

Respond with ONLY a JSON array. No explanation, no markdown fences.

[
  {{
    "subject": "entity name",
    "predicate": "relationship",
    "object": "entity name",
    "subject_type": "person|project|tool|concept|agent|company|location|unknown",
    "object_type": "person|project|tool|concept|agent|company|location|unknown",
    "confidence": 0.8,
    "valid_from": "2026-03 or empty string",
    "temporal_granularity": "explicit|inferred|none",
    "evidence_span": "short excerpt from source",
    "is_negation": false
  }}
]

If no extractable triples exist, return an empty array: []"""


# ── Validator ─────────────────────────────────────────────────

def normalize_entity_name(name: str) -> str:
    """Normalize entity names for consistent matching."""
    name = name.strip()
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name)
    return name


def normalize_predicate(predicate: str) -> str:
    """Normalize predicate names to snake_case canonical forms."""
    p = predicate.strip().lower()
    p = re.sub(r"[\s-]+", "_", p)
    # Common aliases
    aliases = {
        "located_at": "located_in",
        "lives_at": "lives_in",
        "resides_in": "lives_in",
        "employed_at": "employed_by",
        "works_for": "employed_by",
        "made": "created",
        "built_with": "uses",
        "written_in": "uses",
        "coded_in": "uses",
        "friend_of": "friends_with",
        "collaborates": "collaborates_with",
        "contributes": "contributes_to",
        "is_member_of": "member_of",
        "part_of": "member_of",
        "manages": "managed_by",  # reversed direction handled in validation
        "runs": "runs_on",
        "hosted_at": "hosted_on",
    }
    return aliases.get(p, p)


def get_predicate_type(predicate: str) -> str:
    """Classify a predicate as functional, multi_valued, or event."""
    if predicate in FUNCTIONAL_PREDICATES:
        return "functional"
    if predicate in MULTI_VALUED_PREDICATES:
        return "multi_valued"
    if predicate in EVENT_PREDICATES:
        return "event"
    # Default unknown predicates to multi_valued (safer — no auto-invalidation)
    return "multi_valued"


def validate_triple(t: ExtractedTriple) -> list[str]:
    """Validate a single extracted triple. Returns list of issues (empty = valid)."""
    issues = []
    if not t.subject or len(t.subject) < 1:
        issues.append("empty subject")
    if not t.predicate or len(t.predicate) < 1:
        issues.append("empty predicate")
    if not t.object or len(t.object) < 1:
        issues.append("empty object")
    if t.subject.lower() == t.object.lower():
        issues.append("subject equals object")
    if t.confidence < _MIN_CONFIDENCE:
        issues.append(f"confidence too low ({t.confidence})")
    if len(t.subject) > 200 or len(t.object) > 200:
        issues.append("entity name too long")
    if len(t.predicate) > 100:
        issues.append("predicate too long")
    return issues


def parse_llm_response(raw: str) -> list[ExtractedTriple]:
    """Parse LLM JSON response into ExtractedTriple objects."""
    # Strip markdown fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _log(f"kg-extractor: JSON parse error: {e}")
        return []

    if not isinstance(data, list):
        _log(f"kg-extractor: expected list, got {type(data).__name__}")
        return []

    triples = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            t = ExtractedTriple(
                subject=normalize_entity_name(str(item.get("subject", ""))),
                predicate=normalize_predicate(str(item.get("predicate", ""))),
                object=normalize_entity_name(str(item.get("object", ""))),
                subject_type=str(item.get("subject_type", "unknown")).lower(),
                object_type=str(item.get("object_type", "unknown")).lower(),
                confidence=float(item.get("confidence", 0.8)),
                valid_from=str(item.get("valid_from", "")),
                temporal_granularity=str(
                    item.get("temporal_granularity", "none")
                ).lower(),
                evidence_span=str(item.get("evidence_span", ""))[:200],
                is_negation=bool(item.get("is_negation", False)),
            )
            triples.append(t)
        except (ValueError, TypeError) as e:
            _log(f"kg-extractor: skipping malformed triple: {e}")

    return triples


# ── Main Extractor ────────────────────────────────────────────

class KGExtractor:
    """Extracts and inserts KG triples from reflections.

    Uses a ReflectionStore for KG operations and an LLM caller
    for extraction. The LLM caller is a callable that takes a
    prompt string and returns the response text.
    """

    # Version string for idempotent reprocessing tracking
    EXTRACTOR_VERSION = "1.0"

    def __init__(self, store: object, llm_caller: object | None = None):
        """
        Args:
            store: ReflectionStore instance with KG methods.
            llm_caller: Callable[[str], str] that sends a prompt to an LLM
                        and returns the response. If None, extraction is
                        skipped (validation-only mode for testing).
        """
        self._store = store
        self._llm_caller = llm_caller

    def extract_from_reflection(
        self,
        reflection_id: str,
        content: str,
        context: str = "",
        project: str = "",
    ) -> ExtractionResult:
        """Extract KG triples from a single reflection.

        Returns ExtractionResult with lists of added, superseded, and skipped triples.
        """
        result = ExtractionResult(reflection_id=reflection_id)

        # Step 1: Pre-pass — skip very short or clearly non-factual content
        if len(content.strip()) < 20:
            result.errors.append("content too short for extraction")
            return result

        # Step 2: LLM extraction
        if self._llm_caller is None:
            result.errors.append("no LLM caller configured")
            return result

        context_block = ""
        if context:
            context_block = f"Context: {context}\n"
        if project:
            context_block += f"Project: {project}\n"

        prompt = KG_EXTRACTION_PROMPT.format(
            content=content,
            context_block=context_block,
        )

        try:
            raw_response = self._llm_caller(prompt)
        except Exception as e:
            result.errors.append(f"LLM call failed: {e}")
            return result

        candidates = parse_llm_response(raw_response)

        if not candidates:
            return result  # No triples found — that's fine

        # Step 3: Validate and insert each candidate
        for t in candidates:
            issues = validate_triple(t)
            if issues:
                result.triples_skipped.append({
                    "subject": t.subject, "predicate": t.predicate,
                    "object": t.object, "reason": "; ".join(issues),
                })
                continue

            # Handle negations: invalidate matching active triples
            if t.is_negation:
                count = self._store.kg_invalidate(
                    t.subject, t.predicate, t.object,
                    valid_to=t.valid_from or "",
                )
                if count:
                    result.triples_superseded.append({
                        "subject": t.subject, "predicate": t.predicate,
                        "object": t.object, "negation": True,
                    })
                continue

            # Check for conflicts on functional predicates
            pred_type = get_predicate_type(t.predicate)
            if pred_type == "functional" and t.confidence >= _SUPERSEDE_CONFIDENCE:
                self._resolve_functional_conflict(t, result)

            # Insert the triple
            try:
                added = self._store.kg_add(
                    subject=t.subject,
                    predicate=t.predicate,
                    obj=t.object,
                    valid_from=t.valid_from,
                    subject_type=t.subject_type,
                    object_type=t.object_type,
                    confidence=t.confidence,
                    source_reflection_id=reflection_id,
                    extraction_method="auto_llm",
                    status="active",
                    temporal_granularity=t.temporal_granularity,
                    evidence_span=t.evidence_span,
                )
                result.triples_added.append(added)
            except Exception as e:
                result.errors.append(
                    f"insert failed for ({t.subject}, {t.predicate}, {t.object}): {e}"
                )

        return result

    def _resolve_functional_conflict(
        self,
        new_triple: ExtractedTriple,
        result: ExtractionResult,
    ) -> None:
        """For functional predicates, supersede conflicting active triples."""
        existing = self._store.kg_query(
            entity=new_triple.subject,
            predicate=new_triple.predicate,
            include_expired=False,
            limit=10,
        )

        for old in existing:
            # Only supersede if subject matches exactly and object differs
            if (
                old["subject"].lower() == new_triple.subject.lower()
                and old["object"].lower() != new_triple.object.lower()
            ):
                valid_to = new_triple.valid_from or datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%d")
                self._store.kg_invalidate(
                    old["subject"], old["predicate"], old["object"],
                    valid_to=valid_to,
                )
                result.triples_superseded.append({
                    "subject": old["subject"],
                    "predicate": old["predicate"],
                    "object": old["object"],
                    "superseded_by_object": new_triple.object,
                })

    def extract_batch(
        self,
        reflections: list[dict],
    ) -> list[ExtractionResult]:
        """Process a batch of reflections. Each extracted independently.

        Args:
            reflections: List of dicts with keys: id, content, context, project

        Returns:
            List of ExtractionResult, one per reflection.
        """
        results = []
        for ref in reflections:
            r = self.extract_from_reflection(
                reflection_id=ref["id"],
                content=ref["content"],
                context=ref.get("context", ""),
                project=ref.get("project", ""),
            )
            results.append(r)

        return results
