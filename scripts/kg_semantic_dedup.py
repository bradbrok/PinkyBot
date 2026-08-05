#!/usr/bin/env python3
"""
kg_semantic_dedup.py - Semantic deduplication of knowledge graph entities.

Uses an LLM to validate candidate merge groups identified via string similarity.
Designed for the alter-ego project's KG with ~2000 entities.

Algorithm:
1. Load all entities from SQLite
2. Group by string similarity (difflib SequenceMatcher > 0.75 or containment)
3. LLM validates each group (merge true/false + confidence)
4. Auto-merge high-confidence groups via REST API
5. Report results

Author: Engineer (PinkyBot)
"""

import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


# ============================================================================
# Configuration
# ============================================================================

DB_PATH = Path("/home/pinky/projects/alter-ego/data/memories.db")
MERGE_API_URL = "http://localhost:7778/admin/merge-entities"
BROKER_URL = "http://localhost:8888"
REPORT_PATH = Path("/home/pinky/.pinkybot/logs/kg_dedup_report.json")

# Similarity thresholds
SIMILARITY_THRESHOLD = 0.85  # Raised from 0.75 to reduce false positives

# Safety limits — CRITICAL: prevent catastrophic merges
MAX_GROUP_SIZE = 8            # Skip groups larger than this (almost certainly spurious)
MIN_CONTAINMENT_LENGTH = 7   # Min chars for containment check (was 3 — too loose!)
PROTECTED_TYPES = {"PERSON"}  # Never auto-merge these entity types

# LLM settings
LLM_RATE_LIMIT_SECONDS = 0.5
LLM_TIMEOUT_SECONDS = 60
AUTO_MERGE_CONFIDENCE = 0.95  # Raised from 0.85 — be very conservative


# ============================================================================
# Data structures
# ============================================================================

@dataclass
class Entity:
    id: int
    name: str
    type: str
    description: str
    salience: float


@dataclass
class CandidateGroup:
    entities: list[Entity]
    similarity_reason: str = ""


@dataclass
class MergeDecision:
    merge: bool
    keep_id: int
    reason: str
    confidence: float


@dataclass
class Report:
    total_candidates: int = 0
    auto_merged: list = field(default_factory=list)
    needs_review: list = field(default_factory=list)
    skipped: int = 0
    errors: list = field(default_factory=list)


# ============================================================================
# Database operations
# ============================================================================

def load_entities(db_path: Path) -> list[Entity]:
    """Load all entities from kg_entities table."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, name, type, COALESCE(description, '') as description,
               COALESCE(salience, 0.5) as salience
        FROM kg_entities
        ORDER BY id
    """)

    entities = []
    for row in cursor.fetchall():
        entities.append(Entity(
            id=row["id"],
            name=row["name"],
            type=row["type"],
            description=row["description"],
            salience=row["salience"]
        ))

    conn.close()
    print(f"[INFO] Loaded {len(entities)} entities from database")
    return entities


# ============================================================================
# Normalization and similarity
# ============================================================================

def normalize_name(name: str) -> str:
    """
    Normalize entity name for comparison.
    Removes common Italian articles and prefixes.
    """
    s = name.lower().strip()

    # Remove leading articles
    prefixes = [
        "la ", "il ", "lo ", "gli ", "le ", "l'", "un ", "una ", "uno ",
        "agente per ", "agente ", "essere ", "il concetto di ", "concetto di "
    ]
    for prefix in prefixes:
        if s.startswith(prefix):
            s = s[len(prefix):]
            break  # Only remove one prefix

    return s.strip()


def are_similar(name1: str, name2: str) -> tuple[bool, str]:
    """
    Check if two names are similar enough to be merge candidates.
    Returns (is_similar, reason).
    """
    norm1 = normalize_name(name1)
    norm2 = normalize_name(name2)

    # Exact match after normalization
    if norm1 == norm2:
        return True, "exact_after_norm"

    # One contains the other (after normalization)
    # Use MIN_CONTAINMENT_LENGTH to avoid short substrings matching everywhere
    if len(norm1) >= MIN_CONTAINMENT_LENGTH and len(norm2) >= MIN_CONTAINMENT_LENGTH:
        if norm1 in norm2 or norm2 in norm1:
            return True, "containment"

    # SequenceMatcher ratio
    ratio = SequenceMatcher(None, norm1, norm2).ratio()
    if ratio >= SIMILARITY_THRESHOLD:
        return True, f"sequence_match_{ratio:.2f}"

    return False, ""


def build_candidate_groups(entities: list[Entity]) -> list[CandidateGroup]:
    """
    Group entities by similarity using efficient clustering.
    Returns groups with 2+ members.
    """
    # Build similarity graph
    n = len(entities)
    similar_pairs = []

    print(f"[INFO] Computing similarity matrix for {n} entities...")

    for i in range(n):
        for j in range(i + 1, n):
            is_sim, reason = are_similar(entities[i].name, entities[j].name)
            if is_sim:
                similar_pairs.append((i, j, reason))

    print(f"[INFO] Found {len(similar_pairs)} similar pairs")

    # Union-Find clustering
    parent = list(range(n))

    def find(x):
        if parent[x] != x:
            parent[x] = find(parent[x])
        return parent[x]

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for i, j, _ in similar_pairs:
        union(i, j)

    # Group by root
    groups_map = defaultdict(list)
    for i in range(n):
        groups_map[find(i)].append(i)

    # Build candidate groups (2+ members, up to MAX_GROUP_SIZE)
    groups = []
    skipped_large = 0
    for indices in groups_map.values():
        if len(indices) >= 2:
            if len(indices) > MAX_GROUP_SIZE:
                skipped_large += 1
                names_preview = [entities[i].name for i in indices[:5]]
                print(f"[WARN] Skipping oversized group ({len(indices)} entities): {names_preview}... "
                      f"(likely false-positive cluster)")
                continue
            group_entities = [entities[i] for i in indices]
            # Determine dominant similarity reason
            reasons = []
            for i in range(len(indices)):
                for j in range(i + 1, len(indices)):
                    _, reason = are_similar(group_entities[i].name, group_entities[j].name)
                    if reason:
                        reasons.append(reason)
            dominant_reason = max(set(reasons), key=reasons.count) if reasons else "mixed"
            groups.append(CandidateGroup(entities=group_entities, similarity_reason=dominant_reason))

    print(f"[INFO] Built {len(groups)} candidate groups")
    if skipped_large > 0:
        print(f"[WARN] Skipped {skipped_large} oversized groups (>{MAX_GROUP_SIZE} entities) — potential false-positive clusters")
    return groups


def filter_period_groups(groups: list[CandidateGroup]) -> list[CandidateGroup]:
    """
    Remove groups where ALL members are PERIOD type.
    Dates/periods should not be merged.
    """
    filtered = []
    skipped = 0

    for group in groups:
        types = set(e.type for e in group.entities)
        if types == {"PERIOD"}:
            skipped += 1
            continue
        filtered.append(group)

    if skipped > 0:
        print(f"[INFO] Skipped {skipped} all-PERIOD groups")

    return filtered


# ============================================================================
# LLM Session management
# ============================================================================

class LLMSession:
    """Manages a PinkyBot LLM session for entity merge validation."""

    def __init__(self, broker_url: str):
        self.broker_url = broker_url
        self.session_id: Optional[str] = None

    def create(self) -> bool:
        """Create a new LLM session."""
        try:
            payload = {
                "name": f"kg_dedup_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "agent": "testimone",
                "model": "haiku"
            }
            req = urllib.request.Request(
                f"{self.broker_url}/sessions",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                self.session_id = data.get("session_id") or data.get("id")
                print(f"[INFO] Created LLM session: {self.session_id}")
                return True
        except Exception as e:
            print(f"[ERROR] Failed to create LLM session: {e}")
            return False

    def send_message(self, content: str) -> Optional[str]:
        """Send a message and get response."""
        if not self.session_id:
            return None

        try:
            payload = {"content": content}
            req = urllib.request.Request(
                f"{self.broker_url}/sessions/{self.session_id}/message",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode())
                # Handle different response formats
                if isinstance(data, dict):
                    return data.get("response") or data.get("content") or data.get("message")
                return str(data)
        except urllib.error.URLError as e:
            print(f"[ERROR] LLM request failed: {e}")
            return None
        except Exception as e:
            print(f"[ERROR] LLM error: {e}")
            return None

    def delete(self) -> bool:
        """Delete the session."""
        if not self.session_id:
            return True

        try:
            req = urllib.request.Request(
                f"{self.broker_url}/sessions/{self.session_id}",
                method="DELETE"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[INFO] Deleted LLM session: {self.session_id}")
                return True
        except Exception as e:
            print(f"[WARN] Failed to delete session: {e}")
            return False


# ============================================================================
# LLM validation
# ============================================================================

def build_llm_prompt(group: CandidateGroup) -> str:
    """Build the prompt for LLM merge validation."""
    entities_text = "\n".join([
        f"  {e.id} | {e.name} | {e.type} | {e.description[:100]}{'...' if len(e.description) > 100 else ''}"
        for e in group.entities
    ])

    prompt = f"""Sei un assistente che gestisce un knowledge graph del profilo di Mirko (persona reale italiana, ~50 anni).
Valuta se queste entita del grafo rappresentano la STESSA cosa e devono essere unificate.

Entita candidate per merge:
{entities_text}

Rispondi SOLO con JSON valido (nessun altro testo):
{{"merge": true, "keep_id": 123, "reason": "perche si o no", "confidence": 0.95}}

Regole:
- merge=true SOLO se sono chiaramente la stessa cosa
- Se sono concetti diversi (es. "Bitcoin come valuta" vs "Bitcoin Foundation") -> merge=false
- Se sono periodi di tempo diversi -> merge=false
- keep_id = l'entita con descrizione piu ricca o salience piu alta
- confidence > 0.85 per auto-merge, altrimenti segnala per revisione"""

    return prompt


def parse_llm_response(response: str, group: CandidateGroup) -> Optional[MergeDecision]:
    """Parse LLM JSON response into MergeDecision."""
    if not response:
        return None

    # Try to extract JSON from response
    try:
        # Handle potential markdown code blocks
        cleaned = response.strip()
        if "```json" in cleaned:
            cleaned = cleaned.split("```json")[1].split("```")[0]
        elif "```" in cleaned:
            cleaned = cleaned.split("```")[1].split("```")[0]

        # Find JSON object
        match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
        if match:
            data = json.loads(match.group())
        else:
            data = json.loads(cleaned)

        # Validate required fields
        merge = data.get("merge", False)
        keep_id = data.get("keep_id")
        reason = data.get("reason", "")
        confidence = data.get("confidence", 0.0)

        # Validate keep_id is in group
        valid_ids = {e.id for e in group.entities}
        if keep_id not in valid_ids:
            # Default to entity with highest salience or longest description
            best = max(group.entities, key=lambda e: (e.salience, len(e.description)))
            keep_id = best.id

        return MergeDecision(
            merge=bool(merge),
            keep_id=int(keep_id),
            reason=str(reason),
            confidence=float(confidence)
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[WARN] Failed to parse LLM response: {e}")
        print(f"       Response was: {response[:200]}")
        return None


def validate_group_with_llm(session: LLMSession, group: CandidateGroup) -> Optional[MergeDecision]:
    """Ask LLM to validate a candidate group for merge."""
    prompt = build_llm_prompt(group)
    response = session.send_message(prompt)

    if not response:
        return None

    return parse_llm_response(response, group)


# ============================================================================
# Merge execution
# ============================================================================

def execute_merge(keep_id: int, delete_ids: list[int], retry: bool = True) -> bool:
    """
    Execute merge via REST API.
    Reroutes all relations from deleted entities to kept one, then deletes.
    """
    try:
        payload = {
            "keep_id": keep_id,
            "delete_ids": delete_ids
        }
        req = urllib.request.Request(
            MERGE_API_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200

    except urllib.error.URLError as e:
        if "locked" in str(e).lower() and retry:
            print(f"[WARN] DB locked, retrying in 1s...")
            time.sleep(1)
            return execute_merge(keep_id, delete_ids, retry=False)
        print(f"[ERROR] Merge API failed: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Merge failed: {e}")
        return False


# ============================================================================
# Main pipeline
# ============================================================================

def run_dedup() -> Report:
    """Main deduplication pipeline."""
    report = Report()

    # Step 1: Load entities
    print("\n=== Step 1: Loading entities ===")
    entities = load_entities(DB_PATH)

    # Step 2: Build candidate groups
    print("\n=== Step 2: Building candidate groups ===")
    groups = build_candidate_groups(entities)
    groups = filter_period_groups(groups)
    report.total_candidates = len(groups)

    if not groups:
        print("[INFO] No candidate groups found")
        return report

    # Step 3: LLM validation
    print(f"\n=== Step 3: LLM validation of {len(groups)} groups ===")
    session = LLMSession(BROKER_URL)
    if not session.create():
        print("[ERROR] Cannot proceed without LLM session")
        report.errors.append("Failed to create LLM session")
        return report

    try:
        for i, group in enumerate(groups):
            print(f"[{i+1}/{len(groups)}] Validating: {[e.name for e in group.entities][:3]}...")

            decision = validate_group_with_llm(session, group)

            if decision is None:
                print("         -> LLM timeout/error, skipping")
                report.skipped += 1
                report.errors.append(f"LLM failed for group: {[e.name for e in group.entities]}")
                time.sleep(LLM_RATE_LIMIT_SECONDS)
                continue

            if not decision.merge:
                print(f"         -> No merge ({decision.reason})")
                report.skipped += 1
                time.sleep(LLM_RATE_LIMIT_SECONDS)
                continue

            # Merge decision
            keep_entity = next(e for e in group.entities if e.id == decision.keep_id)
            delete_entities = [e for e in group.entities if e.id != decision.keep_id]
            delete_ids = [e.id for e in delete_entities]

            # SAFETY: Never auto-merge PERSON entities — too risky
            has_person = any(e.type in PROTECTED_TYPES for e in group.entities)
            if has_person:
                print(f"         -> Skipped (group contains PERSON — manual review required)")
                report.skipped += 1
                report.needs_review.append({
                    "reason": "contains_person",
                    "entities": [{"id": e.id, "name": e.name, "type": e.type} for e in group.entities]
                })
                time.sleep(LLM_RATE_LIMIT_SECONDS)
                continue

            if decision.confidence >= AUTO_MERGE_CONFIDENCE:
                # Step 4: Execute auto-merge
                print(f"         -> Auto-merge (conf={decision.confidence:.2f}): "
                      f"keep {keep_entity.name}, delete {[e.name for e in delete_entities]}")

                if execute_merge(decision.keep_id, delete_ids):
                    report.auto_merged.append({
                        "kept_name": keep_entity.name,
                        "kept_id": keep_entity.id,
                        "deleted_names": [e.name for e in delete_entities],
                        "deleted_ids": delete_ids,
                        "reason": decision.reason,
                        "confidence": decision.confidence
                    })
                else:
                    report.errors.append(f"Merge failed: keep={keep_entity.name}, "
                                        f"delete={[e.name for e in delete_entities]}")
            else:
                # Low confidence - needs review
                print(f"         -> Needs review (conf={decision.confidence:.2f})")
                report.needs_review.append({
                    "entities": [{"id": e.id, "name": e.name, "type": e.type}
                                for e in group.entities],
                    "suggested_keep": keep_entity.name,
                    "suggested_keep_id": keep_entity.id,
                    "reason": decision.reason,
                    "confidence": decision.confidence
                })

            time.sleep(LLM_RATE_LIMIT_SECONDS)

    finally:
        session.delete()

    return report


def save_report(report: Report) -> None:
    """Save report to JSON file."""
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    report_dict = {
        "timestamp": datetime.now().isoformat(),
        "total_candidates": report.total_candidates,
        "auto_merged": report.auto_merged,
        "auto_merged_count": len(report.auto_merged),
        "needs_review": report.needs_review,
        "needs_review_count": len(report.needs_review),
        "skipped": report.skipped,
        "errors": report.errors
    }

    with open(REPORT_PATH, "w") as f:
        json.dump(report_dict, f, ensure_ascii=False, indent=2)

    print(f"\n[INFO] Report saved to {REPORT_PATH}")


def print_summary(report: Report) -> None:
    """Print human-readable summary."""
    print("\n" + "=" * 60)
    print("KG SEMANTIC DEDUP - SUMMARY")
    print("=" * 60)
    print(f"Total candidate groups:  {report.total_candidates}")
    print(f"Auto-merged:             {len(report.auto_merged)}")
    print(f"Needs review:            {len(report.needs_review)}")
    print(f"Skipped (no merge):      {report.skipped}")
    print(f"Errors:                  {len(report.errors)}")
    print("=" * 60)

    if report.auto_merged:
        print("\nAuto-merged entities:")
        for merge in report.auto_merged[:10]:  # Show first 10
            print(f"  - Kept: {merge['kept_name']}")
            print(f"    Deleted: {', '.join(merge['deleted_names'])}")
            print(f"    Reason: {merge['reason']}")
        if len(report.auto_merged) > 10:
            print(f"  ... and {len(report.auto_merged) - 10} more")

    if report.needs_review:
        print("\nNeeds manual review:")
        for item in report.needs_review[:5]:  # Show first 5
            entities_str = ", ".join(e["name"] for e in item["entities"])
            print(f"  - {entities_str}")
            print(f"    Suggested: keep '{item['suggested_keep']}' (conf={item['confidence']:.2f})")
        if len(report.needs_review) > 5:
            print(f"  ... and {len(report.needs_review) - 5} more")

    if report.errors:
        print("\nErrors:")
        for err in report.errors[:5]:
            print(f"  - {err}")
        if len(report.errors) > 5:
            print(f"  ... and {len(report.errors) - 5} more")


# ============================================================================
# Entry point
# ============================================================================

def main():
    print("KG Semantic Deduplication")
    print(f"Database: {DB_PATH}")
    print(f"Merge API: {MERGE_API_URL}")
    print(f"Auto-merge threshold: {AUTO_MERGE_CONFIDENCE}")
    print()

    if not DB_PATH.exists():
        print(f"[ERROR] Database not found: {DB_PATH}")
        sys.exit(1)

    report = run_dedup()
    save_report(report)
    print_summary(report)

    # Exit code based on errors
    if report.errors:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
