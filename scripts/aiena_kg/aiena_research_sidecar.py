#!/usr/bin/env python3
"""
C12 AIena Research Sidecar — Ingesta entita/relazioni dalla ricerca AIena nel KG.

Flusso:
1. Scansiona /home/pinky/.pinkybot/data/agents/aiena/ per:
   - File kg_data_*.json generati esplicitamente da AIena (primary path)
   - File *_research.md con blocchi ```json_kg (secondary path)
   - File session_*.json con key_facts_investigati (tertiary path)
2. Estrae entita e relazioni
3. Ingesta nel KG con source_type="RICERCA"
4. Traccia i file processati in SQLite per evitare duplicati

Uso:
    python3 aiena_research_sidecar.py           # processa tutto il nuovo
    python3 aiena_research_sidecar.py --status  # mostra statistiche
    python3 aiena_research_sidecar.py --force   # riprocessa anche gia processati
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# Path setup
# ============================================================

SCRIPTS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from aiena_kg.aiena_kg import KnowledgeGraph, ENTITY_TYPES, RELATION_TYPES

AIENA_AGENT_DIR = Path("/home/pinky/.pinkybot/data/agents/aiena")
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
STATE_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_research_sidecar.db")

# Entity type mapping from json_kg format to KG types
TYPE_MAP = {
    "PERSONA": "PERSONA",
    "AZIENDA": "AZIENDA",
    "ENTE_PUBBLICO": "ENTE_PUBBLICO",
    "INDIRIZZO": "INDIRIZZO",
    "APPALTO": "APPALTO",
    "ALTRO": "ALTRO",
    # Aliases comuni
    "PERSONA_FISICA": "PERSONA",
    "EMPRESA": "AZIENDA",
    "ENTE": "ENTE_PUBBLICO",
    "PUBBLICO": "ENTE_PUBBLICO",
    "COMUNE": "ENTE_PUBBLICO",
    "ASL": "ENTE_PUBBLICO",
    "OSPEDALE": "ENTE_PUBBLICO",
    "REGIONE": "ENTE_PUBBLICO",
    "MINISTERO": "ENTE_PUBBLICO",
    "SRL": "AZIENDA",
    "SPA": "AZIENDA",
    "SOCIETA": "AZIENDA",
    "CONSORZI": "AZIENDA",
    "COOPERATIVA": "AZIENDA",
}

# Relation type mapping
RELATION_MAP = {
    "VINCE": "VINCE",
    "AMMINISTRA": "AMMINISTRA",
    "POSSIEDE": "POSSIEDE",
    "LAVORA_PER": "LAVORA_PER",
    "COLLABORA": "COLLABORA",
    "PAGA": "PAGA",
    "RICEVE": "RICEVE",
    "LOCALIZZATO_IN": "LOCALIZZATO_IN",
    "CORRELATO": "CORRELATO",
    "SUCCESSORE_DI": "SUCCESSORE_DI",
    "CONTROLLATO_DA": "CONTROLLATO_DA",
    # Aliases
    "VINCE_APPALTO": "VINCE",
    "GESTISCE": "AMMINISTRA",
    "DIRIGE": "AMMINISTRA",
    "TITOLARE": "POSSIEDE",
    "PROPRIETARIO": "POSSIEDE",
    "AGGIUDICA": "VINCE",
    "ASSEGNA": "PAGA",
    "PAGATO_DA": "RICEVE",
    "CONNESSO": "CORRELATO",
    "LEGATO_A": "CORRELATO",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ============================================================
# State DB — traccia file processati
# ============================================================

def init_state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(STATE_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            file_type TEXT NOT NULL,  -- 'kg_json', 'research_md', 'session_json'
            processed_at TEXT NOT NULL,
            entities_added INTEGER DEFAULT 0,
            relations_added INTEGER DEFAULT 0,
            errors TEXT
        );
        CREATE TABLE IF NOT EXISTS sidecar_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            entity_name TEXT,
            entity_type TEXT,
            relation_type TEXT,
            source_file TEXT,
            created_at TEXT NOT NULL
        );
    """)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, file_path: str) -> bool:
    row = conn.execute(
        "SELECT id FROM processed_files WHERE file_path = ?", (file_path,)
    ).fetchone()
    return row is not None


def mark_processed(
    conn: sqlite3.Connection,
    file_path: str,
    file_type: str,
    entities: int,
    relations: int,
    errors: str = ""
):
    conn.execute("""
        INSERT OR REPLACE INTO processed_files
            (file_path, file_type, processed_at, entities_added, relations_added, errors)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (file_path, file_type, now_iso(), entities, relations, errors))
    conn.commit()


# ============================================================
# Parser: kg_data_*.json (explicit AIena output)
# ============================================================

def parse_kg_json_file(path: Path) -> dict:
    """
    Parsa un file kg_data_*.json generato da AIena.

    Formato atteso:
    {
        "slug": "...",
        "title": "...",
        "entities": [
            {"name": "...", "type": "PERSONA", "notes": "...", "cf": "..."}
        ],
        "relations": [
            {"from": "...", "to": "...", "type": "VINCE", "desc": "...", "confidence": 0.9}
        ]
    }
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}

    return {
        "slug": data.get("slug", path.stem.replace("kg_data_", "")),
        "title": data.get("title", ""),
        "entities": data.get("entities", []),
        "relations": data.get("relations", []),
        "source_url": data.get("source_url", ""),
    }


# ============================================================
# Parser: *_research.md (blocchi ```json_kg)
# ============================================================

JSON_KG_PATTERN = re.compile(
    r"```json_kg\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE
)


def parse_research_md(path: Path) -> list[dict]:
    """
    Parsa file markdown per blocchi ```json_kg.
    Ritorna lista di dict entita/relazioni.
    """
    results = []
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return results

    for match in JSON_KG_PATTERN.finditer(content):
        try:
            block = json.loads(match.group(1))
            slug = path.stem.replace("_research", "").replace("research_", "")
            block.setdefault("slug", slug)
            results.append(block)
        except json.JSONDecodeError:
            pass  # blocco malformato, skip

    return results


# ============================================================
# Parser: session_*.json — key_facts_investigati
# ============================================================

def parse_session_json(path: Path) -> dict:
    """
    Estrae entita da session_*.json tramite key_facts_investigati.
    Formato:
    {
        "key_facts_investigati": {
            "azienda_slug": {
                "proprietario": "...",
                "nascita_azienda": "...",
                "appalto_brand_...": "...",
                "anomalia": "..."
            }
        }
    }
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    facts = data.get("key_facts_investigati", {})
    if not facts:
        return {}

    entities = []
    relations = []

    for slug, info in facts.items():
        # Crea entita azienda da slug
        azienda_name = slug.replace("_", " ").title()
        azienda_entry = {
            "name": azienda_name,
            "type": "AZIENDA",
            "notes": info.get("anomalia", "")
        }
        entities.append(azienda_entry)

        # Proprietario → PERSONA + POSSIEDE
        proprietario = info.get("proprietario")
        if proprietario:
            # Estrai solo nome (rimuovi età/info tra virgole)
            nome = proprietario.split(",")[0].strip()
            if nome and len(nome) > 3:
                entities.append({"name": nome, "type": "PERSONA", "notes": ""})
                relations.append({
                    "from": nome,
                    "to": azienda_name,
                    "type": "AMMINISTRA",
                    "desc": f"Proprietario/amministratore di {azienda_name}"
                })

        # Cerca pagante (ente_pagante, committente, comune_*)
        for key, val in info.items():
            if any(k in key.lower() for k in ["committente", "comune", "ente", "regione", "asl"]):
                if isinstance(val, str) and len(val) > 3:
                    entities.append({"name": val, "type": "ENTE_PUBBLICO", "notes": ""})
                    relations.append({
                        "from": azienda_name,
                        "to": val,
                        "type": "RICEVE",
                        "desc": f"Appalto/pagamento da {val}"
                    })

    return {
        "slug": path.stem,
        "entities": entities,
        "relations": relations,
    }


# ============================================================
# KG Ingester
# ============================================================

def ingest_into_kg(
    kg: KnowledgeGraph,
    data: dict,
    source_file: str,
    state_conn: sqlite3.Connection,
) -> tuple[int, int]:
    """
    Ingesta entities e relations nel KG.
    Returns (entities_added, relations_added).
    """
    entities_added = 0
    relations_added = 0

    slug = data.get("slug", "unknown")
    source_url = data.get("source_url", "")

    entities = data.get("entities", [])
    relations = data.get("relations", [])

    # Prima passata: aggiungi tutte le entita
    for ent in entities:
        raw_name = ent.get("name", "").strip()
        raw_type = (ent.get("type") or ent.get("entity_type") or "ALTRO").upper()

        if not raw_name or len(raw_name) < 2:
            continue

        # Mappa il tipo
        ent_type = TYPE_MAP.get(raw_type, "ALTRO")
        if ent_type not in ENTITY_TYPES:
            ent_type = "ALTRO"

        # Confidence basata su presenza di tipo esplicito
        confidence = 0.8 if raw_type in ENTITY_TYPES else 0.6

        metadata = {}
        if ent.get("notes"):
            metadata["notes"] = ent["notes"]
        if ent.get("cf"):
            metadata["cf"] = ent["cf"]
        if ent.get("piva"):
            metadata["piva"] = ent["piva"]

        try:
            entity_id = kg.add_entity(
                name=raw_name,
                entity_type=ent_type,
                metadata=metadata if metadata else None,
                codice_fiscale=ent.get("cf"),
                partita_iva=ent.get("piva"),
                confidence=confidence,
            )
            # Aggiungi fonte
            try:
                kg.add_entity_source(
                    entity_id=entity_id,
                    source_type="RICERCA",
                    source_url=source_url or f"aiena://research/{slug}",
                    notes=ent.get("notes", ""),
                )
            except Exception:
                pass  # add_entity_source potrebbe non esistere in tutte le versioni

            entities_added += 1
            state_conn.execute("""
                INSERT INTO sidecar_log (event_type, entity_name, entity_type, source_file, created_at)
                VALUES ('ENTITY', ?, ?, ?, ?)
            """, (raw_name, ent_type, source_file, now_iso()))

        except Exception as e:
            log(f"  ⚠ Entity skip '{raw_name}': {e}")

    # Seconda passata: aggiungi relazioni
    for rel in relations:
        from_name = rel.get("from", "").strip()
        to_name = rel.get("to", "").strip()
        raw_rel_type = (rel.get("type") or rel.get("relation_type") or "CORRELATO").upper()

        if not from_name or not to_name:
            continue

        rel_type = RELATION_MAP.get(raw_rel_type, "CORRELATO")
        if rel_type not in RELATION_TYPES:
            rel_type = "CORRELATO"

        try:
            rel_id = kg.add_relation(
                entity1_name=from_name,
                entity2_name=to_name,
                relation_type=rel_type,
                source_type="RICERCA",
                source_url=source_url or f"aiena://research/{slug}",
                description=rel.get("desc") or rel.get("description"),
                confidence=float(rel.get("confidence", 0.7)),
            )
            if rel_id:
                relations_added += 1
                state_conn.execute("""
                    INSERT INTO sidecar_log
                        (event_type, entity_name, entity_type, relation_type, source_file, created_at)
                    VALUES ('RELATION', ?, ?, ?, ?, ?)
                """, (f"{from_name} → {to_name}", rel_type, rel_type, source_file, now_iso()))

        except Exception as e:
            log(f"  ⚠ Relation skip '{from_name}→{to_name}': {e}")

    state_conn.commit()
    return entities_added, relations_added


# ============================================================
# Scanner
# ============================================================

def scan_and_ingest(force: bool = False):
    """
    Scansiona la directory AIena e ingesta entita nel KG.
    """
    if not AIENA_AGENT_DIR.exists():
        log(f"Directory AIena non trovata: {AIENA_AGENT_DIR}")
        return

    if not KG_DB_PATH.exists():
        log(f"KG DB non trovato: {KG_DB_PATH}")
        return

    kg = KnowledgeGraph(KG_DB_PATH)
    state_conn = init_state_db()

    total_entities = 0
    total_relations = 0
    files_processed = 0
    files_skipped = 0

    # --- Path 1: kg_data_*.json (explicit AIena output) ---
    kg_json_files = sorted(AIENA_AGENT_DIR.glob("kg_data_*.json"))
    log(f"Path 1 (kg_data_*.json): {len(kg_json_files)} file trovati")

    for path in kg_json_files:
        path_str = str(path)
        if not force and is_processed(state_conn, path_str):
            files_skipped += 1
            continue

        log(f"  → Processa: {path.name}")
        data = parse_kg_json_file(path)
        if "error" in data:
            log(f"  ⚠ Errore parse: {data['error']}")
            mark_processed(state_conn, path_str, "kg_json", 0, 0, data["error"])
            continue

        ents, rels = ingest_into_kg(kg, data, path.name, state_conn)
        mark_processed(state_conn, path_str, "kg_json", ents, rels)
        log(f"  ✓ {ents} entita, {rels} relazioni")
        total_entities += ents
        total_relations += rels
        files_processed += 1

    # --- Path 2: *_research.md con blocchi ```json_kg ---
    md_files = sorted(AIENA_AGENT_DIR.glob("*_research.md")) + sorted(
        AIENA_AGENT_DIR.glob("*research*.md")
    )
    # Dedup
    md_files = list(dict.fromkeys(md_files))
    log(f"Path 2 (*_research.md): {len(md_files)} file trovati")

    for path in md_files:
        path_str = str(path)
        if not force and is_processed(state_conn, path_str):
            files_skipped += 1
            continue

        blocks = parse_research_md(path)
        if not blocks:
            # Marca comunque come processato (no blocchi json_kg)
            mark_processed(state_conn, path_str, "research_md", 0, 0, "no_json_kg_blocks")
            continue

        log(f"  → Processa: {path.name} ({len(blocks)} blocchi json_kg)")
        total_ents = 0
        total_rels = 0
        for block in blocks:
            ents, rels = ingest_into_kg(kg, block, path.name, state_conn)
            total_ents += ents
            total_rels += rels

        mark_processed(state_conn, path_str, "research_md", total_ents, total_rels)
        log(f"  ✓ {total_ents} entita, {total_rels} relazioni")
        total_entities += total_ents
        total_relations += total_rels
        files_processed += 1

    # --- Path 3: session_*.json con key_facts_investigati ---
    session_files = sorted(AIENA_AGENT_DIR.glob("session_*.json"))
    log(f"Path 3 (session_*.json): {len(session_files)} file trovati")

    for path in session_files:
        path_str = str(path)
        if not force and is_processed(state_conn, path_str):
            files_skipped += 1
            continue

        data = parse_session_json(path)
        if not data:
            mark_processed(state_conn, path_str, "session_json", 0, 0, "no_key_facts")
            continue

        ents_list = data.get("entities", [])
        rels_list = data.get("relations", [])
        if not ents_list and not rels_list:
            mark_processed(state_conn, path_str, "session_json", 0, 0, "empty")
            continue

        log(f"  → Processa: {path.name} ({len(ents_list)} entita, {len(rels_list)} relazioni)")
        ents, rels = ingest_into_kg(kg, data, path.name, state_conn)
        mark_processed(state_conn, path_str, "session_json", ents, rels)
        log(f"  ✓ {ents} entita, {rels} relazioni")
        total_entities += ents
        total_relations += rels
        files_processed += 1

    state_conn.close()

    log("")
    log(f"=== Sidecar completato ===")
    log(f"File processati: {files_processed}")
    log(f"File saltati (gia processati): {files_skipped}")
    log(f"Entita aggiunte/aggiornate: {total_entities}")
    log(f"Relazioni aggiunte/aggiornate: {total_relations}")

    return total_entities, total_relations


def show_status():
    """Mostra statistiche sidecar."""
    state_conn = init_state_db()

    total = state_conn.execute("SELECT COUNT(*) FROM processed_files").fetchone()[0]
    by_type = state_conn.execute("""
        SELECT file_type, COUNT(*), SUM(entities_added), SUM(relations_added)
        FROM processed_files GROUP BY file_type
    """).fetchall()
    recent = state_conn.execute("""
        SELECT file_path, entities_added, relations_added, processed_at
        FROM processed_files ORDER BY processed_at DESC LIMIT 10
    """).fetchall()

    log(f"=== C12 Research Sidecar Status ===")
    log(f"File totali processati: {total}")
    for row in by_type:
        log(f"  {row[0]}: {row[1]} file, {row[2] or 0} entita, {row[3] or 0} relazioni")

    if recent:
        log(f"\nUltimi 10 file processati:")
        for row in recent:
            fname = Path(row[0]).name
            log(f"  {fname}: {row[1]}e {row[2]}r — {row[3][:16]}")

    # KG stats
    if KG_DB_PATH.exists():
        kg = KnowledgeGraph(KG_DB_PATH)
        stats = kg.get_stats() if hasattr(kg, "get_stats") else {}
        if stats:
            log(f"\nKG totale: {stats.get('entities', '?')} entita, {stats.get('relations', '?')} relazioni")

    state_conn.close()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="C12 AIena Research Sidecar — Ingesta entita dalla ricerca nel KG"
    )
    parser.add_argument("--status", action="store_true", help="Mostra statistiche")
    parser.add_argument("--force", action="store_true", help="Riprocessa anche file gia processati")
    args = parser.parse_args()

    if args.status:
        show_status()
    else:
        scan_and_ingest(force=args.force)


if __name__ == "__main__":
    main()
