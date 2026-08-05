#!/usr/bin/env python3
"""
AIena Flags-to-KG — Mini bridge veloce.

Legge le anomaly_flags già salvate nel KG (con data_json completo),
crea entità buyer/supplier nel KG, crea relazioni VINCE, aggiorna entity_id.

Idempotente: processa solo flags WHERE entity_id IS NULL.
Exact match cache: no Levenshtein, veloce su qualsiasi numero di entità.

Uso:
    python -u aiena_flags_to_kg.py           # Esegue (unbuffered output)
    python -u aiena_flags_to_kg.py --dry-run # Solo preview
    python -u aiena_flags_to_kg.py --min-score 0.4
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")

RELATION_TYPE_MAP = {
    "VINCITORE_RICORRENTE": "VINCE",
    "RIBASSO_ANOMALO": "VINCE",
    "ZERO_CONCORRENZA": "VINCE",
    "VARIANTE_ECCESSIVA": "VINCE",
    "CONCENTRAZIONE_GEOGRAFICA": "VINCE",
}


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def norm(name: str) -> str:
    return " ".join(name.upper().split())


def run(min_score: float = 0.0, dry_run: bool = False, limit: int = 0):
    now = datetime.now(timezone.utc).isoformat()

    conn = sqlite3.connect(str(KG_DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row

    # Carica flags senza entity_id
    q = """
        SELECT id, anomaly_type, anomaly_score, description, data_json
        FROM anomaly_flags
        WHERE entity_id IS NULL AND data_json IS NOT NULL AND data_json != ''
        AND anomaly_score >= ?
        ORDER BY anomaly_score DESC
    """
    if limit:
        q += f" LIMIT {limit}"
    flags = conn.execute(q, (min_score,)).fetchall()
    log(f"Flags da processare: {len(flags)} (min_score={min_score}, dry_run={dry_run})")

    stats = dict(entities_created=0, relations_created=0, flags_linked=0, skipped=0)

    # Precarica tutte le entità esistenti in cache (nome → id, CF → id)
    name_cache: dict[str, int] = {}   # normalized name → entity_id
    cf_cache: dict[str, int] = {}     # CF/PIVA → entity_id

    existing = conn.execute("SELECT id, name, codice_fiscale, partita_iva FROM entities").fetchall()
    for row in existing:
        name_cache[row["name"]] = row["id"]
        if row["codice_fiscale"]:
            cf_cache[row["codice_fiscale"]] = row["id"]
        if row["partita_iva"]:
            cf_cache[row["partita_iva"]] = row["id"]
    log(f"Entità pre-caricate in cache: {len(existing)}")

    def get_or_create(name: str, etype: str, cf: str = "") -> int | None:
        if not name or len(name) < 3:
            return None
        n = norm(name)

        # 1. Cache per nome
        if n in name_cache:
            return name_cache[n]

        # 2. Cache per CF/PIVA (più affidabile)
        if cf and cf in cf_cache:
            name_cache[n] = cf_cache[cf]
            return cf_cache[cf]

        # 3. Crea nuova entità
        if dry_run:
            fake_id = -(len(name_cache) + 1)
            name_cache[n] = fake_id
            return fake_id

        meta = {}
        if cf:
            meta["partita_iva" if etype == "AZIENDA" else "codice_fiscale"] = cf

        cur = conn.execute(
            """INSERT INTO entities
               (name, name_original, entity_type, codice_fiscale, partita_iva,
                metadata, confidence, first_seen_at, last_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                n, name, etype,
                cf if etype != "AZIENDA" else None,
                cf if etype == "AZIENDA" else None,
                json.dumps(meta) if meta else "{}",
                0.9, now, now,
            ),
        )
        eid = cur.lastrowid
        name_cache[n] = eid
        if cf:
            cf_cache[cf] = eid
        stats["entities_created"] += 1
        return eid

    def rel_exists(e1: int, e2: int, rtype: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM relations WHERE entity1_id=? AND entity2_id=? AND relation_type=? LIMIT 1",
            (e1, e2, rtype),
        ).fetchone() is not None

    for i, row in enumerate(flags):
        try:
            data = json.loads(row["data_json"])
        except Exception:
            stats["skipped"] += 1
            continue

        buyer_name = (data.get("buyer_name") or "").strip()
        buyer_id   = (data.get("buyer_id") or "").strip()
        sup_name   = (data.get("supplier_name") or "").strip()
        sup_id     = (data.get("supplier_id") or "").strip()

        if not buyer_name or not sup_name:
            stats["skipped"] += 1
            continue

        buyer_eid = get_or_create(buyer_name, "ENTE_PUBBLICO", buyer_id)
        sup_eid   = get_or_create(sup_name, "AZIENDA", sup_id)

        if not buyer_eid or not sup_eid:
            stats["skipped"] += 1
            continue

        rel_type = RELATION_TYPE_MAP.get(row["anomaly_type"], "VINCE")

        if not dry_run and buyer_eid > 0 and sup_eid > 0:
            if not rel_exists(sup_eid, buyer_eid, rel_type):
                conn.execute(
                    """INSERT INTO relations
                       (entity1_id, entity2_id, relation_type, description,
                        source_type, confidence, is_suspicious, created_at, weight)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        sup_eid, buyer_eid, rel_type,
                        (row["description"] or "")[:300],
                        "ANAC", row["anomaly_score"],
                        1 if row["anomaly_score"] >= 0.6 else 0,
                        now, row["anomaly_score"],
                    ),
                )
                stats["relations_created"] += 1

            conn.execute(
                "UPDATE anomaly_flags SET entity_id=? WHERE id=?",
                (sup_eid, row["id"]),
            )
            stats["flags_linked"] += 1
        elif dry_run:
            stats["relations_created"] += 1
            stats["flags_linked"] += 1

        if (i + 1) % 100 == 0:
            if not dry_run:
                conn.commit()
            log(f"  {i+1}/{len(flags)} | entità create: {stats['entities_created']} | relazioni: {stats['relations_created']}")

    if not dry_run:
        conn.commit()
    conn.close()

    log("=" * 50)
    log(f"{'DRY-RUN ' if dry_run else ''}COMPLETATO")
    log(f"  Entità create:          {stats['entities_created']}")
    log(f"  Relazioni VINCE create: {stats['relations_created']}")
    log(f"  Flags linkate:          {stats['flags_linked']}")
    log(f"  Saltati:                {stats['skipped']}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-score", type=float, default=0.0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    run(min_score=args.min_score, dry_run=args.dry_run, limit=args.limit)
