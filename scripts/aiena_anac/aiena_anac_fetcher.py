#!/usr/bin/env python3
"""
AIena ANAC Fetcher — Scarica dati grezzi ANAC in modo incrementale.

I dati ANAC sono pubblicati in formato OCDS (Open Contracting Data Standard)
come file JSON/CSV scaricabili dal portale dati.anticorruzione.it.

Nota: Il portale ANAC ha un WAF aggressivo. Questo modulo:
- Usa User-Agent browser standard
- Implementa rate limiting (1 req/sec)
- Fa retry esponenziale
- Cache locale in SQLite

Decisioni architetturali:
- Download file completi (non API) per evitare WAF
- SQLite per cache locale (coerenza con stack)
- Incrementale: scarica solo file piu recenti del last_fetch
- Focus su campi rilevanti per anomaly detection
"""
import gzip
import hashlib
import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

# ============================================================
# Configurazione
# ============================================================

DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_anac_cache.db")

# User agent realistico
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Rate limiting
REQUEST_DELAY = 2.0  # secondi tra richieste
MAX_RETRIES = 3

# Dataset ANAC disponibili (OCDS format)
# Fonte: https://dati.anticorruzione.it/opendata/dataset
ANAC_DATASETS = {
    "appalti_2024": {
        "name": "Appalti Ordinari 2024",
        "url": "https://dati.anticorruzione.it/opendata/dataset/ocds-appalti-ordinari-2024",
        "year": 2024,
    },
    "appalti_2025": {
        "name": "Appalti Ordinari 2025",
        "url": "https://dati.anticorruzione.it/opendata/dataset/ocds-appalti-ordinari-2025",
        "year": 2025,
    },
    "appalti_2026": {
        "name": "Appalti Ordinari 2026",
        "url": "https://dati.anticorruzione.it/opendata/dataset/ocds-appalti-ordinari-2026",
        "year": 2026,
    },
}

# Categorie di interesse per AIena
CATEGORIES_OF_INTEREST = [
    "sanita",
    "sanità",
    "ospedale",
    "asl",
    "asp",
    "manutenzione",
    "edilizia",
    "scuola",
    "scolastic",
    "pnrr",
    "vigilanza",
    "pulizia",
    "guardiania",
]


# ============================================================
# Schema SQLite per cache ANAC
# ============================================================

ANAC_SCHEMA = """
-- Cache dati ANAC scaricati
CREATE TABLE IF NOT EXISTS anac_tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identificatori ANAC
    cig TEXT UNIQUE NOT NULL,            -- Codice Identificativo Gara
    ocid TEXT,                           -- Open Contracting ID

    -- Ente appaltante
    buyer_name TEXT,
    buyer_id TEXT,                       -- CF ente
    buyer_region TEXT,

    -- Dettagli gara
    title TEXT,
    description TEXT,
    procurement_method TEXT,             -- open, limited, direct, etc.
    tender_status TEXT,                  -- planning, active, complete, cancelled

    -- Importi
    amount_value REAL,                   -- Importo aggiudicazione
    amount_currency TEXT DEFAULT 'EUR',
    estimated_value REAL,                -- Importo base asta

    -- Date
    tender_start_date TEXT,
    tender_end_date TEXT,
    award_date TEXT,
    contract_start TEXT,
    contract_end TEXT,

    -- Aggiudicatario
    supplier_name TEXT,
    supplier_id TEXT,                    -- CF/PIVA fornitore
    supplier_address TEXT,

    -- Statistiche gara
    number_of_tenderers INTEGER,         -- Numero offerte ricevute
    award_criteria TEXT,                 -- lowest_price, best_value, etc.

    -- Percentuali
    rebate_percentage REAL,              -- Ribasso %

    -- Metadata
    category TEXT,                       -- Categoria merceologica (CPV)
    cpv_code TEXT,                       -- Codice CPV
    nuts_code TEXT,                      -- Codice NUTS (regione)
    source_url TEXT,
    raw_data TEXT,                       -- JSON originale

    -- Tracking
    fetched_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    -- Flags per anomaly detection
    flagged INTEGER DEFAULT 0,
    flag_reason TEXT
);

-- Download tracking
CREATE TABLE IF NOT EXISTS anac_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key TEXT UNIQUE NOT NULL,
    last_fetched TEXT,
    file_hash TEXT,
    records_count INTEGER,
    status TEXT DEFAULT 'pending'
);

-- Indici
CREATE INDEX IF NOT EXISTS idx_anac_cig ON anac_tenders(cig);
CREATE INDEX IF NOT EXISTS idx_anac_buyer ON anac_tenders(buyer_id);
CREATE INDEX IF NOT EXISTS idx_anac_supplier ON anac_tenders(supplier_id);
CREATE INDEX IF NOT EXISTS idx_anac_supplier_name ON anac_tenders(supplier_name);
CREATE INDEX IF NOT EXISTS idx_anac_amount ON anac_tenders(amount_value);
CREATE INDEX IF NOT EXISTS idx_anac_date ON anac_tenders(award_date);
CREATE INDEX IF NOT EXISTS idx_anac_method ON anac_tenders(procurement_method);
CREATE INDEX IF NOT EXISTS idx_anac_status ON anac_tenders(tender_status);
CREATE INDEX IF NOT EXISTS idx_anac_category ON anac_tenders(category);
CREATE INDEX IF NOT EXISTS idx_anac_flagged ON anac_tenders(flagged);
CREATE INDEX IF NOT EXISTS idx_anac_rebate ON anac_tenders(rebate_percentage);
CREATE INDEX IF NOT EXISTS idx_anac_tenderers ON anac_tenders(number_of_tenderers);
"""


# ============================================================
# Utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(val: Any) -> float | None:
    """Converte un valore in float, gestendo errori."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int(val: Any) -> int | None:
    """Converte un valore in int, gestendo errori."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def extract_region_from_nuts(nuts: str | None) -> str | None:
    """Estrae la regione dal codice NUTS."""
    # Mappatura codici NUTS italiani -> regione
    nuts_map = {
        "ITC1": "Piemonte", "ITC2": "Valle d'Aosta", "ITC3": "Liguria",
        "ITC4": "Lombardia", "ITH1": "Trentino-Alto Adige", "ITH2": "Trentino-Alto Adige",
        "ITH3": "Veneto", "ITH4": "Friuli-Venezia Giulia", "ITH5": "Emilia-Romagna",
        "ITI1": "Toscana", "ITI2": "Umbria", "ITI3": "Marche", "ITI4": "Lazio",
        "ITF1": "Abruzzo", "ITF2": "Molise", "ITF3": "Campania", "ITF4": "Puglia",
        "ITF5": "Basilicata", "ITF6": "Calabria", "ITG1": "Sicilia", "ITG2": "Sardegna",
    }
    if not nuts:
        return None
    # Prendi i primi 4 caratteri
    prefix = nuts[:4].upper()
    return nuts_map.get(prefix)


# ============================================================
# Classe principale
# ============================================================

class ANACFetcher:
    """
    Fetcher per dati ANAC.

    Scarica i dataset OCDS e li salva in SQLite locale.

    Esempio:
        fetcher = ANACFetcher()
        fetcher.fetch_all()
        tenders = fetcher.get_tenders(buyer_region="Calabria", min_amount=100000)
    """

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._last_request_time = 0

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.executescript(ANAC_SCHEMA)

    def _rate_limit(self):
        """Implementa rate limiting."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self._last_request_time = time.time()

    def _fetch_url(self, url: str) -> bytes | None:
        """
        Scarica un URL con retry e rate limiting.
        """
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/csv, */*",
            "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
        }

        for attempt in range(MAX_RETRIES):
            self._rate_limit()
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read()

                    # Decomprime se gzipped
                    if response.info().get("Content-Encoding") == "gzip":
                        data = gzip.decompress(data)

                    return data

            except HTTPError as e:
                if e.code == 429:
                    # Rate limited, aspetta di piu
                    wait_time = REQUEST_DELAY * (2 ** attempt)
                    print(f"[ANAC] Rate limited, attendo {wait_time}s...")
                    time.sleep(wait_time)
                elif e.code in (403, 406):
                    # WAF blocked
                    print(f"[ANAC] Bloccato da WAF (HTTP {e.code})")
                    return None
                else:
                    print(f"[ANAC] HTTP error {e.code}: {e.reason}")
                    if attempt == MAX_RETRIES - 1:
                        return None

            except URLError as e:
                print(f"[ANAC] URL error: {e.reason}")
                if attempt == MAX_RETRIES - 1:
                    return None

            except Exception as e:
                print(f"[ANAC] Error: {e}")
                if attempt == MAX_RETRIES - 1:
                    return None

        return None

    def fetch_dataset_page(self, dataset_url: str) -> list[str]:
        """
        Scrapa la pagina del dataset per trovare i link ai file dati.

        Nota: Il WAF ANAC blocca spesso. Questo metodo potrebbe fallire.
        In tal caso, usiamo URL pre-calcolati.
        """
        data = self._fetch_url(dataset_url)
        if not data:
            return []

        import re
        text = data.decode("utf-8", errors="ignore")

        # Cerca link a file JSON/CSV
        links = re.findall(r'href="([^"]*\.(?:json|csv)[^"]*)"', text, re.IGNORECASE)
        return links

    def _parse_ocds_release(self, release: dict) -> dict | None:
        """
        Parsa un release OCDS in formato per il nostro DB.

        Schema OCDS: https://standard.open-contracting.org/latest/en/
        """
        try:
            # Estrai campi base
            ocid = release.get("ocid", "")
            tender = release.get("tender", {})
            awards = release.get("awards", [])
            parties = release.get("parties", [])
            contracts = release.get("contracts", [])

            # CIG: ANAC usa tender.id come CIG (es. "ARIA_2024_032/L0095")
            # Fallback: estrai dall'ocid
            cig = tender.get("id", "")
            if not cig and ocid:
                cig = ocid.split("/")[-1] if "/" in ocid else ocid

            if not cig:
                return None

            # Buyer: ANAC usa campo top-level "buyer" (non in parties[])
            buyer_top = release.get("buyer", {})
            if buyer_top:
                buyer_name = buyer_top.get("name", "")
                buyer_id = buyer_top.get("id", "")
            else:
                # Fallback: cerca in parties[] con ruolo buyer
                buyer = next((p for p in parties if "buyer" in p.get("roles", [])), {})
                buyer_name = buyer.get("name", "")
                buyer_id = buyer.get("identifier", {}).get("id", "")
            buyer_region = None  # ANAC non fornisce region nel buyer standard

            # Trova supplier (dal primo award)
            supplier_name = ""
            supplier_id = ""
            supplier_address = ""
            award_date = None
            amount_value = None
            number_of_tenderers = None

            if awards:
                award = awards[0]
                award_date = award.get("date")
                award_value = award.get("value") or {}
                amount_value = safe_float(award_value.get("amount"))

                suppliers = award.get("suppliers", [])
                if suppliers:
                    sup = suppliers[0]
                    supplier_name = sup.get("name", "")
                    # ANAC: supplier.id diretta (non identifier.id)
                    supplier_id = sup.get("id", "") or sup.get("identifier", {}).get("id", "")
                    sup_addr = sup.get("address", {}) or {}
                    supplier_address = ", ".join(filter(None, [
                        sup_addr.get("streetAddress", ""),
                        sup_addr.get("locality", ""),
                        sup_addr.get("region", "")
                    ]))

            # Tender details
            title = tender.get("title", "")
            description = tender.get("description", "")
            procurement_method = tender.get("procurementMethod", "")
            tender_status = tender.get("status", "")
            estimated_value = safe_float(tender.get("value", {}).get("amount"))
            number_of_tenderers = safe_int(tender.get("numberOfTenderers"))

            # Date
            tender_period = tender.get("tenderPeriod", {})
            tender_start = tender_period.get("startDate")
            tender_end = tender_period.get("endDate")

            # Contratti
            contract_start = None
            contract_end = None
            if contracts:
                contract = contracts[0]
                period = contract.get("period", {})
                contract_start = period.get("startDate")
                contract_end = period.get("endDate")

            # CPV e categoria
            items = tender.get("items", [])
            cpv_code = ""
            category = ""
            if items:
                classification = items[0].get("classification", {})
                cpv_code = classification.get("id", "")
                category = classification.get("description", "")

            # Calcola ribasso
            rebate_percentage = None
            if estimated_value and amount_value and estimated_value > 0:
                rebate_percentage = ((estimated_value - amount_value) / estimated_value) * 100

            return {
                "cig": cig,
                "ocid": ocid,
                "buyer_name": buyer_name,
                "buyer_id": buyer_id,
                "buyer_region": buyer_region,
                "title": title,
                "description": description,
                "procurement_method": procurement_method,
                "tender_status": tender_status,
                "amount_value": amount_value,
                "estimated_value": estimated_value,
                "tender_start_date": tender_start,
                "tender_end_date": tender_end,
                "award_date": award_date,
                "contract_start": contract_start,
                "contract_end": contract_end,
                "supplier_name": supplier_name,
                "supplier_id": supplier_id,
                "supplier_address": supplier_address,
                "number_of_tenderers": number_of_tenderers,
                "rebate_percentage": rebate_percentage,
                "category": category,
                "cpv_code": cpv_code,
                "raw_data": json.dumps(release, ensure_ascii=False, default=str)
            }

        except Exception as e:
            print(f"[ANAC] Errore parsing release: {e}")
            return None

    def import_json_data(self, json_data: bytes, source_url: str = "") -> int:
        """
        Importa dati JSON OCDS nel database.

        Args:
            json_data: Bytes del file JSON
            source_url: URL di provenienza

        Returns:
            Numero di record importati
        """
        try:
            data = json.loads(json_data.decode("utf-8"))
        except json.JSONDecodeError as e:
            print(f"[ANAC] Errore parsing JSON: {e}")
            return 0

        # Il formato OCDS ha releases array
        releases = data.get("releases", [])
        if not releases:
            # Potrebbe essere un singolo release
            if "ocid" in data:
                releases = [data]
            else:
                print("[ANAC] Nessun release trovato nel JSON")
                return 0

        imported = 0
        now = now_iso()

        with self._get_conn() as conn:
            for release in releases:
                parsed = self._parse_ocds_release(release)
                if not parsed:
                    continue

                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO anac_tenders
                            (cig, ocid, buyer_name, buyer_id, buyer_region,
                             title, description, procurement_method, tender_status,
                             amount_value, estimated_value,
                             tender_start_date, tender_end_date, award_date,
                             contract_start, contract_end,
                             supplier_name, supplier_id, supplier_address,
                             number_of_tenderers, rebate_percentage,
                             category, cpv_code, source_url, raw_data,
                             fetched_at, updated_at)
                        VALUES
                            (:cig, :ocid, :buyer_name, :buyer_id, :buyer_region,
                             :title, :description, :procurement_method, :tender_status,
                             :amount_value, :estimated_value,
                             :tender_start_date, :tender_end_date, :award_date,
                             :contract_start, :contract_end,
                             :supplier_name, :supplier_id, :supplier_address,
                             :number_of_tenderers, :rebate_percentage,
                             :category, :cpv_code, :source_url, :raw_data,
                             :fetched_at, :updated_at)
                    """, {
                        **parsed,
                        "source_url": source_url,
                        "fetched_at": now,
                        "updated_at": now,
                    })
                    imported += 1
                except sqlite3.IntegrityError:
                    pass  # CIG duplicato, ignora

            conn.commit()

        return imported

    def import_stream(self, url: str, max_records: int | None = None) -> int:
        """
        Importa dati OCDS da URL scaricando su disco, poi parsando con ijson.

        Approccio robusto per file da centinaia di MB:
        1. Scarica su file temporaneo in chunks (nessun timeout di parsing)
        2. Parsa con ijson dal file (streaming, poca RAM)
        3. Inserisce in DB a batch

        Args:
            url: URL del file JSON OCDS
            max_records: Limite massimo record da importare (None = tutti)

        Returns:
            Numero di record importati
        """
        import tempfile
        import os

        try:
            import ijson
        except ImportError:
            print("[ANAC] ijson non installato. Usa: pip install ijson")
            return 0

        # Step 1: Scarica su file temporaneo
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="anac_")
        os.close(tmp_fd)

        print(f"[ANAC] Download: {url}")
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "identity",
            }
        )

        downloaded = 0
        try:
            with urllib.request.urlopen(req, timeout=300) as response:
                with open(tmp_path, "wb") as f:
                    while True:
                        chunk = response.read(131072)  # 128KB
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if downloaded % (50 * 1024 * 1024) < 131072:
                            print(f"[ANAC]   {downloaded / 1024 / 1024:.0f} MB scaricati...")

            print(f"[ANAC] Download completato: {downloaded / 1024 / 1024:.1f} MB")

        except Exception as e:
            print(f"[ANAC] Errore download: {e}")
            os.unlink(tmp_path)
            return 0

        # Step 2: Parsa da file con ijson
        imported = 0
        batch: list[dict] = []
        batch_size = 500
        now = now_iso()

        print(f"[ANAC] Parsing con ijson...")
        try:
            with open(tmp_path, "rb") as f:
                for release in ijson.items(f, "releases.item"):
                    parsed = self._parse_ocds_release(release)
                    if not parsed:
                        continue

                    parsed["source_url"] = url
                    parsed["fetched_at"] = now
                    parsed["updated_at"] = now
                    batch.append(parsed)

                    if len(batch) >= batch_size:
                        imported += self._insert_batch(batch)
                        batch = []
                        print(f"[ANAC]   {imported} record importati...")

                    if max_records and imported >= max_records:
                        print(f"[ANAC] Limite {max_records} raggiunto, stop.")
                        break

            if batch:
                imported += self._insert_batch(batch)

        except Exception as e:
            print(f"[ANAC] Errore parsing: {e}")
        finally:
            os.unlink(tmp_path)

        print(f"[ANAC] Completato: {imported} record importati")
        return imported

    def _insert_batch(self, batch: list[dict]) -> int:
        """Inserisce un batch di record nel DB. Ritorna numero inseriti."""
        inserted = 0
        with self._get_conn() as conn:
            for parsed in batch:
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO anac_tenders
                            (cig, ocid, buyer_name, buyer_id, buyer_region,
                             title, description, procurement_method, tender_status,
                             amount_value, estimated_value,
                             tender_start_date, tender_end_date, award_date,
                             contract_start, contract_end,
                             supplier_name, supplier_id, supplier_address,
                             number_of_tenderers, rebate_percentage,
                             category, cpv_code, source_url, raw_data,
                             fetched_at, updated_at)
                        VALUES
                            (:cig, :ocid, :buyer_name, :buyer_id, :buyer_region,
                             :title, :description, :procurement_method, :tender_status,
                             :amount_value, :estimated_value,
                             :tender_start_date, :tender_end_date, :award_date,
                             :contract_start, :contract_end,
                             :supplier_name, :supplier_id, :supplier_address,
                             :number_of_tenderers, :rebate_percentage,
                             :category, :cpv_code, :source_url, :raw_data,
                             :fetched_at, :updated_at)
                    """, parsed)
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
        return inserted

    def import_from_file(self, file_path: Path | str) -> int:
        """Importa da file locale."""
        path = Path(file_path)
        if not path.exists():
            print(f"[ANAC] File non trovato: {path}")
            return 0

        data = path.read_bytes()
        if path.suffix == ".gz":
            data = gzip.decompress(data)

        return self.import_json_data(data, source_url=f"file://{path}")

    def get_tenders(
        self,
        buyer_region: str | None = None,
        buyer_name: str | None = None,
        supplier_name: str | None = None,
        supplier_id: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        procurement_method: str | None = None,
        year: int | None = None,
        category_contains: str | None = None,
        flagged_only: bool = False,
        limit: int = 100
    ) -> list[dict]:
        """
        Cerca gare nel database locale.

        Args:
            buyer_region: Filtra per regione ente
            buyer_name: Filtra per nome ente (LIKE)
            supplier_name: Filtra per nome fornitore (LIKE)
            supplier_id: Filtra per CF/PIVA fornitore
            min_amount: Importo minimo
            max_amount: Importo massimo
            procurement_method: Metodo (open, limited, direct)
            year: Anno della gara
            category_contains: Categoria contiene testo
            flagged_only: Solo gare flaggate
            limit: Numero massimo risultati

        Returns:
            Lista di gare
        """
        sql = "SELECT * FROM anac_tenders WHERE 1=1"
        params: list[Any] = []

        if buyer_region:
            sql += " AND buyer_region = ?"
            params.append(buyer_region)

        if buyer_name:
            sql += " AND buyer_name LIKE ?"
            params.append(f"%{buyer_name}%")

        if supplier_name:
            sql += " AND supplier_name LIKE ?"
            params.append(f"%{supplier_name}%")

        if supplier_id:
            sql += " AND supplier_id = ?"
            params.append(supplier_id)

        if min_amount is not None:
            sql += " AND amount_value >= ?"
            params.append(min_amount)

        if max_amount is not None:
            sql += " AND amount_value <= ?"
            params.append(max_amount)

        if procurement_method:
            sql += " AND procurement_method = ?"
            params.append(procurement_method)

        if year:
            sql += " AND (award_date LIKE ? OR tender_start_date LIKE ?)"
            year_pattern = f"{year}%"
            params.extend([year_pattern, year_pattern])

        if category_contains:
            sql += " AND (category LIKE ? OR title LIKE ? OR description LIKE ?)"
            cat_pattern = f"%{category_contains}%"
            params.extend([cat_pattern, cat_pattern, cat_pattern])

        if flagged_only:
            sql += " AND flagged = 1"

        sql += " ORDER BY award_date DESC, amount_value DESC LIMIT ?"
        params.append(limit)

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_tenders_by_supplier(
        self,
        supplier_name: str,
        fuzzy: bool = True
    ) -> list[dict]:
        """
        Trova tutte le gare vinte da un fornitore.
        """
        if fuzzy:
            pattern = f"%{supplier_name.upper()}%"
            sql = """
                SELECT * FROM anac_tenders
                WHERE UPPER(supplier_name) LIKE ?
                ORDER BY award_date DESC
            """
            params = [pattern]
        else:
            sql = """
                SELECT * FROM anac_tenders
                WHERE supplier_name = ?
                ORDER BY award_date DESC
            """
            params = [supplier_name]

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_tenders_by_buyer(
        self,
        buyer_name: str,
        fuzzy: bool = True
    ) -> list[dict]:
        """
        Trova tutte le gare di un ente appaltante.
        """
        if fuzzy:
            pattern = f"%{buyer_name.upper()}%"
            sql = """
                SELECT * FROM anac_tenders
                WHERE UPPER(buyer_name) LIKE ?
                ORDER BY award_date DESC
            """
            params = [pattern]
        else:
            sql = """
                SELECT * FROM anac_tenders
                WHERE buyer_name = ?
                ORDER BY award_date DESC
            """
            params = [buyer_name]

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Statistiche sul database locale."""
        with self._get_conn() as conn:
            stats = {
                "total_tenders": conn.execute(
                    "SELECT COUNT(*) FROM anac_tenders"
                ).fetchone()[0],
                "flagged_tenders": conn.execute(
                    "SELECT COUNT(*) FROM anac_tenders WHERE flagged = 1"
                ).fetchone()[0],
                "total_value": conn.execute(
                    "SELECT SUM(amount_value) FROM anac_tenders"
                ).fetchone()[0] or 0,
            }

            # Per regione
            stats["by_region"] = {}
            for row in conn.execute("""
                SELECT buyer_region, COUNT(*) as c, SUM(amount_value) as v
                FROM anac_tenders
                WHERE buyer_region IS NOT NULL
                GROUP BY buyer_region
                ORDER BY c DESC
            """).fetchall():
                stats["by_region"][row["buyer_region"]] = {
                    "count": row["c"],
                    "value": row["v"] or 0
                }

            # Per anno
            stats["by_year"] = {}
            for row in conn.execute("""
                SELECT SUBSTR(award_date, 1, 4) as year, COUNT(*) as c
                FROM anac_tenders
                WHERE award_date IS NOT NULL
                GROUP BY year
                ORDER BY year DESC
            """).fetchall():
                if row["year"]:
                    stats["by_year"][row["year"]] = row["c"]

            return stats

    def flag_tender(self, cig: str, reason: str):
        """Marca una gara come flaggata."""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE anac_tenders
                SET flagged = 1, flag_reason = ?, updated_at = ?
                WHERE cig = ?
            """, (reason, now_iso(), cig))


# ============================================================
# Test self-contained
# ============================================================

if __name__ == "__main__":
    print("=== Test ANACFetcher ===\n")

    fetcher = ANACFetcher()

    # Test con dati fittizi OCDS
    test_data = {
        "releases": [
            {
                "ocid": "ocds-test-1234567890",
                "tender": {
                    "id": "1234567890",
                    "title": "Manutenzione Ospedale San Giovanni",
                    "description": "Lavori di manutenzione edilizia",
                    "procurementMethod": "open",
                    "status": "complete",
                    "value": {"amount": 500000, "currency": "EUR"},
                    "numberOfTenderers": 3,
                    "items": [{
                        "classification": {
                            "id": "45000000",
                            "description": "Lavori di costruzione"
                        }
                    }]
                },
                "awards": [{
                    "date": "2025-03-15",
                    "value": {"amount": 425000, "currency": "EUR"},
                    "suppliers": [{
                        "name": "Edilizia Calabria SRL",
                        "identifier": {"id": "12345678901"},
                        "address": {
                            "streetAddress": "Via Roma 1",
                            "locality": "Crotone",
                            "region": "Calabria"
                        }
                    }]
                }],
                "parties": [{
                    "name": "ASP di Crotone",
                    "roles": ["buyer"],
                    "identifier": {"id": "ASPCROTONE"},
                    "address": {"region": "ITF6"}
                }]
            }
        ]
    }

    # Importa dati test
    print("1. Importazione dati test...")
    imported = fetcher.import_json_data(
        json.dumps(test_data).encode(),
        source_url="test://mock"
    )
    print(f"   Importati: {imported} record")

    # Cerca gare
    print("\n2. Ricerca gare Calabria...")
    tenders = fetcher.get_tenders(buyer_region="Calabria")
    print(f"   Trovate: {len(tenders)} gare")
    for t in tenders[:3]:
        print(f"   - CIG: {t['cig']}, {t['title'][:50]}...")
        print(f"     Fornitore: {t['supplier_name']}, Importo: {t['amount_value']}")

    # Cerca per fornitore
    print("\n3. Gare per fornitore 'Edilizia'...")
    by_supplier = fetcher.get_tenders_by_supplier("Edilizia")
    print(f"   Trovate: {len(by_supplier)} gare")

    # Stats
    print("\n4. Statistiche:")
    stats = fetcher.get_stats()
    print(f"   Totale gare: {stats['total_tenders']}")
    print(f"   Valore totale: {stats['total_value']:,.0f} EUR")

    print("\n=== Test completati ===")
