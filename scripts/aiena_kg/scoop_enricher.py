#!/usr/bin/env python3
"""
AIena Scoop Enricher (C7) — arricchisce le anomalie ANAC con dati esterni.

Pipeline:
    1. Recupera anomalie da AnomalyDetector (score >= min_score)
    2. Per ogni anomalia con supplier_id valido:
       a) OpenCorporates  -> azienda + amministratori (REGISTRO_IMPRESE)
       b) OpenSanctions   -> match locale CSV bulk (PEP / sanzioni)
       c) OpenCoesione    -> finanziamenti UE per CF beneficiario
    3. Calcola scoop_score combinato e aggiorna il Knowledge Graph

Uso:
    python3 scoop_enricher.py
    python3 scoop_enricher.py --min-score 0.3
    python3 scoop_enricher.py --dry-run
    python3 scoop_enricher.py --piva 00492340583

Decisioni architetturali:
- Cache HTTP in tabella SQLite enrichment_cache (TTL 30 giorni default)
- OpenSanctions CSV caricato lazy in memoria solo al primo match
- Rate limiting per ogni API (OC: 1 req / 2s, OCoesione: 1 req / 1s)
- Backoff esponenziale su HTTP 429
- Mai crashare: ogni step e' wrapped in try/except con fallback graceful
- Tutto idempotente: rieseguire e' sicuro (upsert)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ============================================================
# Path setup (supporta esecuzione diretta + import package)
# ============================================================

SCRIPTS_DIR = Path(__file__).resolve().parent.parent  # -> scripts/
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from aiena_kg.aiena_kg import KnowledgeGraph, normalize_cf, now_iso  # noqa: E402
from aiena_anac.aiena_anomaly_detector import AnomalyDetector  # noqa: E402

# ============================================================
# Costanti
# ============================================================

KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
OPENSANCTIONS_CSV = Path("/home/pinky/.pinkybot/data/opensanctions_targets.csv")

OPENCORPORATES_URL = "https://api.opencorporates.com/v0.4/companies/it/{piva}"
OPENSANCTIONS_URL = (
    "https://data.opensanctions.org/datasets/latest/peps/targets.simple.csv"
)
OPENCOESIONE_URL = (
    "https://opencoesione.gov.it/it/api/soggetti/?codice_fiscale={piva}&format=json"
)

USER_AGENT = "AIenaBot/1.0; +https://aiena.it/bot"

# Rate limits (secondi minimi tra richieste consecutive per host)
RATE_LIMITS = {
    "opencorporates": 2.0,
    "opencoesione": 1.0,
    "opensanctions": 0.0,  # download bulk una tantum
}

# TTL cache HTTP (giorni)
CACHE_TTL_DAYS = 30
OPENSANCTIONS_REFRESH_DAYS = 7

# Thresholds
NEW_COMPANY_DAYS = 365  # azienda neonata se fondata <365gg prima della prima vincita
MAX_HTTP_RETRIES = 3
HTTP_TIMEOUT = 30  # secondi

# Score weights
SCORE_WEIGHT_ANAC_STRONG = 0.30
SCORE_WEIGHT_NEW_COMPANY = 0.25
SCORE_WEIGHT_SANCTIONS = 0.30
SCORE_WEIGHT_COESIONE = 0.15
ANAC_STRONG_THRESHOLD = 0.6

logger = logging.getLogger("scoop_enricher")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
logger.setLevel(logging.INFO)


# ============================================================
# Helpers
# ============================================================


def normalize_person_name(name: str) -> str:
    """
    Normalizza nome persona per match cross-source.
    - Rimuove diacritici (NFD + filtro Mn)
    - Uppercase
    - Strip e collassa whitespace
    """
    if not name:
        return ""
    n = unicodedata.normalize("NFD", name)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    n = n.upper().strip()
    # Collassa whitespace
    return " ".join(n.split())


def parse_iso_date(value: str | None) -> datetime | None:
    """Parsa data ISO (YYYY-MM-DD o ISO8601) restituendo datetime UTC-aware."""
    if not value:
        return None
    try:
        # Provo formato compatto (gestisce solo YYYY-MM-DD e ISO con T)
        s = str(value).strip()
        if "T" in s or "+" in s or "Z" in s:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        # Solo data
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def piva_for_opencorporates(piva: str | None) -> str | None:
    """OpenCorporates richiede 11 cifre senza prefix IT."""
    norm = normalize_cf(piva)
    if not norm:
        return None
    # Strip eventuale prefisso IT
    if norm.startswith("IT"):
        norm = norm[2:]
    # P.IVA italiana = 11 cifre
    if len(norm) == 11 and norm.isdigit():
        return norm
    return None


# ============================================================
# Classe principale
# ============================================================


class ScoopEnricher:
    """
    Arricchisce anomalie ANAC con fonti esterne e calcola scoop_score.

    Esempio:
        e = ScoopEnricher()
        results = e.run(min_score=0.5)
        for r in results:
            print(r["scoop_score"], r["flags"])
    """

    def __init__(
        self,
        kg_db_path: Path | str | None = None,
        opensanctions_csv: Path | str | None = None,
    ):
        self.kg = KnowledgeGraph(Path(kg_db_path) if kg_db_path else KG_DB_PATH)
        self.detector = AnomalyDetector()
        self.opensanctions_csv = (
            Path(opensanctions_csv) if opensanctions_csv else OPENSANCTIONS_CSV
        )

        # Stato runtime
        self._os_cache: list[dict] | None = None  # OpenSanctions parsato (lazy)
        self._last_request_at: dict[str, float] = {}

        # Inizializza tabella cache HTTP
        self._init_cache_table()

    # --------------------------------------------------------
    # Cache HTTP (SQLite)
    # --------------------------------------------------------

    def _init_cache_table(self) -> None:
        """Crea enrichment_cache se non esiste."""
        with sqlite3.connect(str(self.kg.db_path), timeout=30) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS enrichment_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT UNIQUE NOT NULL,
                    payload TEXT,
                    fetched_at TEXT NOT NULL,
                    ttl_days INTEGER DEFAULT 30
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_enrichcache_key "
                "ON enrichment_cache(cache_key)"
            )
            conn.commit()

    def _cache_get(self, cache_key: str) -> Any | None:
        """Ritorna payload se presente e non scaduto, altrimenti None."""
        try:
            with sqlite3.connect(str(self.kg.db_path), timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT payload, fetched_at, ttl_days FROM enrichment_cache "
                    "WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except sqlite3.Error as e:
            logger.warning(f"cache_get error: {e}")
            return None

        if not row:
            return None

        fetched = parse_iso_date(row["fetched_at"])
        if not fetched:
            return None

        ttl = int(row["ttl_days"] or CACHE_TTL_DAYS)
        if datetime.now(timezone.utc) - fetched > timedelta(days=ttl):
            return None

        try:
            return json.loads(row["payload"]) if row["payload"] else None
        except json.JSONDecodeError:
            return None

    def _cache_set(
        self, cache_key: str, payload: Any, ttl_days: int = CACHE_TTL_DAYS
    ) -> None:
        """Salva payload in cache (upsert)."""
        try:
            with sqlite3.connect(str(self.kg.db_path), timeout=30) as conn:
                conn.execute(
                    """
                    INSERT INTO enrichment_cache
                        (cache_key, payload, fetched_at, ttl_days)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        payload = excluded.payload,
                        fetched_at = excluded.fetched_at,
                        ttl_days = excluded.ttl_days
                    """,
                    (
                        cache_key,
                        json.dumps(payload, ensure_ascii=False),
                        now_iso(),
                        ttl_days,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"cache_set error: {e}")

    # --------------------------------------------------------
    # HTTP con rate limiting + retry/backoff
    # --------------------------------------------------------

    def _rate_limit(self, host_key: str) -> None:
        """Sleep se necessario per rispettare il rate limit per host."""
        min_interval = RATE_LIMITS.get(host_key, 0.0)
        if min_interval <= 0:
            return
        last = self._last_request_at.get(host_key, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_at[host_key] = time.monotonic()

    def _http_get(
        self,
        url: str,
        host_key: str,
        accept: str = "application/json",
    ) -> tuple[int, bytes | None]:
        """
        GET con retry/backoff. Ritorna (status_code, body_bytes).
        Status 0 indica errore di rete non recuperabile.
        """
        backoff = 2.0
        last_status = 0

        for attempt in range(1, MAX_HTTP_RETRIES + 1):
            self._rate_limit(host_key)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": accept,
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                last_status = e.code
                if e.code == 404:
                    return 404, None
                if e.code == 429 or 500 <= e.code < 600:
                    logger.warning(
                        f"HTTP {e.code} su {url} (tentativo {attempt}/{MAX_HTTP_RETRIES})"
                    )
                    if attempt < MAX_HTTP_RETRIES:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    return e.code, None
                # 4xx non recuperabile
                logger.warning(f"HTTP {e.code} su {url}: skip")
                return e.code, None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                logger.warning(
                    f"Network error su {url}: {e} (tentativo {attempt}/{MAX_HTTP_RETRIES})"
                )
                if attempt < MAX_HTTP_RETRIES:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                return 0, None

        return last_status, None

    # --------------------------------------------------------
    # Step 1: OpenCorporates
    # --------------------------------------------------------

    def _fetch_opencorporates(self, piva: str) -> dict | None:
        """
        Recupera dati azienda da OpenCorporates IT.
        Ritorna il dict 'company' o None se non disponibile.
        """
        oc_piva = piva_for_opencorporates(piva)
        if not oc_piva:
            return None

        cache_key = f"opencorporates:{oc_piva}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached if isinstance(cached, dict) else None

        url = OPENCORPORATES_URL.format(piva=oc_piva)
        status, body = self._http_get(url, host_key="opencorporates")

        if status == 404 or not body:
            self._cache_set(cache_key, {})  # cache negativa
            return None

        if status != 200:
            return None

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"OpenCorporates JSON parse error per {oc_piva}: {e}")
            return None

        company = (data or {}).get("results", {}).get("company")
        if not isinstance(company, dict):
            self._cache_set(cache_key, {})
            return None

        self._cache_set(cache_key, company)
        return company

    @staticmethod
    def _extract_officers(company: dict) -> list[dict]:
        """Estrae lista officers dal payload OC."""
        officers_raw = company.get("officers") or []
        out = []
        for entry in officers_raw:
            # OC format: {"officer": {...}}
            if isinstance(entry, dict) and "officer" in entry:
                off = entry["officer"]
            elif isinstance(entry, dict):
                off = entry
            else:
                continue
            name = off.get("name")
            if not name:
                continue
            out.append(
                {
                    "name": str(name),
                    "position": off.get("position"),
                    "start_date": off.get("start_date"),
                    "end_date": off.get("end_date"),
                }
            )
        return out

    # --------------------------------------------------------
    # Step 2: OpenSanctions (CSV bulk + match locale)
    # --------------------------------------------------------

    def _ensure_opensanctions_csv(self) -> bool:
        """
        Scarica/aggiorna il CSV OpenSanctions se mancante o vecchio (>7gg).
        Ritorna True se il file e' disponibile.
        """
        # Check freshness
        if self.opensanctions_csv.exists():
            mtime = datetime.fromtimestamp(
                self.opensanctions_csv.stat().st_mtime, tz=timezone.utc
            )
            age_days = (datetime.now(timezone.utc) - mtime).days
            if age_days < OPENSANCTIONS_REFRESH_DAYS:
                return True

        logger.info(
            f"Download OpenSanctions CSV in {self.opensanctions_csv} ..."
        )
        self.opensanctions_csv.parent.mkdir(parents=True, exist_ok=True)
        status, body = self._http_get(
            OPENSANCTIONS_URL, host_key="opensanctions", accept="text/csv"
        )
        if status != 200 or not body:
            logger.warning(
                f"Download OpenSanctions fallito (status={status}); "
                f"continuo senza match sanzioni"
            )
            # Se esiste comunque un file vecchio, usalo
            return self.opensanctions_csv.exists()

        try:
            self.opensanctions_csv.write_bytes(body)
            logger.info(
                f"OpenSanctions CSV aggiornato ({len(body) / (1024 * 1024):.1f} MB)"
            )
            return True
        except OSError as e:
            logger.warning(f"Errore scrittura OpenSanctions CSV: {e}")
            return False

    def _load_opensanctions(self) -> list[dict]:
        """
        Carica il CSV OpenSanctions in memoria (lazy).
        Ritorna lista di dict normalizzati con 'name_norm'.
        Lista vuota se file non disponibile.
        """
        if self._os_cache is not None:
            return self._os_cache

        if not self._ensure_opensanctions_csv():
            self._os_cache = []
            return self._os_cache

        import re as _re

        records: list[dict] = []
        # Pattern per match country code 'it' nel campo countries (es: "eu;it" o "it;eu")
        _it_pattern = _re.compile(r'(^|;)it(;|$)', _re.IGNORECASE)

        try:
            with open(
                self.opensanctions_csv, encoding="utf-8", errors="replace", newline=""
            ) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Il campo si chiama 'name' nel formato simple CSV (non 'caption')
                    name_raw = (row.get("name") or row.get("caption") or "").strip()
                    if not name_raw:
                        continue
                    countries = row.get("countries", "")
                    # Carica tutti i record (non solo IT) — match globale per sanzioni internazionali
                    records.append(
                        {
                            "id": row.get("id"),
                            "caption": name_raw,
                            "schema": row.get("schema"),
                            "countries": countries,
                            "is_italian": bool(_it_pattern.search(countries)),
                            "datasets": row.get("dataset") or row.get("datasets", ""),
                            "topics": row.get("topics", ""),
                            "name_norm": normalize_person_name(name_raw),
                        }
                    )
        except (OSError, csv.Error) as e:
            logger.warning(f"Errore parsing OpenSanctions CSV: {e}")
            self._os_cache = []
            return self._os_cache

        it_count = sum(1 for r in records if r["is_italian"])
        logger.info(f"OpenSanctions caricato: {len(records)} record totali, {it_count} italiani")
        self._os_cache = records
        return self._os_cache

    def _match_opensanctions(self, name: str) -> list[dict]:
        """
        Cerca match esatto (su name normalizzato) nel CSV OpenSanctions.
        Ritorna lista di hit (ognuno e' un dict del CSV).
        """
        if not name:
            return []
        norm = normalize_person_name(name)
        if not norm:
            return []

        records = self._load_opensanctions()
        if not records:
            return []

        return [r for r in records if r["name_norm"] == norm]

    # --------------------------------------------------------
    # Step 3: OpenCoesione
    # --------------------------------------------------------

    def _fetch_opencoesione(self, piva: str) -> list[dict]:
        """
        Recupera progetti OpenCoesione per CF/P.IVA beneficiario.
        Ritorna lista di progetti (anche vuota).
        """
        norm = normalize_cf(piva)
        if not norm:
            return []

        cache_key = f"opencoesione:{norm}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached if isinstance(cached, list) else []

        url = OPENCOESIONE_URL.format(piva=norm)
        status, body = self._http_get(url, host_key="opencoesione")

        if status == 404 or not body:
            self._cache_set(cache_key, [])
            return []
        if status != 200:
            # 5xx / network: skip silenzioso, NON cacheare il fallimento
            return []

        try:
            data = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"OpenCoesione JSON parse error per {norm}: {e}")
            return []

        # Schema OpenCoesione soggetti API:
        # {"count": N, "results": [{"denominazione":..., "costo_pubblico":..., "temi":..., "ruoli":...}]}
        if isinstance(data, dict):
            projects = (
                data.get("results")
                or data.get("data")
                or data.get("soggetti")
                or []
            )
        elif isinstance(data, list):
            projects = data
        else:
            projects = []

        if not isinstance(projects, list):
            projects = []

        # Filtra solo risultati con costo pubblico > 0 (fondi effettivi)
        funded = [p for p in projects if p.get("costo_pubblico", 0)]

        self._cache_set(cache_key, funded or projects)
        return funded or projects

    # --------------------------------------------------------
    # Step 4: Scoop score
    # --------------------------------------------------------

    @staticmethod
    def _compute_scoop_score(
        anomaly: dict,
        is_new_company: bool,
        os_hits: list,
        oc_funds: list,
    ) -> float:
        """Combina i segnali in un singolo score 0-1."""
        score = 0.0
        if anomaly.get("score", 0) > ANAC_STRONG_THRESHOLD:
            score += SCORE_WEIGHT_ANAC_STRONG
        if is_new_company:
            score += SCORE_WEIGHT_NEW_COMPANY
        if os_hits:
            score += SCORE_WEIGHT_SANCTIONS
        if oc_funds:
            score += SCORE_WEIGHT_COESIONE
        return min(1.0, round(score, 4))

    # --------------------------------------------------------
    # Persistenza KG
    # --------------------------------------------------------

    def _flag_anomaly_in_kg(
        self,
        entity_id: int | None,
        anomaly_type: str,
        score: float,
        description: str,
        data: dict | None = None,
    ) -> None:
        """Inserisce una riga in anomaly_flags (idempotente best-effort)."""
        try:
            with sqlite3.connect(str(self.kg.db_path), timeout=30) as conn:
                conn.execute(
                    """
                    INSERT INTO anomaly_flags
                        (entity_id, cig, anomaly_type, anomaly_score,
                         description, data_json, detected_at)
                    VALUES (?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        entity_id,
                        anomaly_type,
                        float(score),
                        description,
                        json.dumps(data or {}, ensure_ascii=False),
                        now_iso(),
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning(f"flag_anomaly_in_kg error ({anomaly_type}): {e}")

    # --------------------------------------------------------
    # Enrichment per singola anomalia
    # --------------------------------------------------------

    def enrich_anomaly(self, anomaly: dict, dry_run: bool = False) -> dict:
        """
        Arricchisce una singola anomalia. Mai lancia eccezioni:
        in caso di errore parziale, popola comunque il dict result.
        """
        data = anomaly.get("data", {}) or {}
        supplier_name = (data.get("supplier_name") or "").strip()
        supplier_id = (data.get("supplier_id") or "").strip()
        buyer_name = (data.get("buyer_name") or "").strip()

        result: dict[str, Any] = {
            "anomaly": anomaly,
            "supplier_piva": supplier_id or None,
            "oc_data": None,
            "officers": [],
            "opensanctions_hits": [],
            "opencoesione_funds": [],
            "flags": [],
            "scoop_score": 0.0,
            "kg_updated": False,
            "enriched_at": now_iso(),
        }

        if not supplier_id or not supplier_name:
            return result

        # ---- Step 1: OpenCorporates ----
        is_new_company = False
        try:
            company = self._fetch_opencorporates(supplier_id)
        except Exception as e:  # difensivo
            logger.warning(f"OpenCorporates exception per {supplier_id}: {e}")
            company = None

        officers: list[dict] = []
        if company:
            result["oc_data"] = {
                "name": company.get("name"),
                "incorporation_date": company.get("incorporation_date"),
                "company_status": company.get("company_status"),
            }
            officers = self._extract_officers(company)
            result["officers"] = [o["name"] for o in officers]

            # Flag azienda neonata
            inc_date = parse_iso_date(company.get("incorporation_date"))
            if inc_date:
                # Riferimento temporale: prima vincita stimata = oggi se non disponibile
                # Il dato "prima vittoria" non e' in anomaly[data]; uso current time
                # come approssimazione conservativa.
                age_days = (datetime.now(timezone.utc) - inc_date).days
                if 0 <= age_days < NEW_COMPANY_DAYS:
                    is_new_company = True
                    result["flags"].append("AZIENDA_NEONATA")

        # ---- Step 2: OpenSanctions match per ogni officer ----
        all_os_hits: list[dict] = []
        for officer in officers:
            try:
                hits = self._match_opensanctions(officer["name"])
            except Exception as e:
                logger.warning(f"OpenSanctions match error per {officer['name']}: {e}")
                hits = []
            for h in hits:
                all_os_hits.append(
                    {
                        "officer_name": officer["name"],
                        "officer_position": officer.get("position"),
                        "os_id": h.get("id"),
                        "os_caption": h.get("caption"),
                        "os_schema": h.get("schema"),
                        "os_datasets": h.get("datasets"),
                        "os_countries": h.get("countries"),
                    }
                )
        result["opensanctions_hits"] = all_os_hits
        if all_os_hits:
            result["flags"].append("ADMIN_SANCTIONED")

        # ---- Step 3: OpenCoesione ----
        try:
            funds = self._fetch_opencoesione(supplier_id)
        except Exception as e:
            logger.warning(f"OpenCoesione exception per {supplier_id}: {e}")
            funds = []
        result["opencoesione_funds"] = funds or []
        if funds:
            result["flags"].append("DOPPIO_FINANZIAMENTO")

        # ---- Step 4: Scoop score ----
        result["scoop_score"] = self._compute_scoop_score(
            anomaly, is_new_company, all_os_hits, funds or []
        )

        # ---- Step 5: Aggiorna KG ----
        if not dry_run:
            try:
                self._persist_to_kg(
                    supplier_name=supplier_name,
                    supplier_piva=supplier_id,
                    buyer_name=buyer_name,
                    company=company,
                    officers=officers,
                    os_hits=all_os_hits,
                    funds=funds or [],
                    scoop_score=result["scoop_score"],
                    flags=result["flags"],
                )
                result["kg_updated"] = True
            except Exception as e:
                logger.warning(f"KG persist error per {supplier_name}: {e}")
                result["kg_updated"] = False

        return result

    def _persist_to_kg(
        self,
        supplier_name: str,
        supplier_piva: str,
        buyer_name: str,
        company: dict | None,
        officers: list[dict],
        os_hits: list[dict],
        funds: list[dict],
        scoop_score: float,
        flags: list[str],
    ) -> None:
        """Scrive nel KG tutto cio' che abbiamo arricchito."""
        # Upsert azienda
        company_meta = {}
        if company:
            company_meta = {
                "incorporation_date": company.get("incorporation_date"),
                "company_status": company.get("company_status"),
            }
        supplier_id = self.kg.add_entity(
            name=supplier_name,
            entity_type="AZIENDA",
            metadata=company_meta or None,
            partita_iva=supplier_piva,
            confidence=0.95,
        )
        if company:
            self.kg.add_entity_source(
                entity_id=supplier_id,
                source_type="REGISTRO_IMPRESE",
                source_name="OpenCorporates IT",
                info_type="ESISTENZA",
                info_value=json.dumps(company_meta, ensure_ascii=False),
                source_url=f"https://opencorporates.com/companies/it/"
                f"{piva_for_opencorporates(supplier_piva) or supplier_piva}",
                confidence=0.95,
            )

        # Officers + relazione AMMINISTRA
        for off in officers:
            try:
                pid = self.kg.add_entity(
                    name=off["name"],
                    entity_type="PERSONA",
                    metadata={
                        "position": off.get("position"),
                        "start_date": off.get("start_date"),
                        "end_date": off.get("end_date"),
                    },
                    confidence=0.85,
                )
                self.kg.add_entity_source(
                    entity_id=pid,
                    source_type="REGISTRO_IMPRESE",
                    source_name="OpenCorporates IT (officers)",
                    info_type="RUOLO",
                    info_value=off.get("position") or "",
                    confidence=0.85,
                )
                self.kg.add_relation(
                    entity1_name=off["name"],
                    entity2_name=supplier_name,
                    relation_type="AMMINISTRA",
                    source_type="REGISTRO_IMPRESE",
                    description=off.get("position") or None,
                    source_date=off.get("start_date"),
                    confidence=0.9,
                    entity1_type="PERSONA",
                    entity2_type="AZIENDA",
                )
            except Exception as e:
                logger.warning(f"KG officer error {off.get('name')}: {e}")

        # OpenSanctions: flag PERSONA come sospetta + anomaly_flag
        for hit in os_hits:
            try:
                ent = self.kg.get_entity_by_name(hit["officer_name"], "PERSONA")
                if ent:
                    self.kg.flag_suspicious(ent["id"], suspicious=True)
                    self.kg.add_entity_source(
                        entity_id=ent["id"],
                        source_type="OPENSANCTIONS",
                        source_name="OpenSanctions targets.simple",
                        info_type="ALTRO",
                        info_value=f"{hit.get('os_schema')}: {hit.get('os_caption')}",
                        source_url=f"https://www.opensanctions.org/entities/{hit.get('os_id')}/",
                        confidence=0.9,
                    )
                    self._flag_anomaly_in_kg(
                        entity_id=ent["id"],
                        anomaly_type="OPENSANCTIONS_HIT",
                        score=0.9,
                        description=(
                            f"Match OpenSanctions per {hit['officer_name']} "
                            f"(schema={hit.get('os_schema')}, "
                            f"datasets={hit.get('os_datasets')})"
                        ),
                        data=hit,
                    )
            except Exception as e:
                logger.warning(f"KG sanctions flag error: {e}")

        # OpenCoesione: relazione RICEVE da ENTE (best-effort)
        for proj in funds[:50]:  # cap per sanity
            if not isinstance(proj, dict):
                continue
            ente = (
                proj.get("denominazione_programma")
                or proj.get("ente_finanziatore")
                or proj.get("programma")
                or "Programmi UE Coesione"
            )
            importo = (
                proj.get("finanziamento_totale_pubblico")
                or proj.get("finanziamento_totale")
                or proj.get("importo")
            )
            try:
                self.kg.add_entity(
                    name=str(ente),
                    entity_type="ENTE_PUBBLICO",
                    metadata={"fonte": "OpenCoesione"},
                    confidence=0.7,
                )
                self.kg.add_relation(
                    entity1_name=str(ente),
                    entity2_name=supplier_name,
                    relation_type="PAGA",
                    source_type="ALTRO",
                    description=f"OpenCoesione: importo={importo}",
                    source_url="https://opencoesione.gov.it/",
                    confidence=0.7,
                    entity1_type="ENTE_PUBBLICO",
                    entity2_type="AZIENDA",
                )
            except Exception as e:
                logger.warning(f"KG coesione relation error: {e}")

        # Flags compositi sull'azienda
        if "AZIENDA_NEONATA" in flags:
            self._flag_anomaly_in_kg(
                entity_id=supplier_id,
                anomaly_type="AZIENDA_NEONATA",
                score=scoop_score,
                description=(
                    f"{supplier_name} fondata di recente "
                    f"(incorporation_date={company.get('incorporation_date') if company else 'N/A'})"
                ),
                data={"piva": supplier_piva, "scoop_score": scoop_score},
            )
        if "DOPPIO_FINANZIAMENTO" in flags:
            self._flag_anomaly_in_kg(
                entity_id=supplier_id,
                anomaly_type="DOPPIO_FINANZIAMENTO",
                score=scoop_score,
                description=(
                    f"{supplier_name} riceve fondi sia da ANAC ({buyer_name}) "
                    f"che da OpenCoesione ({len(funds)} progetti)"
                ),
                data={
                    "piva": supplier_piva,
                    "scoop_score": scoop_score,
                    "n_projects": len(funds),
                },
            )

        # Buyer come ENTE_PUBBLICO + relazione VINCE
        if buyer_name:
            try:
                self.kg.add_entity(
                    name=buyer_name,
                    entity_type="ENTE_PUBBLICO",
                    confidence=0.8,
                )
                self.kg.add_relation(
                    entity1_name=supplier_name,
                    entity2_name=buyer_name,
                    relation_type="VINCE",
                    source_type="ANAC",
                    confidence=0.9,
                    entity1_type="AZIENDA",
                    entity2_type="ENTE_PUBBLICO",
                )
            except Exception as e:
                logger.warning(f"KG buyer relation error: {e}")

    # --------------------------------------------------------
    # Run pipeline completa
    # --------------------------------------------------------

    def run(self, min_score: float = 0.5, dry_run: bool = False) -> list[dict]:
        """
        Arricchisce tutte le anomalie con score >= min_score.
        Ritorna lista di enrichment result.
        """
        anomalies = self.detector.detect_all(min_score=min_score)
        logger.info(f"Anomalie da arricchire (score>={min_score}): {len(anomalies)}")

        results: list[dict] = []
        for i, anomaly in enumerate(anomalies, start=1):
            data = anomaly.get("data", {}) or {}
            if not data.get("supplier_id"):
                continue
            logger.info(
                f"[{i}/{len(anomalies)}] enrich {data.get('supplier_name')} "
                f"(P.IVA {data.get('supplier_id')}) "
                f"anomaly={anomaly.get('anomaly_type')} score={anomaly.get('score'):.2f}"
            )
            try:
                res = self.enrich_anomaly(anomaly, dry_run=dry_run)
            except Exception as e:
                logger.exception(f"enrich_anomaly fallita: {e}")
                continue
            results.append(res)

        return results

    def run_for_piva(self, piva: str, dry_run: bool = False) -> dict:
        """
        Test su singola P.IVA senza richiedere anomalie pre-esistenti.
        Costruisce un'anomalia minimal sintetica.
        """
        synthetic = {
            "cig": None,
            "anomaly_type": "MANUAL_TEST",
            "score": 0.0,
            "description": f"Manual test per P.IVA {piva}",
            "data": {
                "buyer_id": None,
                "buyer_name": "",
                "supplier_id": piva,
                "supplier_name": f"P.IVA {piva}",
            },
        }
        # Tenta di recuperare il vero supplier_name dal DB ANAC
        try:
            with sqlite3.connect(str(self.detector.anac_db), timeout=30) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT supplier_name FROM anac_tenders "
                    "WHERE supplier_id = ? AND supplier_name IS NOT NULL "
                    "LIMIT 1",
                    (piva,),
                ).fetchone()
                if row and row["supplier_name"]:
                    synthetic["data"]["supplier_name"] = row["supplier_name"]
        except sqlite3.Error as e:
            logger.warning(f"Lookup supplier_name in ANAC fallito: {e}")

        return self.enrich_anomaly(synthetic, dry_run=dry_run)


# ============================================================
# CLI
# ============================================================


def _format_result_summary(r: dict) -> str:
    a = r.get("anomaly", {}) or {}
    data = a.get("data", {}) or {}
    return (
        f"  supplier={data.get('supplier_name')} "
        f"piva={r.get('supplier_piva')} "
        f"officers={len(r.get('officers') or [])} "
        f"os_hits={len(r.get('opensanctions_hits') or [])} "
        f"coesione={len(r.get('opencoesione_funds') or [])} "
        f"flags={r.get('flags')} "
        f"scoop_score={r.get('scoop_score'):.2f} "
        f"kg_updated={r.get('kg_updated')}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="AIena Scoop Enricher (C7): arricchisce anomalie ANAC con OC/OS/OCoesione."
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.5,
        help="Score minimo anomalie ANAC da arricchire (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Non scrive nel KG (utile per test)",
    )
    parser.add_argument(
        "--piva",
        type=str,
        default=None,
        help="Esegue enrichment su singola P.IVA (test mode)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Logging DEBUG"
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    enricher = ScoopEnricher()

    if args.piva:
        logger.info(f"=== Modalita single-PIVA: {args.piva} (dry_run={args.dry_run}) ===")
        result = enricher.run_for_piva(args.piva, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print()
        print(_format_result_summary(result))
        return 0

    logger.info(
        f"=== Modalita batch (min_score={args.min_score}, dry_run={args.dry_run}) ==="
    )
    results = enricher.run(min_score=args.min_score, dry_run=args.dry_run)
    logger.info(f"Arricchite {len(results)} anomalie")
    high = [r for r in results if r["scoop_score"] >= 0.6]
    logger.info(f"Scoop ad alto valore (score>=0.6): {len(high)}")
    for r in sorted(results, key=lambda x: -x["scoop_score"])[:10]:
        print(_format_result_summary(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
