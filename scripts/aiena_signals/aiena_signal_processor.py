#!/usr/bin/env python3
"""
AIena Signal Processor — Elabora segnalazioni come seed investigativi.

Flusso:
1. Legge ticket da Supabase (tabella `tickets`)
2. Estrae entita nominate (persone, aziende, enti, luoghi)
3. Per ogni entita: cerca nel Knowledge Graph se gia nota
4. Calcola "investigability score" basato su connessioni note e anomalie correlate
5. Output: segnalazioni ordinate per score, pronte per backlog prioritario

Integrazione:
- Quando score alto -> inserimento prioritario in pipeline.json["backlog"]
- Il backlog scout le processera con prompt arricchito dal KG

Decisioni architetturali:
- HTTP diretto su Supabase (niente SDK per leggerezza)
- Cache locale per evitare rielaborazioni
- Score composito pesato
"""
import json
import os
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Import locali (supporta sia import package che esecuzione diretta)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from aiena_signals.entity_extractor import EntityExtractor
    from aiena_kg.aiena_kg import KnowledgeGraph
except ImportError:
    from entity_extractor import EntityExtractor
    sys.path.insert(0, str(Path(__file__).parent.parent / "aiena_kg"))
    from aiena_kg import KnowledgeGraph

# ============================================================
# Configurazione
# ============================================================

# Supabase (stesse credenziali di aiena_ticket_dispatcher.py)
def _sb_service_key() -> str:
    """Legge SB_SERVICE_KEY da scripts/.aiena_secrets (fallback: variabile d'ambiente)."""
    if os.environ.get("SB_SERVICE_KEY"):
        return os.environ["SB_SERVICE_KEY"]
    secrets_file = Path("/home/pinky/.pinkybot/scripts/.aiena_secrets")
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                if k.strip() == "SB_SERVICE_KEY":
                    return v.strip()
    raise SystemExit("SB_SERVICE_KEY mancante: controlla scripts/.aiena_secrets")


SUPABASE_URL = "https://fwyjxolljcogblvwvfca.supabase.co"
SUPABASE_KEY = _sb_service_key()

# Paths
STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_signal_processor_state.json")
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
ANAC_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_anac_cache.db")

# Pesi per calcolo score investigabilita
SCORE_WEIGHTS = {
    "kg_entity_known": 0.15,           # Entita gia nota nel KG
    "kg_entity_suspicious": 0.25,      # Entita marcata sospetta
    "kg_connections": 0.10,            # Per ogni connessione nota
    "kg_prior_investigations": 0.20,   # Gia investigata prima
    "anac_anomalies": 0.30,            # Anomalie ANAC correlate
    "corroboration": 0.25,             # Altre segnalazioni simili
    "entity_count": 0.05,              # Piu entita = piu strutturata
    "has_attachment": 0.10,            # Ha allegato documentale
}

# Soglia per considerare una segnalazione "alta priorita"
HIGH_PRIORITY_THRESHOLD = 0.6


# ============================================================
# Utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def supabase_request(
    endpoint: str,
    method: str = "GET",
    data: dict | None = None
) -> list | dict | None:
    """Esegue richiesta a Supabase REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

    req_data = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=req_data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read())
    except Exception as e:
        print(f"[SignalProcessor] Supabase error: {e}")
        return None


def load_state() -> dict:
    """Carica stato processore."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_tickets": [], "last_run": None}


def save_state(state: dict):
    """Salva stato processore."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def load_pipeline() -> dict:
    """Carica pipeline.json."""
    if PIPELINE_JSON.exists():
        return json.loads(PIPELINE_JSON.read_text())
    return {"investigations": [], "leads": [], "published": [], "events": []}


def save_pipeline(data: dict):
    """Salva pipeline.json con atomic write."""
    import tempfile
    import os

    tmp_path = PIPELINE_JSON.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.rename(tmp_path, PIPELINE_JSON)


# ============================================================
# Classe principale
# ============================================================

class SignalProcessor:
    """
    Processore di segnalazioni per trasformarle in seed investigativi.

    Esempio:
        processor = SignalProcessor()
        results = processor.process_new_tickets()
        for r in results:
            if r['investigability_score'] > 0.6:
                print(f"Alta priorita: {r['ticket_code']}")
    """

    def __init__(self):
        self.extractor = EntityExtractor()
        self.kg = KnowledgeGraph(KG_DB_PATH) if KG_DB_PATH.exists() else None

        # Cache per correlazioni
        self._corroboration_cache: dict[str, list] = {}

    def fetch_tickets(
        self,
        limit: int = 50,
        status_filter: str | None = None
    ) -> list[dict]:
        """
        Recupera ticket da Supabase.

        Args:
            limit: Numero massimo ticket
            status_filter: Filtra per status (opzionale)
        """
        endpoint = f"tickets?select=*&order=created_at.desc&limit={limit}"
        if status_filter:
            endpoint += f"&status=eq.{status_filter}"

        result = supabase_request(endpoint)
        return result if isinstance(result, list) else []

    def extract_entities_from_ticket(self, ticket: dict) -> list[dict]:
        """Estrae entita da un ticket."""
        # Combina campi testuali
        text_parts = [
            ticket.get("titolo", ""),
            ticket.get("descrizione", ""),
        ]
        combined_text = " ".join(filter(None, text_parts))

        if not combined_text.strip():
            return []

        entities = self.extractor.extract(combined_text, min_confidence=0.5)
        return [e.to_dict() for e in entities]

    def check_kg_presence(self, entity_name: str, entity_type: str) -> dict:
        """
        Verifica se un'entita e gia nota nel Knowledge Graph.

        Returns:
            Dict con info su presenza, connessioni, sospetto, indagini precedenti
        """
        if not self.kg:
            return {"known": False}

        # Cerca nel KG
        results = self.kg.search_entity(entity_name, limit=1)
        if not results:
            return {"known": False}

        entity = results[0]
        fuzzy_dist = entity.get("_fuzzy_distance", 0)

        # Se match fuzzy troppo distante, non considerarlo
        if fuzzy_dist > 3:
            return {"known": False}

        # Ottieni connessioni
        relations = self.kg.get_relations(entity["id"])

        return {
            "known": True,
            "kg_entity_id": entity["id"],
            "suspicious": bool(entity.get("is_suspicious")),
            "investigation_count": entity.get("investigation_count", 0),
            "connection_count": len(relations),
            "confidence": entity.get("confidence", 1.0),
        }

    def check_anac_anomalies(self, entity_name: str) -> list[dict]:
        """
        Cerca anomalie ANAC correlate a un'entita.
        """
        if not ANAC_DB_PATH.exists():
            return []

        try:
            conn = sqlite3.connect(str(ANAC_DB_PATH), timeout=10)
            conn.row_factory = sqlite3.Row

            # Cerca come supplier o buyer
            pattern = f"%{entity_name.upper()}%"
            rows = conn.execute("""
                SELECT cig, title, amount_value, flag_reason
                FROM anac_tenders
                WHERE (UPPER(supplier_name) LIKE ? OR UPPER(buyer_name) LIKE ?)
                  AND flagged = 1
                LIMIT 10
            """, (pattern, pattern)).fetchall()

            conn.close()
            return [dict(r) for r in rows]

        except Exception as e:
            print(f"[SignalProcessor] ANAC query error: {e}")
            return []

    def check_corroboration(self, entities: list[dict]) -> int:
        """
        Verifica se altre segnalazioni menzionano le stesse entita.

        Returns:
            Numero di segnalazioni correlate
        """
        if not entities:
            return 0

        # Crea chiave cache
        entity_names = sorted(e["name"] for e in entities if e["entity_type"] in ("AZIENDA", "ENTE_PUBBLICO"))
        if not entity_names:
            return 0

        cache_key = "|".join(entity_names[:3])  # Max 3 entita per chiave
        if cache_key in self._corroboration_cache:
            return len(self._corroboration_cache[cache_key])

        # Cerca altri ticket con entita simili
        all_tickets = self.fetch_tickets(limit=100)
        correlated = []

        for ticket in all_tickets:
            ticket_text = f"{ticket.get('titolo', '')} {ticket.get('descrizione', '')}"
            for name in entity_names:
                if name.upper() in ticket_text.upper():
                    correlated.append(ticket["ticket_code"])
                    break

        self._corroboration_cache[cache_key] = correlated
        return len(correlated) - 1  # Escludi se stesso

    def calculate_investigability_score(
        self,
        ticket: dict,
        entities: list[dict]
    ) -> tuple[float, dict]:
        """
        Calcola lo score di investigabilita di una segnalazione.

        Returns:
            Tuple (score 0-1, dettagli calcolo)
        """
        score = 0.0
        details: dict[str, Any] = {
            "components": {},
            "entities_checked": [],
            "anac_anomalies": [],
            "corroboration_count": 0,
        }

        # 1. Numero entita estratte
        entity_count = len(entities)
        if entity_count >= 3:
            component = SCORE_WEIGHTS["entity_count"]
            score += component
            details["components"]["entity_count"] = component

        # 2. Ha allegato?
        if ticket.get("allegato_url"):
            component = SCORE_WEIGHTS["has_attachment"]
            score += component
            details["components"]["has_attachment"] = component

        # 3. Check KG per ogni entita
        kg_score = 0.0
        for entity in entities:
            if entity["entity_type"] not in ("AZIENDA", "ENTE_PUBBLICO", "PERSONA"):
                continue

            kg_info = self.check_kg_presence(entity["name"], entity["entity_type"])
            details["entities_checked"].append({
                "name": entity["name"],
                "type": entity["entity_type"],
                "kg_info": kg_info
            })

            if kg_info.get("known"):
                kg_score += SCORE_WEIGHTS["kg_entity_known"]

                if kg_info.get("suspicious"):
                    kg_score += SCORE_WEIGHTS["kg_entity_suspicious"]

                # Bonus per connessioni
                conn_count = kg_info.get("connection_count", 0)
                kg_score += min(conn_count * SCORE_WEIGHTS["kg_connections"], 0.3)

                # Bonus per indagini precedenti
                inv_count = kg_info.get("investigation_count", 0)
                if inv_count > 0:
                    kg_score += SCORE_WEIGHTS["kg_prior_investigations"]

        score += min(kg_score, 0.5)  # Cap KG contribution
        if kg_score > 0:
            details["components"]["kg_presence"] = min(kg_score, 0.5)

        # 4. Check anomalie ANAC
        anac_anomalies = []
        for entity in entities:
            if entity["entity_type"] in ("AZIENDA", "ENTE_PUBBLICO"):
                anomalies = self.check_anac_anomalies(entity["name"])
                anac_anomalies.extend(anomalies)

        if anac_anomalies:
            component = min(len(anac_anomalies) * 0.1, SCORE_WEIGHTS["anac_anomalies"])
            score += component
            details["components"]["anac_anomalies"] = component
            details["anac_anomalies"] = anac_anomalies[:5]  # Max 5 per report

        # 5. Corroborazione
        corr_count = self.check_corroboration(entities)
        details["corroboration_count"] = corr_count
        if corr_count > 0:
            component = min(corr_count * 0.1, SCORE_WEIGHTS["corroboration"])
            score += component
            details["components"]["corroboration"] = component

        # Normalizza score finale
        final_score = min(1.0, max(0.0, score))
        details["final_score"] = final_score

        return final_score, details

    def process_ticket(self, ticket: dict) -> dict:
        """
        Processa un singolo ticket.

        Returns:
            Dict con risultato elaborazione
        """
        ticket_code = ticket.get("ticket_code", "?")

        # Estrai entita
        entities = self.extract_entities_from_ticket(ticket)

        # Calcola score
        score, details = self.calculate_investigability_score(ticket, entities)

        return {
            "ticket_code": ticket_code,
            "ticket_id": ticket.get("id"),
            "titolo": ticket.get("titolo"),
            "tipo": ticket.get("tipo"),
            "status": ticket.get("status"),
            "created_at": ticket.get("created_at"),
            "entities_extracted": entities,
            "investigability_score": score,
            "score_details": details,
            "is_high_priority": score >= HIGH_PRIORITY_THRESHOLD,
            "processed_at": now_iso(),
        }

    def process_new_tickets(
        self,
        reprocess_all: bool = False
    ) -> list[dict]:
        """
        Processa tutti i ticket nuovi.

        Args:
            reprocess_all: Se True, rielabora anche i gia processati

        Returns:
            Lista risultati ordinati per score
        """
        state = load_state()
        processed_codes = set(state.get("processed_tickets", []))

        # Fetch ticket
        tickets = self.fetch_tickets(limit=100)
        print(f"[SignalProcessor] Trovati {len(tickets)} ticket")

        results = []
        for ticket in tickets:
            code = ticket.get("ticket_code", "")

            if code in processed_codes and not reprocess_all:
                continue

            print(f"[SignalProcessor] Elaboro: {code}")
            result = self.process_ticket(ticket)
            results.append(result)

            # Salva come processato
            if code:
                processed_codes.add(code)

        # Ordina per score
        results.sort(key=lambda x: -x["investigability_score"])

        # Salva stato
        state["processed_tickets"] = list(processed_codes)
        state["last_run"] = now_iso()
        save_state(state)

        return results

    def _add_lead_via_api(self, item: dict, priority_score: int) -> bool:
        """
        Aggiunge un lead via API endpoint invece di scrivere direttamente su pipeline.json.

        Args:
            item: Dizionario con i dati del lead
            priority_score: Score numerico (0-100)

        Returns:
            True se API riuscita, False se fallback a file diretto
        """
        # Mappa priority_score a stringa
        if priority_score >= 80:
            priority = "alta"
        elif priority_score >= 60:
            priority = "media"
        else:
            priority = "bassa"

        # Prepara payload per API
        payload = {
            "title": item.get("title"),
            "slug": item.get("slug"),
            "category": item.get("category"),
            "description": item.get("description"),
            "priority": priority,
            "status": item.get("status", "idea"),
            "ticket_ref": item.get("ticket_ref"),
            "notify": False,  # Signal processor non notifica Telegram
        }

        # Chiama API
        api_url = "http://localhost:8888/api/leads/create"
        headers = {
            "Content-Type": "application/json",
        }

        try:
            req_data = json.dumps(payload).encode()
            req = urllib.request.Request(
                api_url,
                data=req_data,
                headers=headers,
                method="POST"
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read())
                if result.get("success"):
                    print(
                        f"[SignalProcessor] Lead aggiunto via API: {item.get('slug')} "
                        f"({item.get('ticket_ref')}, priority: {priority})"
                    )
                    return True
                else:
                    print(
                        f"[SignalProcessor] API ritorno success=false per {item.get('slug')}"
                    )
                    return False

        except Exception as e:
            print(
                f"[SignalProcessor] API call fallito per {item.get('slug')}: {e}. "
                f"Fallback a file diretto."
            )
            return False

    def _fallback_add_to_pipeline(self, item: dict, pipeline: dict) -> None:
        """Fallback: aggiunge item direttamente a pipeline.json."""
        pipeline.setdefault("leads", []).insert(0, item)
        pipeline["updated_at"] = datetime.now().strftime("%Y-%m-%d")
        save_pipeline(pipeline)
        print(f"[SignalProcessor] Lead aggiunto via fallback file: {item.get('slug')}")

    def add_high_priority_to_backlog(
        self,
        results: list[dict],
        max_items: int = 3
    ) -> int:
        """
        Aggiunge segnalazioni ad alta priorita al backlog di pipeline.json
        tramite API endpoint (con fallback a file diretto se API non disponibile).

        Returns:
            Numero di item aggiunti
        """
        high_priority = [r for r in results if r["is_high_priority"]]
        if not high_priority:
            print("[SignalProcessor] Nessuna segnalazione ad alta priorita")
            return 0

        pipeline = load_pipeline()
        existing_slugs = {item.get("slug") for item in pipeline.get("leads", [])}
        existing_refs = {
            item.get("ticket_ref")
            for item in pipeline.get("leads", [])
            if item.get("ticket_ref")
        }

        added = 0
        for result in high_priority[:max_items]:
            code = result["ticket_code"]

            # Skip se gia presente
            if code in existing_refs:
                print(f"[SignalProcessor] {code} gia nel backlog, skip")
                continue

            # Genera slug
            slug = f"segnalazione-{code.lower()}"
            if slug in existing_slugs:
                continue

            # Genera item backlog
            entities_summary = ", ".join(
                e["name"] for e in result["entities_extracted"][:5]
                if e["entity_type"] in ("AZIENDA", "ENTE_PUBBLICO")
            )

            item = {
                "title": f"Segnalazione: {result['titolo'][:60]}",
                "slug": slug,
                "status": "idea",
                "category": self._guess_category(result),
                "description": f"Segnalazione cittadino ({code}). Entita menzionate: {entities_summary}",
                "priority_score": round(result["investigability_score"] * 10),
                "research_notes": [
                    f"Ticket: {code}",
                    f"Score investigabilita: {result['investigability_score']:.2f}",
                    f"Entita estratte: {len(result['entities_extracted'])}",
                ],
                "sources": [],
                "added": datetime.now().strftime("%Y-%m-%d"),
                "ticket_ref": code,
                "signal_source": "aiena_signal_processor",
            }

            # Aggiungi dettagli KG se presenti
            for entity_check in result["score_details"].get("entities_checked", []):
                if entity_check.get("kg_info", {}).get("known"):
                    item["research_notes"].append(
                        f"KG: {entity_check['name']} gia nota "
                        f"({entity_check['kg_info'].get('connection_count', 0)} connessioni)"
                    )

            # Aggiungi anomalie ANAC se presenti
            for anomaly in result["score_details"].get("anac_anomalies", [])[:2]:
                item["research_notes"].append(
                    f"ANAC: Anomalia CIG {anomaly.get('cig')} - {anomaly.get('flag_reason', 'N/A')}"
                )

            # Tenta aggiunta via API, fallback a file se API non disponibile
            priority_score = item["priority_score"]
            if not self._add_lead_via_api(item, priority_score):
                self._fallback_add_to_pipeline(item, pipeline)
                # Ricarica pipeline per il prossimo item (file potrebbe essere stato aggiornato)
                pipeline = load_pipeline()

            added += 1
            print(f"[SignalProcessor] Elaborato: {code} (score: {result['investigability_score']:.2f})")

        return added

    def _guess_category(self, result: dict) -> str:
        """Indovina la categoria basandosi sulle entita estratte."""
        entities = result.get("entities_extracted", [])
        text = f"{result.get('titolo', '')} {result.get('entities_extracted', '')}".lower()

        # Euristiche semplici
        if any(e.get("metadata", {}).get("sottotipo") == "SANITARIO" for e in entities):
            return "Sanita"
        if "ospedale" in text or "asl" in text or "asp" in text:
            return "Sanita"
        if "pnrr" in text or "fondi europei" in text:
            return "PNRR"
        if "appalto" in text or "gara" in text or "cig" in text:
            return "Appalti pubblici"
        if "corruzione" in text or "tangent" in text:
            return "Corruzione"
        if "comune" in text or "provincia" in text:
            return "Pubblica Amministrazione"

        return "Altro"

    def export_kg_context_for_ticket(self, ticket_code: str) -> str:
        """
        Esporta il contesto KG per una segnalazione (per arricchire prompt AIena).
        """
        # Trova ticket
        tickets = self.fetch_tickets(limit=100)
        ticket = next((t for t in tickets if t.get("ticket_code") == ticket_code), None)

        if not ticket:
            return f"[SignalProcessor] Ticket {ticket_code} non trovato"

        # Processa
        result = self.process_ticket(ticket)

        # Genera output
        lines = [
            f"=== CONTESTO SEGNALAZIONE {ticket_code} ===",
            f"Titolo: {result['titolo']}",
            f"Score investigabilita: {result['investigability_score']:.2f}",
            f"Priorita alta: {'SI' if result['is_high_priority'] else 'NO'}",
            "",
            "ENTITA ESTRATTE:",
        ]

        for entity in result["entities_extracted"]:
            lines.append(f"  - [{entity['entity_type']}] {entity['name']} (conf: {entity['confidence']:.0%})")

        lines.append("")
        lines.append("VERIFICA KNOWLEDGE GRAPH:")

        for check in result["score_details"].get("entities_checked", []):
            kg_info = check.get("kg_info", {})
            if kg_info.get("known"):
                lines.append(f"  - {check['name']}: NOTA")
                lines.append(f"    Connessioni: {kg_info.get('connection_count', 0)}")
                if kg_info.get("suspicious"):
                    lines.append("    FLAG: SOSPETTA")
                if kg_info.get("investigation_count", 0) > 0:
                    lines.append(f"    Indagini precedenti: {kg_info['investigation_count']}")

                # Export dettagliato dal KG
                if self.kg:
                    kg_export = self.kg.export_for_aiena(check['name'])
                    lines.append(f"\n{kg_export}")

            else:
                lines.append(f"  - {check['name']}: non nota nel KG")

        # Anomalie ANAC
        anomalies = result["score_details"].get("anac_anomalies", [])
        if anomalies:
            lines.append("")
            lines.append("ANOMALIE ANAC CORRELATE:")
            for a in anomalies:
                lines.append(f"  - CIG {a.get('cig')}: {a.get('title', 'N/A')[:50]}")
                lines.append(f"    Importo: {a.get('amount_value', 0):,.0f} EUR")
                lines.append(f"    Motivo flag: {a.get('flag_reason', 'N/A')}")

        # Corroborazione
        corr = result["score_details"].get("corroboration_count", 0)
        if corr > 0:
            lines.append("")
            lines.append(f"CORROBORAZIONE: {corr} altre segnalazioni menzionano entita simili")

        lines.append("")
        lines.append("=== FINE CONTESTO ===")

        return "\n".join(lines)


# ============================================================
# Test self-contained
# ============================================================

if __name__ == "__main__":
    print("=== Test SignalProcessor ===\n")

    processor = SignalProcessor()

    # Test con ticket di esempio
    test_ticket = {
        "id": 999,
        "ticket_code": "TEST-001",
        "titolo": "Segnalazione appalti Ospedale Crotone",
        "descrizione": """
            Ho notato che l'azienda Edilizia Calabria SRL ha vinto numerosi
            appalti per la manutenzione dell'Ospedale San Giovanni di Dio di Crotone.
            L'amministratore si chiama Mario Rossi. Richiedo verifiche.
        """,
        "tipo": "segnalazione",
        "status": "aperto",
        "email": "test@example.com",
        "allegato_url": None,
        "created_at": "2026-05-04T10:00:00Z",
    }

    print("1. Elaborazione ticket di test...")
    result = processor.process_ticket(test_ticket)

    print(f"\n   Ticket: {result['ticket_code']}")
    print(f"   Titolo: {result['titolo']}")
    print(f"   Score: {result['investigability_score']:.2f}")
    print(f"   Alta priorita: {result['is_high_priority']}")

    print("\n   Entita estratte:")
    for e in result["entities_extracted"]:
        print(f"     - [{e['entity_type']}] {e['name']}")

    print("\n   Dettagli score:")
    for component, value in result["score_details"].get("components", {}).items():
        print(f"     - {component}: +{value:.2f}")

    # Test export contesto
    print("\n2. Export contesto per prompt:")
    print("-" * 60)
    # Nota: questo fallirebbe senza connessione Supabase reale
    # context = processor.export_kg_context_for_ticket("TEST-001")
    # print(context)

    print("\n=== Test completati ===")
