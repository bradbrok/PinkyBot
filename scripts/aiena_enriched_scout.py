#!/usr/bin/env python3
"""
AIena Enriched Scout — Wrapper per backlog scout con contesto KG e ANAC.

Questo script:
1. Legge l'item dal backlog (come il normale backlog_scout)
2. Estrae entita menzionate nel titolo/descrizione
3. Arricchisce il prompt con:
   - Connessioni note dal Knowledge Graph
   - Anomalie ANAC correlate
   - Contesto da segnalazioni precedenti
4. Invia il prompt arricchito ad AIena

Integrazione:
- Pensato per sostituire aiena_backlog_scout.py nel cron
- Oppure eseguibile in parallelo per casi specifici
- Non modifica l'originale backlog_scout.py

Uso:
    python3 aiena_enriched_scout.py              # Processa prossimo item rotazione
    python3 aiena_enriched_scout.py --slug X     # Processa item specifico
    python3 aiena_enriched_scout.py --ticket X   # Processa da segnalazione
"""
import argparse
import json
import os
import requests
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Aggiungi path per import locali
sys.path.insert(0, str(Path(__file__).parent))

# cron bootstrap: quando lo script gira da crontab, PINKY_SESSION_SECRET non è
# nell'ambiente → resolve_signing_secret() ritorna "" → richiesta senza auth → 401.
# Carichiamo il .env del daemon se PINKY_SESSION_SECRET è assente o vuoto.
_DAEMON_ENV = Path("/home/pinky/.pinkybot/.env")
if not os.environ.get("PINKY_SESSION_SECRET") and _DAEMON_ENV.exists():
    for _ln in _DAEMON_ENV.read_text().splitlines():
        _ln = _ln.strip()
        if _ln and "=" in _ln and not _ln.startswith("#"):
            _ek, _, _ev = _ln.partition("=")
            _k = _ek.strip()
            _v = _ev.strip().strip('"').strip("'")
            if not os.environ.get(_k):  # non sovrascrivere valori già impostati
                os.environ[_k] = _v

from aiena_secrets import _load_secrets
from pinky_daemon.auth import build_internal_auth_headers, resolve_signing_secret
from aiena_kg.aiena_kg import KnowledgeGraph
from aiena_anac.aiena_anomaly_detector import AnomalyDetector
from aiena_signals.entity_extractor import EntityExtractor

# ============================================================
# Configurazione
# ============================================================

AIENA_ENDPOINT = "http://localhost:8888/agents/aiena/message"
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_enriched_scout_state.json")

KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
ANAC_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_anac_cache.db")


# ============================================================
# Classe principale
# ============================================================

class EnrichedScout:
    """
    Scout arricchito con contesto da KG e ANAC.
    """

    def __init__(self):
        # Inizializza componenti (lazy, solo se DB esistono)
        self.kg = None
        self.anomaly_detector = None
        self.extractor = EntityExtractor()

        if KG_DB_PATH.exists():
            try:
                self.kg = KnowledgeGraph(KG_DB_PATH)
            except Exception as e:
                print(f"[EnrichedScout] KG init warning: {e}")

        if ANAC_DB_PATH.exists():
            try:
                self.anomaly_detector = AnomalyDetector()
            except Exception as e:
                print(f"[EnrichedScout] AnomalyDetector init warning: {e}")

    def load_state(self) -> dict:
        if STATE_FILE.exists():
            return json.loads(STATE_FILE.read_text())
        return {"last_index": -1, "processed_items": {}}

    def calculate_local_score(self, item: dict) -> int:
        """
        Calculate priority score for a backlog item.
        Higher score = higher priority for investigation.
        Mirrors update_admin_data.calculate_backlog_score() with freshness bonus.
        """
        from datetime import date, timedelta

        score = 0

        # Ticket submissions get priority (democratic journalism)
        if item.get("ticket_ref"):
            score += 50

        # Sources count: multiple fallbacks
        sources = item.get("sources_count", 0)
        if sources == 0:
            sources = len(item.get("sources", []))
        if sources == 0:
            sources = item.get("leads", 0) or 0
        # Fallback: research_notes (Fix 6 logic)
        if sources == 0:
            notes = item.get("research_notes", [])
            if len(notes) >= 3:
                sources = max(1, len(notes) // 2)

        if sources >= 5:
            score += 30
        elif sources >= 3:
            score += 15
        elif sources >= 2:
            score += 5
        elif sources == 0 and item.get("status") == "idea":
            score -= 10

        if item.get("status") == "ricerca":
            score += 10

        # Category multiplier
        category = (item.get("category") or "").lower()
        multiplier = 1.0
        if any(k in category for k in ["appalti", "corruzione", "fondi"]):
            multiplier = 1.5
        elif any(k in category for k in ["politica", "conflitti"]):
            multiplier = 1.4
        elif any(k in category for k in ["aziende", "criminalità", "organizzata", "ndrangheta", "mafia"]):
            multiplier = 1.3
        elif any(k in category for k in ["cronaca", "patrimoni", "immobili"]):
            multiplier = 1.2

        score = int(score * multiplier)

        # Age bonus (older items)
        added = item.get("added_date") or item.get("added") or item.get("created_at", "")
        if added:
            try:
                added_date = date.fromisoformat(added[:10])
                age_days = (date.today() - added_date).days
                if age_days > 60:
                    score += 20
                elif age_days > 30:
                    score += 10
                elif age_days > 14:
                    score += 5
                # FIX 3: Freshness bonus for recent items (weekend discoveries)
                elif age_days <= 2:
                    score += 20
            except (ValueError, TypeError):
                pass

        return max(0, score)

    def save_state(self, state: dict):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def get_next_item(self) -> tuple[dict | None, int]:
        """
        Ottiene il prossimo item dal backlog in rotazione.

        Returns:
            Tuple (item, index) o (None, -1)
        """
        try:
            data = json.loads(PIPELINE_JSON.read_text())
        except Exception as e:
            print(f"[EnrichedScout] Errore lettura pipeline.json: {e}")
            return None, -1

        backlog = data.get("leads", [])
        if not backlog:
            return None, -1

        state = self.load_state()
        last_idx = state.get("last_index", -1)
        current_idx = (last_idx + 1) % len(backlog)

        return backlog[current_idx], current_idx

    def get_item_by_slug(self, slug: str) -> dict | None:
        """Trova item per slug nel backlog o pipeline."""
        try:
            data = json.loads(PIPELINE_JSON.read_text())
        except Exception:
            return None

        for item in data.get("leads", []) + data.get("investigations", []):
            if item.get("slug") == slug:
                return item
        return None

    def extract_entities_from_item(self, item: dict) -> list[dict]:
        """Estrae entita da titolo e descrizione/notes di un item."""
        text_parts = [
            item.get("title", ""),
            item.get("description", ""),
        ]
        # Aggiungi research notes
        for note in item.get("research_notes", []):
            text_parts.append(note)

        combined = " ".join(filter(None, text_parts))
        entities = self.extractor.extract(combined, min_confidence=0.5)

        return [e.to_dict() for e in entities]

    def build_kg_context(self, entities: list[dict]) -> str:
        """
        Costruisce contesto dal Knowledge Graph per le entita date.
        """
        if not self.kg:
            return ""

        context_parts = []

        for entity in entities:
            if entity["entity_type"] not in ("AZIENDA", "ENTE_PUBBLICO", "PERSONA"):
                continue

            name = entity["name"]

            # Cerca nel KG
            results = self.kg.search_entity(name, limit=1)
            if not results:
                continue

            kg_entity = results[0]
            fuzzy_dist = kg_entity.get("_fuzzy_distance", 0)
            if fuzzy_dist > 3:
                continue

            # Export contesto
            export = self.kg.export_for_aiena(name, max_connections=10)
            context_parts.append(export)

        if not context_parts:
            return ""

        return (
            "\n\n" + "=" * 60 + "\n"
            "CONTESTO DAL KNOWLEDGE GRAPH\n"
            "(Entita e connessioni gia note ad AIena)\n"
            + "=" * 60 + "\n\n"
            + "\n\n".join(context_parts)
        )

    def build_anac_context(self, entities: list[dict]) -> str:
        """
        Costruisce contesto da anomalie ANAC per le entita date.
        """
        if not self.anomaly_detector:
            return ""

        context_parts = []

        for entity in entities:
            if entity["entity_type"] not in ("AZIENDA", "ENTE_PUBBLICO"):
                continue

            name = entity["name"]
            entity_type = "supplier" if entity["entity_type"] == "AZIENDA" else "buyer"

            anomalies = self.anomaly_detector.get_anomalies_for_entity(name, entity_type)
            if not anomalies:
                continue

            part = [f"ANOMALIE per {name}:"]
            for a in anomalies[:5]:
                if "cig" in a:
                    part.append(f"  - CIG {a['cig']}: {a.get('flag_reason') or a.get('description', 'N/A')}")
                    if "amount" in a:
                        part.append(f"    Importo: {a['amount']:,.0f} EUR")

            context_parts.append("\n".join(part))

        if not context_parts:
            return ""

        return (
            "\n\n" + "=" * 60 + "\n"
            "ANOMALIE ANAC CORRELATE\n"
            "(Gare con flag sospetto nel database ANAC)\n"
            + "=" * 60 + "\n\n"
            + "\n\n".join(context_parts)
        )

    def build_enriched_prompt(self, item: dict) -> str:
        """
        Costruisce il prompt arricchito per AIena.
        """
        now = datetime.now(ZoneInfo("Europe/Rome"))

        title = item.get("title", "")
        slug = item.get("slug", "")
        description = item.get("description", "")
        category = item.get("category", "")
        existing_notes = item.get("research_notes", [])
        notes_str = "\n".join(f"- {n}" for n in existing_notes) if existing_notes else "Nessuna ancora."

        # Ticket reference se presente
        ticket_ref = item.get("ticket_ref", "")
        ticket_block = ""
        if ticket_ref:
            ticket_block = f"\n**Origine:** Segnalazione cittadino {ticket_ref}\n"

        # Estrai entita
        entities = self.extract_entities_from_item(item)
        entities_str = ""
        if entities:
            entities_str = "\n**Entita identificate automaticamente:**\n"
            for e in entities[:10]:
                entities_str += f"  - [{e['entity_type']}] {e['name']}\n"

        # Contesto KG
        kg_context = self.build_kg_context(entities)

        # Contesto ANAC
        anac_context = self.build_anac_context(entities)

        # Fonti per categoria (come backlog_scout originale)
        source_map = {
            "Sanita": ["ANAC (dati.anticorruzione.it)", "CONSIP (consip.it)", "Corte dei Conti"],
            "PNRR": ["OpenCoesione (opencoesione.gov.it)", "OpenBDAP", "ItaliaDomai.gov.it"],
            "Patrimonio": ["Agenzia Entrate / Catasto", "Corte dei Conti", "ANAC"],
            "Corruzione": ["ANAC", "OCCRP Aleph", "OpenSanctions"],
            "Appalti pubblici": ["ANAC (CIG lookup)", "CONSIP", "Corte dei Conti"],
        }
        fonti = source_map.get(category, ["ANAC", "OpenData (dati.gov.it)", "Corte dei Conti"])
        fonti_str = "\n".join(f"  - {f}" for f in fonti)

        # Costruisci prompt
        prompt = f"""Backlog scout ARRICCHITO — {now.strftime('%A %d %b %Y')}.

Indaga questa storia dal backlog con CONTESTO AGGIUNTIVO dal Knowledge Graph e ANAC:

**Titolo:** {title}
**Categoria:** {category}
**Descrizione:** {description}
{ticket_block}{entities_str}
Note gia raccolte:
{notes_str}

OBIETTIVO: trova 2-3 lead NUOVI e concreti (fonti, dati aperti, documenti, nomi).

Fonti PRIORITARIE per questa categoria ({category}):
{fonti_str}
{kg_context}{anac_context}

ISTRUZIONI SPECIALI per questo prompt arricchito:
1. USA il contesto del Knowledge Graph per seguire connessioni gia note
2. USA le anomalie ANAC come punto di partenza per approfondire
3. Se trovi NUOVE entita/relazioni, aggiungile al KG usando il tool appropriato
4. Se trovi conferme a sospetti esistenti, INCREMENTA la priorita dell'item

Quando finisci:
1. Aggiungi i lead trovati a `research_notes` di questo item in /var/www/aiena.it/data/pipeline.json
2. Se trovi qualcosa di significativo, cambia `status` da 'idea' a 'ricerca'
3. Dopo aver aggiornato pipeline.json, esegui SEMPRE:
   `import subprocess; subprocess.run(['/home/pinky/.pinkybot/.venv/bin/python3',
   '/home/pinky/.pinkybot/scripts/aiena_admin_rebuild.py'], timeout=60)`
4. Nessuna notifica a Mirko — lavoro silenzioso (salvo RICHIESTA_MIRKO)

Tieni il lavoro compatto: 15-20 minuti max, poi aggiorna e stop.

---
ISTRUZIONE SIDECAR C12 — OBBLIGATORIA:
Al termine della ricerca, salva le entita trovate in formato strutturato:
Crea il file /home/pinky/.pinkybot/data/agents/aiena/kg_data_{slug}.json con questo schema:
{{
  "slug": "{slug}",
  "title": "{title}",
  "entities": [
    {{"name": "Nome Entita", "type": "PERSONA|AZIENDA|ENTE_PUBBLICO|ALTRO", "notes": "breve desc"}},
    ...
  ],
  "relations": [
    {{"from": "Nome A", "to": "Nome B", "type": "VINCE|AMMINISTRA|POSSIEDE|CORRELATO|PAGA|RICEVE", "desc": "contesto", "confidence": 0.8}},
    ...
  ]
}}
Tipi entita validi: PERSONA, AZIENDA, ENTE_PUBBLICO, INDIRIZZO, APPALTO, ALTRO
Tipi relazione validi: VINCE, AMMINISTRA, POSSIEDE, LAVORA_PER, COLLABORA, PAGA, RICEVE, CORRELATO
Solo entita rilevanti all'indagine (min 3 per ricerca). Ometti se non trovi nulla di nuovo."""

        return prompt

    def trigger_aiena(self, message: str, max_retries: int = 3) -> bool:
        """Invia messaggio ad AIena con retry e notifica Telegram su fallimento."""
        import time

        TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = "32405655"

        def _notify_failure(reason: str):
            """Notifica Mirko se AIena non risponde dopo tutti i retry."""
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": f"⚠️ Backlog scout fallito\nAIena non risponde dopo {max_retries} tentativi.\nMotivo: {reason}\nItem saltato — riproverà domani alle 10:30."
                    },
                    timeout=10
                )
            except Exception:
                pass  # best-effort

        # Resolve HMAC signing secret once (used for each attempt)
        _signing_secret = resolve_signing_secret()

        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                # Build per-request signed auth headers (timestamp varies)
                _auth_hdrs = build_internal_auth_headers(
                    _signing_secret,
                    agent_name="satoshi",
                    method="POST",
                    path="/agents/aiena/message",
                ) if _signing_secret else {}
                resp = requests.post(
                    AIENA_ENDPOINT,
                    json={"from_agent": "satoshi", "message": message},
                    headers={"Content-Type": "application/json", **_auth_hdrs},
                    timeout=120  # sessioni Claude possono impiegare tempo
                )
                if resp.status_code == 200:
                    print(f"[EnrichedScout] AIena triggered (tentativo {attempt}): {resp.status_code}")
                    return True
                else:
                    last_error = f"HTTP {resp.status_code}"
                    print(f"[EnrichedScout] Tentativo {attempt}/{max_retries} fallito: {last_error}")
            except Exception as e:
                last_error = str(e)
                print(f"[EnrichedScout] Tentativo {attempt}/{max_retries} errore: {last_error}")

            if attempt < max_retries:
                wait = 30 * attempt  # 30s, 60s
                print(f"[EnrichedScout] Retry in {wait}s...")
                time.sleep(wait)

        print(f"[EnrichedScout] Tutti i tentativi esauriti. Notifica Mirko.")
        _notify_failure(last_error)
        return False

    # ============================================================
    # Queue fallback — SQLite per job falliti
    # ============================================================

    QUEUE_DB = Path("/home/pinky/.pinkybot/data/aiena_scout_queue.db")

    def _queue_init(self):
        import sqlite3
        con = sqlite3.connect(str(self.QUEUE_DB))
        con.execute("""CREATE TABLE IF NOT EXISTS failed_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL,
            title TEXT,
            queued_at TEXT NOT NULL,
            retries INTEGER DEFAULT 0,
            last_error TEXT,
            done INTEGER DEFAULT 0,
            abandoned_at TEXT
        )""")
        con.commit()
        con.close()

    def _queue_push(self, item: dict, error: str):
        import sqlite3
        self._queue_init()
        slug = item.get("slug", "")
        title = item.get("title", "")
        now_ts = datetime.now(ZoneInfo("Europe/Rome")).isoformat()
        con = sqlite3.connect(str(self.QUEUE_DB))
        # Evita duplicati: aggiorna riga esistente non completata, altrimenti inserisce
        existing = con.execute(
            "SELECT id FROM failed_jobs WHERE slug=? AND done=0 ORDER BY id LIMIT 1", (slug,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE failed_jobs SET retries=0, last_error=?, queued_at=? WHERE id=?",
                (error, now_ts, existing[0])
            )
            print(f"[EnrichedScout] Job aggiornato in queue (reset retry): {slug}")
        else:
            con.execute(
                "INSERT INTO failed_jobs (slug, title, queued_at, last_error) VALUES (?,?,?,?)",
                (slug, title, now_ts, error)
            )
            print(f"[EnrichedScout] Job accodato per retry: {slug}")
        con.commit()
        con.close()

    def _send_telegram_notification(self, text: str):
        """Invia notifica Telegram a Mirko."""
        TELEGRAM_TOKEN = _load_secrets().get("TELEGRAM_BOT_TOKEN", "")
        TELEGRAM_CHAT_ID = "32405655"
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=10
            )
        except Exception:
            pass  # best-effort

    def retry_queue(self):
        """Riprova job falliti dalla queue. Chiamato da cron separato."""
        import sqlite3
        self._queue_init()
        con = sqlite3.connect(str(self.QUEUE_DB))
        jobs = con.execute(
            "SELECT id, slug, title, retries FROM failed_jobs WHERE done=0 AND retries < 3 ORDER BY queued_at"
        ).fetchall()
        con.close()

        if not jobs:
            print("[EnrichedScout] Queue vuota — nulla da ritentare")
        else:
            print(f"[EnrichedScout] Queue: {len(jobs)} job da ritentare")
            for job_id, slug, title, retries in jobs:
                item = self.get_item_by_slug(slug)
                if not item:
                    print(f"[EnrichedScout] Queue: slug {slug} non trovato — skip")
                    continue
                prompt = self.build_enriched_prompt(item)
                success = self.trigger_aiena(prompt, max_retries=1)
                con = sqlite3.connect(str(self.QUEUE_DB))
                if success:
                    con.execute("UPDATE failed_jobs SET done=1 WHERE id=?", (job_id,))
                    print(f"[EnrichedScout] Queue: {title} completato ✓")
                else:
                    con.execute("UPDATE failed_jobs SET retries=retries+1 WHERE id=?", (job_id,))
                    print(f"[EnrichedScout] Queue: {title} ancora fallito (tentativo {retries+1}/3)")
                con.commit()
                con.close()

        # Gestione job abbandonati (retries >= 3, done=0, non ancora marcati come abandoned)
        now = datetime.now(ZoneInfo("Europe/Rome"))
        con = sqlite3.connect(str(self.QUEUE_DB))
        abandoned = con.execute(
            "SELECT slug, title FROM failed_jobs WHERE done=0 AND retries>=3 AND abandoned_at IS NULL"
        ).fetchall()
        for slug, title in abandoned:
            con.execute(
                "UPDATE failed_jobs SET abandoned_at=? WHERE slug=? AND done=0 AND retries>=3",
                (now.isoformat(), slug)
            )
            self._send_telegram_notification(
                f"⚠️ Scout job abbandonato: {title}\nSlug: {slug}\nFallito 3+ volte, richiede intervento manuale."
            )
            print(f"[EnrichedScout] Job abbandonato e notificato: {slug}")
        con.commit()
        con.close()

    # ============================================================
    # Elaborazione singolo item
    # ============================================================

    def _process_single(self, item: dict, idx: int, dry_run: bool) -> bool:
        """
        Processa un singolo item del backlog.
        Returns True se il trigger AIena ha avuto successo.
        """
        now = datetime.now(ZoneInfo("Europe/Rome"))
        title = item.get("title", "?")
        print(f"[EnrichedScout] → Processo: {title}")

        # Estrai entita
        entities = self.extract_entities_from_item(item)
        print(f"[EnrichedScout]   Entita estratte: {len(entities)}")

        # Verifica KG
        if self.kg:
            known_count = sum(
                1 for e in entities
                if e["entity_type"] in ("AZIENDA", "ENTE_PUBBLICO")
                and self.kg.search_entity(e["name"], limit=1)
                and self.kg.search_entity(e["name"], limit=1)[0].get("_fuzzy_distance", 99) <= 3
            )
            print(f"[EnrichedScout]   Entita note nel KG: {known_count}/{len(entities)}")

        # Costruisci prompt
        prompt = self.build_enriched_prompt(item)
        print(f"[EnrichedScout]   Prompt: {len(prompt)} caratteri")

        if dry_run:
            print("\n" + "=" * 60)
            print(f"DRY RUN — {title}")
            print("=" * 60)
            print(prompt[:500] + "..." if len(prompt) > 500 else prompt)
            print("=" * 60)
            return True

        # Invia ad AIena
        success = self.trigger_aiena(prompt)

        if success and idx >= 0:
            state = self.load_state()
            state["last_index"] = idx
            state["last_run"] = now.isoformat()
            state["last_item"] = title
            self.save_state(state)
        elif not success:
            # Accoda per retry
            self._queue_push(item, "trigger_aiena fallito dopo tutti i retry")

        return success

    # ============================================================
    # Entry point principale
    # ============================================================

    def run(
        self,
        slug: str | None = None,
        ticket: str | None = None,
        dry_run: bool = False,
        n_parallel: int = 2,
    ):
        """
        Esegue lo scout arricchito.

        Args:
            slug: Processa item specifico per slug
            ticket: Processa item da ticket reference
            dry_run: Solo mostra il prompt, non inviare
            n_parallel: Numero di item da processare in rotazione (default 2)
        """
        import time as _time

        now = datetime.now(ZoneInfo("Europe/Rome"))
        dow = now.weekday()

        # Weekend check (solo per rotazione automatica)
        if dow in (5, 6) and not slug and not ticket:
            print("[EnrichedScout] Weekend — skip (ci pensa aiena_weekly_trigger)")
            return
        if dow in (5, 6) and (slug or ticket):
            print(f"[EnrichedScout] Weekend ma slug/ticket forzato — procedo comunque")

        # --- Caso 1: slug specifico ---
        if slug:
            item = self.get_item_by_slug(slug)
            if not item:
                print(f"[EnrichedScout] Item non trovato: {slug}")
                return
            self._process_single(item, idx=-1, dry_run=dry_run)
            print("[EnrichedScout] Done")
            return

        # --- Caso 2: ticket specifico ---
        if ticket:
            try:
                data = json.loads(PIPELINE_JSON.read_text())
                item = next((i for i in data.get("leads", []) if i.get("ticket_ref") == ticket), None)
            except Exception:
                item = None
            if not item:
                print(f"[EnrichedScout] Ticket non trovato nel backlog: {ticket}")
                return
            self._process_single(item, idx=-1, dry_run=dry_run)
            print("[EnrichedScout] Done")
            return

        # --- Caso 3: rotazione automatica N item con selezione score-aware ---
        try:
            data = json.loads(PIPELINE_JSON.read_text())
        except Exception as e:
            print(f"[EnrichedScout] Errore lettura pipeline.json: {e}")
            return

        backlog = data.get("leads", [])
        if not backlog:
            print("[EnrichedScout] Backlog vuoto — nulla da fare")
            return

        state = self.load_state()
        processed_items = state.get("processed_items", {})

        # Calcola score per ogni item e filtra quelli processati nelle ultime 48h
        from datetime import timedelta
        cutoff = now - timedelta(hours=48)

        scored_items = []
        for idx, item in enumerate(backlog):
            slug = item.get("slug", f"item-{idx}")
            score = self.calculate_local_score(item)

            # Check if processed in last 48h
            last_processed_str = processed_items.get(slug)
            recently_processed = False
            if last_processed_str:
                try:
                    last_processed = datetime.fromisoformat(last_processed_str)
                    if last_processed.tzinfo is None:
                        last_processed = last_processed.replace(tzinfo=ZoneInfo("Europe/Rome"))
                    if last_processed > cutoff:
                        recently_processed = True
                except (ValueError, TypeError):
                    pass

            scored_items.append({
                "idx": idx,
                "item": item,
                "slug": slug,
                "score": score,
                "recently_processed": recently_processed
            })

        # Filter out recently processed, unless ALL are recently processed (reset)
        eligible = [s for s in scored_items if not s["recently_processed"]]
        if not eligible:
            print(f"[EnrichedScout] Tutti gli item processati nelle ultime 48h — reset filtro")
            eligible = scored_items

        # Sort by score DESC and take top N
        eligible.sort(key=lambda x: x["score"], reverse=True)
        to_process = eligible[:n_parallel]

        print(f"[EnrichedScout] Score-based selection: {len(to_process)} item selezionati")
        for sp in to_process:
            print(f"  - {sp['slug']}: score={sp['score']}")

        processed = 0
        for i, sp in enumerate(to_process):
            item = sp["item"]
            idx = sp["idx"]
            slug = sp["slug"]
            print(f"[EnrichedScout] Sessione {i+1}/{n_parallel} (idx {idx}, score {sp['score']}):")
            success = self._process_single(item, idx=idx, dry_run=dry_run)
            if success:
                # Mark as processed in state
                state["last_index"] = idx
                state.setdefault("processed_items", {})[slug] = now.isoformat()
                self.save_state(state)
                processed += 1
            # Piccola pausa tra sessioni per non sovraccaricare AIena
            if i < len(to_process) - 1 and not dry_run:
                print(f"[EnrichedScout] Pausa 5s tra sessioni...")
                _time.sleep(5)

        print(f"[EnrichedScout] Done — {processed}/{n_parallel} item processati")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="AIena Enriched Scout — backlog scout con contesto KG/ANAC"
    )
    parser.add_argument(
        "--slug",
        help="Processa item specifico per slug"
    )
    parser.add_argument(
        "--ticket",
        help="Processa item da ticket reference"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra solo il prompt, non inviare"
    )
    parser.add_argument(
        "--retry-queue",
        action="store_true",
        help="Riprova job falliti dalla queue SQLite"
    )
    parser.add_argument(
        "--n-parallel",
        type=int,
        default=2,
        help="Numero di item da processare in rotazione (default 2)"
    )
    args = parser.parse_args()

    scout = EnrichedScout()

    if args.retry_queue:
        scout.retry_queue()
    else:
        scout.run(
            slug=args.slug,
            ticket=args.ticket,
            dry_run=args.dry_run,
            n_parallel=args.n_parallel,
        )


if __name__ == "__main__":
    main()
