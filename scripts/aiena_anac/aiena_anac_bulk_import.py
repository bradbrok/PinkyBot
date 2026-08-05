#!/usr/bin/env python3
"""
aiena_anac_bulk_import.py — Importa file ANAC mensili con ijson streaming.

Usato per file >1GB che non possono essere letti interamente in RAM.
Importa dal file locale se esiste, altrimenti scarica da ANAC.

Uso:
  python3 aiena_anac_bulk_import.py                     # tutti i mesi mancanti
  python3 aiena_anac_bulk_import.py --months 2025-01    # solo gennaio 2025
  python3 aiena_anac_bulk_import.py --dry-run           # solo check stato

Author: Satoshi (PinkyBot) 2026-05-05
"""

import argparse
import gzip
import ijson
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from aiena_anac.aiena_anac_fetcher import ANACFetcher

DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_anac_cache.db")
DATA_DIR = Path("/home/pinky/.pinkybot/data")
BULK_URL = "https://dati.anticorruzione.it/opendata/download/dataset/ocds/filesystem/bulk/{year}/{month}.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Mesi da coprire (ultimi 12 mesi circa)
TARGET_MONTHS = [
    "2024-10", "2024-11", "2024-12",
    "2025-01", "2025-02", "2025-03", "2025-04",
]


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_str(v: Any) -> str | None:
    return str(v).strip() if v is not None else None


def safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def safe_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_imported_months(conn: sqlite3.Connection) -> set[str]:
    """Legge i mesi già importati dalla tabella di tracking."""
    try:
        rows = conn.execute(
            "SELECT dataset_key FROM anac_downloads WHERE status='completed'"
        ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def mark_imported(conn: sqlite3.Connection, month_key: str, count: int) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO anac_downloads
           (dataset_key, last_fetched, records_count, status)
           VALUES (?, ?, ?, 'completed')""",
        (month_key, now_iso(), count)
    )
    conn.commit()


def parse_release(release: dict) -> dict | None:
    """Estrae i campi da un OCDS release.
    Supporta due formati:
    - Standard: ha 'tender' con id, numberOfTenderers, ecc.
    - Award-only (jan2025+): solo 'awards' + 'parties' (no 'tender')
    """
    try:
        tender = release.get("tender") or {}

        cig = (
            tender.get("id")
            or release.get("id")
            or release.get("ocid", "")
        )
        if not cig:
            return None

        # Buyer e supplier da parties (presenti in entrambi i formati)
        buyer_name = buyer_id = buyer_region = None
        supplier_name = supplier_id = supplier_address = None
        for party in release.get("parties", []):
            roles = party.get("roles", [])
            addr = party.get("address", {})
            identifier = party.get("identifier") or party.get("additionalIdentifiers", [{}])[0] if party.get("additionalIdentifiers") else {}
            pid = (party.get("identifier") or {}).get("id") or identifier.get("id")
            pname = party.get("name")
            pregion = addr.get("region") or addr.get("locality") or addr.get("nuts", "")
            if "buyer" in roles or "procuringEntity" in roles:
                if not buyer_name:
                    buyer_name = pname
                    buyer_id = pid
                    buyer_region = pregion
            if "supplier" in roles or "tenderer" in roles:
                if not supplier_name:
                    supplier_name = pname
                    supplier_id = pid
                    supplier_address = ", ".join(filter(None, [
                        addr.get("streetAddress", ""),
                        addr.get("locality", ""),
                        addr.get("region", ""),
                    ]))

        # Award: data, importo, (eventuale supplier in awards.suppliers)
        amount_value = award_date = None
        awards = release.get("awards", [])
        if awards:
            award = awards[0]
            award_date = award.get("date")
            val = award.get("value") or {}
            amount_value = safe_float(val.get("amount"))
            # Formato vecchio: supplier in awards.suppliers
            suppliers = award.get("suppliers", [])
            if suppliers:
                sup = suppliers[0]
                supplier_name = sup.get("name")
                sup_id = (sup.get("identifier") or {})
                supplier_id = sup_id.get("id") or sup_id.get("legalName")
                sup_addr = sup.get("address", {})
                supplier_address = ", ".join(filter(None, [
                    sup_addr.get("streetAddress", ""),
                    sup_addr.get("locality", ""),
                    sup_addr.get("region", ""),
                ]))

        # Tender details (presenti solo nel formato standard)
        tender_status = tender.get("status")
        method = tender.get("procurementMethod")
        n_tenderers = safe_int(tender.get("numberOfTenderers"))
        estimated = safe_float((tender.get("value") or {}).get("amount"))
        title = tender.get("title", "")[:500] if tender.get("title") else None
        description = tender.get("description", "")[:1000] if tender.get("description") else None
        tender_start = tender.get("tenderPeriod", {}).get("startDate")
        tender_end = tender.get("tenderPeriod", {}).get("endDate")
        contract_start = contract_end = None
        contracts = release.get("contracts", [])
        if contracts:
            cp = contracts[0].get("period", {})
            contract_start = cp.get("startDate")
            contract_end = cp.get("endDate")

        # CPV: prima da tender.items, poi da awards[0].items (formato award-only)
        cpv_code = cpv_desc = None
        items_tender = tender.get("items", [])
        items_award = awards[0].get("items", []) if awards else []
        items_cpv = items_tender or items_award
        if items_cpv:
            cl = items_cpv[0].get("classification", {})
            cpv_code = cl.get("id")
            cpv_desc = cl.get("description")
            # Se non c'è title, usa description CPV
            if not title and cpv_desc:
                title = cpv_desc[:500]

        now = now_iso()
        return {
            "cig": str(cig)[:255],
            "ocid": release.get("ocid"),
            "buyer_name": buyer_name,
            "buyer_id": buyer_id,
            "buyer_region": buyer_region,
            "title": title,
            "description": description,
            "procurement_method": method,
            "tender_status": tender_status,
            "amount_value": amount_value,
            "amount_currency": "EUR",
            "estimated_value": estimated,
            "tender_start_date": tender_start,
            "tender_end_date": tender_end,
            "award_date": award_date,
            "contract_start": contract_start,
            "contract_end": contract_end,
            "supplier_name": supplier_name,
            "supplier_id": supplier_id,
            "supplier_address": supplier_address,
            "number_of_tenderers": n_tenderers,
            "category": cpv_desc,
            "cpv_code": cpv_code,
            "source_url": "",
            "fetched_at": now,
            "updated_at": now,
        }
    except Exception as e:
        return None


def import_local_file(file_path: Path, conn: sqlite3.Connection, month_key: str) -> int:
    """Importa un file ANAC locale con ijson streaming. Non carica tutto in RAM."""
    log(f"Import streaming da {file_path.name} ({file_path.stat().st_size / 1e9:.1f}GB)...")
    imported = 0
    skipped = 0
    errors = 0
    batch = []
    BATCH_SIZE = 500

    def flush_batch():
        nonlocal imported, skipped, errors
        for parsed in batch:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO anac_tenders
                       (cig, ocid, buyer_name, buyer_id, buyer_region, title, description,
                        procurement_method, tender_status, amount_value, amount_currency,
                        estimated_value, tender_start_date, tender_end_date, award_date,
                        contract_start, contract_end, supplier_name, supplier_id,
                        supplier_address, number_of_tenderers, category, cpv_code,
                        source_url, fetched_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        parsed["cig"], parsed["ocid"], parsed["buyer_name"], parsed["buyer_id"],
                        parsed["buyer_region"], parsed["title"], parsed["description"],
                        parsed["procurement_method"], parsed["tender_status"], parsed["amount_value"],
                        parsed["amount_currency"], parsed["estimated_value"],
                        parsed["tender_start_date"], parsed["tender_end_date"], parsed["award_date"],
                        parsed["contract_start"], parsed["contract_end"],
                        parsed["supplier_name"], parsed["supplier_id"], parsed["supplier_address"],
                        parsed["number_of_tenderers"], parsed["category"], parsed["cpv_code"],
                        f"file://{file_path}", parsed["fetched_at"], parsed["updated_at"],
                    )
                )
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1
            except Exception as ex:
                errors += 1
        conn.commit()
        batch.clear()

    t0 = time.time()
    processed = 0
    with open(file_path, "rb") as f:
        for release in ijson.items(f, "releases.item"):
            processed += 1
            parsed = parse_release(release)
            if parsed:
                batch.append(parsed)
            if len(batch) >= BATCH_SIZE:
                flush_batch()
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                if processed % 10000 < BATCH_SIZE:
                    log(f"  Progress: {processed:,} parsed, {imported:,} new, {skipped:,} dup — {rate:.0f} rec/s")

    if batch:
        flush_batch()

    elapsed = time.time() - t0
    log(f"Import {month_key}: {imported:,} nuovi, {skipped:,} dup, {errors} errori in {elapsed:.0f}s")
    return imported


def download_month(year: int, month: int) -> Path | None:
    """Scarica un file mensile ANAC. Ritorna il path del file o None."""
    month_str = f"{month:02d}"
    month_key = f"{year}-{month_str}"
    url = BULK_URL.format(year=year, month=month_str)
    out_path = DATA_DIR / f"anac_{year}_{month_str}.json"

    if out_path.exists():
        log(f"{month_key}: file già presente ({out_path.stat().st_size / 1e6:.0f}MB) — skip download")
        return out_path

    log(f"Download {month_key} da {url}...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                log(f"  HTTP {resp.status} — skip")
                return None
            size = int(resp.headers.get("Content-Length", 0))
            log(f"  Dimensione: {size / 1e9:.2f}GB")
            downloaded = 0
            with open(out_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)  # 1MB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if downloaded % (100 * 1024 * 1024) == 0:
                        log(f"  Scaricato: {downloaded / 1e9:.1f}GB")
        log(f"  Download completato: {out_path.stat().st_size / 1e9:.1f}GB")
        return out_path
    except Exception as e:
        log(f"  Errore download {month_key}: {e}")
        if out_path.exists():
            out_path.unlink()
        return None


def check_status() -> None:
    """Mostra stato attuale DB ANAC."""
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM anac_tenders").fetchone()[0]
    imported_months = get_imported_months(conn)
    conn.close()

    print(f"\n=== Stato ANAC DB ===")
    print(f"Record totali: {total:,}")
    print(f"Mesi importati (tracking): {len(imported_months)}")
    for m in sorted(imported_months):
        print(f"  ✓ {m}")

    print(f"\nFile locali disponibili:")
    for f in sorted(DATA_DIR.glob("anac_*.json")):
        print(f"  {f.name}: {f.stat().st_size / 1e9:.2f}GB")

    print(f"\nMesi target: {TARGET_MONTHS}")
    missing = [m for m in TARGET_MONTHS if m not in imported_months]
    print(f"Mesi mancanti dal tracking: {missing}")

    # Check anche file locali non tracciati
    local_files = {}
    for f in DATA_DIR.glob("anac_*.json"):
        # anac_jan2025.json or anac_2025_01.json
        name = f.stem
        if "jan" in name:
            local_files["2025-01"] = f
        else:
            parts = name.replace("anac_", "").split("_")
            if len(parts) == 2:
                key = f"{parts[0]}-{parts[1]}"
                local_files[key] = f

    if local_files:
        print(f"\nFile locali non tracciati:")
        for k, f in local_files.items():
            in_tracking = k in imported_months
            status = "✓ già importato" if in_tracking else "⚠ DA IMPORTARE"
            print(f"  {k}: {f.name} ({f.stat().st_size / 1e9:.2f}GB) — {status}")


def main():
    parser = argparse.ArgumentParser(description="ANAC bulk import con ijson streaming")
    parser.add_argument("--months", nargs="+", help="Mesi specifici (es. 2025-01 2025-02)")
    parser.add_argument("--dry-run", action="store_true", help="Solo check stato")
    parser.add_argument("--skip-download", action="store_true", help="Solo importa file locali, non scaricare")
    args = parser.parse_args()

    check_status()

    if args.dry_run:
        return

    conn = sqlite3.connect(DB_PATH)
    imported_months = get_imported_months(conn)

    # Determina mesi da processare
    months_to_process = args.months if args.months else TARGET_MONTHS
    months_to_process = [m for m in months_to_process if m not in imported_months]

    if not months_to_process:
        log("Tutti i mesi target già importati. Niente da fare.")
        conn.close()
        return

    log(f"\nMesi da processare: {months_to_process}")

    for month_key in months_to_process:
        year, month = int(month_key[:4]), int(month_key[5:7])

        # Cerca file locale (anche con nome legacy)
        local_candidates = [
            DATA_DIR / f"anac_{year}_{month:02d}.json",
            DATA_DIR / f"anac_jan{year}.json" if month == 1 else None,
        ]
        # anac_jan2025.json → special case
        if month == 1:
            legacy = DATA_DIR / f"anac_jan{year}.json"
            if legacy.exists():
                local_candidates.append(legacy)

        file_path = None
        for candidate in local_candidates:
            if candidate and candidate.exists():
                file_path = candidate
                break

        # Download se non disponibile localmente
        if not file_path and not args.skip_download:
            file_path = download_month(year, month)

        if not file_path:
            log(f"{month_key}: file non disponibile, skip")
            continue

        # Import
        count = import_local_file(file_path, conn, month_key)
        if count >= 0:
            mark_imported(conn, month_key, count)
            log(f"✓ {month_key} importato: {count:,} nuovi record")

    conn.close()
    log("\nImport completato. Ora esegui:")
    log("  python3 aiena_anomaly_detector.py  # ricalcola anomalie")
    log("  python3 aiena_flags_to_kg.py       # anomalie → KG")
    log("  python3 ../aiena_kg/scoop_engine.py # rigenera leads")


if __name__ == "__main__":
    main()
