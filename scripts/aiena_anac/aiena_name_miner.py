#!/usr/bin/env python3
"""
AIena Name Miner — Estrae PERSONA da nomi aziendali italiani.

Molte ditte individuali / SNC / SAS italiane includono il nome del titolare
nel nome aziendale: "GIELLE DI GALANTUCCI LUIGI", "CAR SERVICE DI ROSSI MARIO".

Questo script:
1. Scansiona entities KG (tipo AZIENDA)
2. Estrae nomi personali con regex pattern "DI NOME COGNOME"
3. Crea entità PERSONA nel KG
4. Crea relazione AMMINISTRA (company → persona)
5. Idempotente: salta se PERSONA già esiste

Uso:
    python aiena_name_miner.py           # Esegue
    python aiena_name_miner.py --dry-run # Solo preview
"""

import argparse
import re
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path

KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")

SKIP_WORDS = {
    'SRL', 'SPA', 'SNC', 'SAS', 'SRLS', 'SOCIETA', 'SOC', 'AZIENDA', 'IMPRESA',
    'SERVIZI', 'SERVIZIO', 'STUDIO', 'GESTIONE', 'GESTIONI', 'TECNICA', 'TECNICO',
    'LAVORI', 'COSTRUZIONI', 'INGEGNERIA', 'IMMOBILIARE', 'CONSULENZA',
    'INFORMATICA', 'SISTEMI', 'CENTRO', 'GRUPPO', 'UNIONE', 'ASSOCIAZIONE',
    'COOPERATIVE', 'COOP', 'COMUNE', 'PROVINCIA', 'REGIONE', 'UNIVERSITA',
    'ISTITUTO', 'NORD', 'SUD', 'EST', 'OVEST', 'ITALIA', 'ITALIANA', 'ITALIANO',
    'ORGANIZZAZIONE', 'FONDAZIONE', 'AUTONOLEGGIO', 'ASSICURAZIONI',
    'FRATELLI', 'INIZIATIVE', 'DOTTOR', 'DOTT', 'RAGIONIERE', 'RAG',
    'INGEGNERE', 'ING', 'ARCHITETTO', 'AVVOCATO', 'NOTAIO',
    'RICERCHE', 'CHIMICHE', 'CREDITO', 'COOPERATIVO', 'PROMOZIONE',
    'SOCIALE', 'EVENTI', 'CULTURA', 'CULTURALE', 'CULTURALI',
    'ROMA', 'MILANO', 'NAPOLI', 'TORINO', 'FIRENZE', 'BOLOGNA', 'VENEZIA',
    'GENOVA', 'PALERMO', 'CATANIA', 'BARI', 'BRINDISI', 'LECCE',
}

# "DI" + 2-4 capitalized all-alpha words, min 3 chars each
NAME_RE = re.compile(
    r'\bDI\s+([A-ZÀÈÌÒÙ][A-ZÀÈÌÒÙ\']{2,}(?:\s+[A-ZÀÈÌÒÙ][A-ZÀÈÌÒÙ\']{2,}){1,3})\b'
)


def ts():
    return datetime.now().strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def norm(name: str) -> str:
    return " ".join(name.upper().split())


def extract_person(company_name: str) -> str | None:
    """Extract person name from Italian company name."""
    cn = company_name.upper().strip()
    matches = NAME_RE.findall(cn)
    for m in matches:
        words = m.split()
        if (2 <= len(words) <= 4
                and all(re.match(r'^[A-ZÀÈÌÒÙ]{3,}$', w) for w in words)
                and not any(w in SKIP_WORDS for w in words)):
            return " ".join(words)
    return None


def run(dry_run: bool = False, min_flags: int = 0):
    """Main execution."""
    now = datetime.now(timezone.utc).isoformat()
    source = "NAME_MINING"

    conn = sqlite3.connect(str(KG_DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row

    # Load target companies (with anomaly flags if min_flags > 0)
    if min_flags > 0:
        companies = conn.execute(f"""
            SELECT e.id, e.name, e.partita_iva,
                COUNT(af.id) as flag_count,
                MAX(af.anomaly_score) as max_score
            FROM entities e
            JOIN anomaly_flags af ON af.entity_id = e.id
            WHERE e.entity_type = 'AZIENDA'
            GROUP BY e.id
            HAVING flag_count >= {min_flags}
        """).fetchall()
    else:
        companies = conn.execute("""
            SELECT e.id, e.name, e.partita_iva,
                0 as flag_count, 0.0 as max_score
            FROM entities e
            WHERE e.entity_type = 'AZIENDA'
        """).fetchall()

    log(f"Companies scanned: {len(companies)} (min_flags={min_flags}, dry_run={dry_run})")

    # Preload existing PERSONA name cache
    persona_cache: dict[str, int] = {}
    for row in conn.execute("SELECT id, name FROM entities WHERE entity_type='PERSONA'"):
        persona_cache[row["name"]] = row["id"]
    log(f"Existing PERSONA entities: {len(persona_cache)}")

    stats = dict(persons_created=0, relations_created=0, skipped_exists=0, no_person=0)

    for company in companies:
        person_name = extract_person(company["name"])
        if not person_name:
            stats["no_person"] += 1
            continue

        p_norm = norm(person_name)

        # Get or create PERSONA
        if p_norm in persona_cache:
            person_id = persona_cache[p_norm]
            log(f"  EXISTS: {p_norm} (id={person_id})")
            stats["skipped_exists"] += 1
        else:
            if dry_run:
                person_id = -(len(persona_cache) + 1)
                persona_cache[p_norm] = person_id
                log(f"  [DRY] CREATE PERSONA: {p_norm} ← {company['name'][:55]}")
            else:
                cur = conn.execute(
                    """INSERT INTO entities
                       (name, name_original, entity_type, metadata,
                        confidence, first_seen_at, last_updated_at)
                       VALUES (?, ?, 'PERSONA', ?, 0.7, ?, ?)""",
                    (p_norm, person_name,
                     json.dumps({"source": source, "extracted_from": company["name"]}),
                     now, now),
                )
                person_id = cur.lastrowid
                persona_cache[p_norm] = person_id
                log(f"  CREATE PERSONA: {p_norm} (id={person_id}) ← {company['name'][:45]}")
            stats["persons_created"] += 1

        # Check if AMMINISTRA relation already exists
        existing_rel = conn.execute(
            """SELECT 1 FROM relations 
               WHERE entity1_id=? AND entity2_id=? AND relation_type='AMMINISTRA'
               LIMIT 1""",
            (company["id"], person_id),
        ).fetchone()

        if existing_rel:
            continue

        # Create AMMINISTRA relation: company → person
        if not dry_run and person_id > 0:
            conn.execute(
                """INSERT INTO relations
                   (entity1_id, entity2_id, relation_type, description,
                    source_type, confidence, is_suspicious, created_at, weight)
                   VALUES (?, ?, 'AMMINISTRA', ?, ?, 0.7, 0, ?, 0.7)""",
                (
                    company["id"], person_id,
                    f"Titolare/socio estratto da nome aziendale",
                    source, now,
                ),
            )
        elif dry_run:
            log(f"  [DRY] AMMMINISTRA: {company['name'][:40]} → {p_norm}")
        stats["relations_created"] += 1

    if not dry_run:
        conn.commit()
    conn.close()

    log("=" * 55)
    log(f"{'DRY-RUN ' if dry_run else ''}COMPLETATO")
    log(f"  PERSONA create:       {stats['persons_created']}")
    log(f"  AMMINISTRA create:    {stats['relations_created']}")
    log(f"  Già esistenti:        {stats['skipped_exists']}")
    log(f"  Senza persona:        {stats['no_person']}")
    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-flags", type=int, default=0,
                        help="Solo aziende con almeno N anomaly_flags")
    args = parser.parse_args()
    run(dry_run=args.dry_run, min_flags=args.min_flags)
