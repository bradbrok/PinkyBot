#!/usr/bin/env python3
"""
AIena Anomaly Detector — Rileva anomalie statistiche nei dati ANAC.

Tipi di anomalie rilevate:
1. RIBASSO_ANOMALO: Ribassi > 2 deviazioni standard dalla media del settore
2. ZERO_CONCORRENZA: Gare con numero_offerte = 1
3. VINCITORE_RICORRENTE: Stessa azienda vince > X% gare dello stesso ente
4. AZIENDA_NEONATA: Azienda creata < 6 mesi prima della prima vincita
5. VARIANTE_ECCESSIVA: Importo finale > 150% dell'importo base
6. CONCENTRAZIONE_GEOGRAFICA: Azienda fuori regione vince spesso in una regione

Decisioni architetturali:
- NumPy per calcoli statistici (gia installato)
- Anomaly score normalizzato 0-1
- Soglie configurabili
- Output strutturato per integrazione con KG e pipeline.json
"""
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

# Import locale (supporta sia import package che esecuzione diretta)
try:
    from .aiena_anac_fetcher import ANACFetcher, DB_PATH as ANAC_DB_PATH
except ImportError:
    from aiena_anac_fetcher import ANACFetcher, DB_PATH as ANAC_DB_PATH

# ============================================================
# Configurazione soglie
# ============================================================

THRESHOLDS = {
    # Ribasso anomalo: > N deviazioni standard dalla media
    "rebate_std_threshold": 2.0,

    # Vincitore ricorrente: > X% gare stesso ente
    "recurring_winner_pct": 30.0,
    "recurring_winner_min_tenders": 3,  # Minimo gare per considerare

    # Azienda neonata: < N mesi dalla fondazione
    "new_company_months": 6,

    # Variante eccessiva: importo finale > X% base
    "variant_threshold_pct": 150.0,

    # Concentrazione: % minima per flag
    "concentration_threshold_pct": 40.0,
    "concentration_min_tenders": 5,
}

# Score weights per tipo anomalia
ANOMALY_WEIGHTS = {
    "RIBASSO_ANOMALO": 0.7,
    "ZERO_CONCORRENZA": 0.6,
    "VINCITORE_RICORRENTE": 0.8,
    "AZIENDA_NEONATA": 0.9,
    "VARIANTE_ECCESSIVA": 0.75,
    "CONCENTRAZIONE_GEOGRAFICA": 0.5,
}

# Database per anomalie (stesso del KG)
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")


# ============================================================
# Utilities
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def calculate_z_score(value: float, mean: float, std: float) -> float:
    """Calcola z-score."""
    if std == 0:
        return 0.0
    return (value - mean) / std


def normalize_score(raw_score: float, max_score: float = 5.0) -> float:
    """Normalizza score in range 0-1."""
    return min(1.0, max(0.0, raw_score / max_score))


# ============================================================
# Classe principale
# ============================================================

class AnomalyDetector:
    """
    Detector di anomalie su dati ANAC.

    Esempio:
        detector = AnomalyDetector()
        anomalies = detector.detect_all()
        high_priority = [a for a in anomalies if a['score'] > 0.7]
    """

    def __init__(
        self,
        anac_db_path: Path | str | None = None,
        kg_db_path: Path | str | None = None
    ):
        self.anac_db = Path(anac_db_path) if anac_db_path else ANAC_DB_PATH
        self.kg_db = Path(kg_db_path) if kg_db_path else KG_DB_PATH
        self.fetcher = ANACFetcher(self.anac_db)

        # Cache statistiche
        self._stats_cache: dict[str, dict] = {}

    def _get_anac_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.anac_db), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_kg_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.kg_db), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _compute_sector_stats(self) -> dict[str, dict]:
        """
        Calcola statistiche per settore (CPV prefix).

        Returns:
            Dict con media e std ribasso per settore
        """
        if "sectors" in self._stats_cache:
            return self._stats_cache["sectors"]

        stats: dict[str, dict] = {}

        with self._get_anac_conn() as conn:
            # Raggruppa per primi 2 caratteri CPV (macro-settore)
            rows = conn.execute("""
                SELECT
                    SUBSTR(cpv_code, 1, 2) as sector,
                    rebate_percentage
                FROM anac_tenders
                WHERE rebate_percentage IS NOT NULL
                  AND cpv_code IS NOT NULL
                  AND cpv_code != ''
            """).fetchall()

        # Organizza per settore
        by_sector: dict[str, list[float]] = defaultdict(list)
        for row in rows:
            sector = row["sector"]
            if sector:
                by_sector[sector].append(row["rebate_percentage"])

        # Calcola media e std
        for sector, values in by_sector.items():
            if len(values) >= 10:  # Minimo per statistiche significative
                arr = np.array(values)
                stats[sector] = {
                    "mean": float(np.mean(arr)),
                    "std": float(np.std(arr)),
                    "count": len(values),
                    "median": float(np.median(arr)),
                }

        self._stats_cache["sectors"] = stats
        return stats

    def _compute_buyer_supplier_stats(self) -> dict[str, dict]:
        """
        Calcola statistiche buyer-supplier (chi vince da chi).

        Returns:
            Dict con conteggi per coppia buyer-supplier
        """
        if "buyer_supplier" in self._stats_cache:
            return self._stats_cache["buyer_supplier"]

        stats: dict[str, dict] = {}

        with self._get_anac_conn() as conn:
            rows = conn.execute("""
                SELECT
                    buyer_id,
                    buyer_name,
                    supplier_id,
                    supplier_name,
                    COUNT(*) as win_count,
                    SUM(amount_value) as total_value
                FROM anac_tenders
                WHERE buyer_id IS NOT NULL
                  AND supplier_id IS NOT NULL
                  AND supplier_id != ''
                  AND supplier_name IS NOT NULL
                  AND supplier_name != ''
                GROUP BY buyer_id, supplier_id
                HAVING win_count >= 2
            """).fetchall()

        # Calcola anche totale gare per buyer
        buyer_totals: dict[str, int] = {}
        for row in conn.execute("""
            SELECT buyer_id, COUNT(*) as total
            FROM anac_tenders
            WHERE buyer_id IS NOT NULL
            GROUP BY buyer_id
        """).fetchall():
            buyer_totals[row["buyer_id"]] = row["total"]

        for row in rows:
            key = f"{row['buyer_id']}:{row['supplier_id']}"
            buyer_total = buyer_totals.get(row["buyer_id"], 1)
            stats[key] = {
                "buyer_id": row["buyer_id"],
                "buyer_name": row["buyer_name"],
                "supplier_id": row["supplier_id"],
                "supplier_name": row["supplier_name"],
                "win_count": row["win_count"],
                "total_value": row["total_value"],
                "win_percentage": (row["win_count"] / buyer_total) * 100,
                "buyer_total_tenders": buyer_total,
            }

        self._stats_cache["buyer_supplier"] = stats
        return stats

    # ========================================================
    # Detection Methods
    # ========================================================

    def detect_anomalous_rebates(self) -> list[dict]:
        """
        Trova gare con ribassi anomali rispetto alla media del settore.
        """
        anomalies = []
        sector_stats = self._compute_sector_stats()

        with self._get_anac_conn() as conn:
            rows = conn.execute("""
                SELECT *
                FROM anac_tenders
                WHERE rebate_percentage IS NOT NULL
                  AND cpv_code IS NOT NULL
                  AND cpv_code != ''
            """).fetchall()

        threshold = THRESHOLDS["rebate_std_threshold"]

        for row in rows:
            sector = row["cpv_code"][:2] if row["cpv_code"] else None
            if not sector or sector not in sector_stats:
                continue

            stats = sector_stats[sector]
            rebate = row["rebate_percentage"]
            z = calculate_z_score(rebate, stats["mean"], stats["std"])

            if abs(z) > threshold:
                # Ribasso anomalo
                direction = "alto" if z > 0 else "basso"
                raw_score = abs(z) * ANOMALY_WEIGHTS["RIBASSO_ANOMALO"]

                anomalies.append({
                    "cig": row["cig"],
                    "anomaly_type": "RIBASSO_ANOMALO",
                    "score": normalize_score(raw_score),
                    "description": (
                        f"Ribasso {direction} del {rebate:.1f}% "
                        f"(media settore: {stats['mean']:.1f}%, z-score: {z:.2f})"
                    ),
                    "data": {
                        "rebate": rebate,
                        "sector_mean": stats["mean"],
                        "sector_std": stats["std"],
                        "z_score": z,
                        "tender_title": row["title"],
                        "buyer_name": row["buyer_name"],
                        "supplier_name": row["supplier_name"],
                        "amount": row["amount_value"],
                    }
                })

        return anomalies

    def detect_zero_competition(self) -> list[dict]:
        """
        Trova gare con una sola offerta (zero concorrenza).
        """
        anomalies = []

        with self._get_anac_conn() as conn:
            rows = conn.execute("""
                SELECT *
                FROM anac_tenders
                WHERE number_of_tenderers = 1
                  AND procurement_method IN ('open', 'selective')
                  AND amount_value >= 50000
            """).fetchall()

        for row in rows:
            # Score basato sull'importo (piu alto = piu sospetto)
            amount = row["amount_value"] or 0
            if amount >= 1000000:
                raw_score = 1.0
            elif amount >= 500000:
                raw_score = 0.8
            elif amount >= 100000:
                raw_score = 0.6
            else:
                raw_score = 0.4

            raw_score *= ANOMALY_WEIGHTS["ZERO_CONCORRENZA"]

            anomalies.append({
                "cig": row["cig"],
                "anomaly_type": "ZERO_CONCORRENZA",
                "score": normalize_score(raw_score, max_score=1.0),
                "description": (
                    f"Gara con unica offerta (importo: {amount:,.0f} EUR)"
                ),
                "data": {
                    "tender_title": row["title"],
                    "buyer_name": row["buyer_name"],
                    "supplier_name": row["supplier_name"],
                    "amount": amount,
                    "procurement_method": row["procurement_method"],
                }
            })

        return anomalies

    def detect_recurring_winners(self) -> list[dict]:
        """
        Trova fornitori che vincono percentuale anomala di gare da stesso ente.
        """
        anomalies = []
        bs_stats = self._compute_buyer_supplier_stats()

        threshold_pct = THRESHOLDS["recurring_winner_pct"]
        min_tenders = THRESHOLDS["recurring_winner_min_tenders"]

        for key, stats in bs_stats.items():
            if (stats["win_percentage"] >= threshold_pct and
                stats["win_count"] >= min_tenders):

                # Score proporzionale alla percentuale
                raw_score = (stats["win_percentage"] / 100) * ANOMALY_WEIGHTS["VINCITORE_RICORRENTE"]

                anomalies.append({
                    "cig": None,  # Non specifico di una gara
                    "anomaly_type": "VINCITORE_RICORRENTE",
                    "score": normalize_score(raw_score, max_score=1.0),
                    "description": (
                        f"{stats['supplier_name']} vince {stats['win_percentage']:.0f}% "
                        f"delle gare di {stats['buyer_name']} "
                        f"({stats['win_count']}/{stats['buyer_total_tenders']} gare)"
                    ),
                    "data": {
                        "buyer_id": stats["buyer_id"],
                        "buyer_name": stats["buyer_name"],
                        "supplier_id": stats["supplier_id"],
                        "supplier_name": stats["supplier_name"],
                        "win_count": stats["win_count"],
                        "win_percentage": stats["win_percentage"],
                        "total_value": stats["total_value"],
                    }
                })

        return anomalies

    def detect_variant_excessive(self) -> list[dict]:
        """
        Trova gare dove l'importo finale supera significativamente la base.
        """
        anomalies = []
        threshold = THRESHOLDS["variant_threshold_pct"]

        with self._get_anac_conn() as conn:
            rows = conn.execute("""
                SELECT *
                FROM anac_tenders
                WHERE estimated_value IS NOT NULL
                  AND amount_value IS NOT NULL
                  AND estimated_value > 0
                  AND amount_value > estimated_value
            """).fetchall()

        for row in rows:
            ratio = (row["amount_value"] / row["estimated_value"]) * 100
            if ratio >= threshold:
                raw_score = ((ratio - 100) / 100) * ANOMALY_WEIGHTS["VARIANTE_ECCESSIVA"]

                anomalies.append({
                    "cig": row["cig"],
                    "anomaly_type": "VARIANTE_ECCESSIVA",
                    "score": normalize_score(raw_score),
                    "description": (
                        f"Importo finale ({row['amount_value']:,.0f} EUR) "
                        f"superiore del {ratio - 100:.0f}% alla base "
                        f"({row['estimated_value']:,.0f} EUR)"
                    ),
                    "data": {
                        "tender_title": row["title"],
                        "buyer_name": row["buyer_name"],
                        "supplier_name": row["supplier_name"],
                        "estimated_value": row["estimated_value"],
                        "amount_value": row["amount_value"],
                        "variant_ratio": ratio,
                    }
                })

        return anomalies

    def detect_geographic_concentration(self) -> list[dict]:
        """
        Trova fornitori che vincono in modo anomalo in regioni diverse dalla sede.
        """
        anomalies = []

        with self._get_anac_conn() as conn:
            # Trova supplier che vincono spesso fuori dalla propria regione
            # (richiede che supplier_address contenga info sulla regione)
            rows = conn.execute("""
                SELECT
                    supplier_id,
                    supplier_name,
                    buyer_region,
                    COUNT(*) as win_count,
                    SUM(amount_value) as total_value
                FROM anac_tenders
                WHERE supplier_id IS NOT NULL
                  AND buyer_region IS NOT NULL
                GROUP BY supplier_id, buyer_region
                HAVING win_count >= ?
            """, (THRESHOLDS["concentration_min_tenders"],)).fetchall()

        # Raggruppa per supplier
        by_supplier: dict[str, list] = defaultdict(list)
        for row in rows:
            by_supplier[row["supplier_id"]].append(dict(row))

        threshold = THRESHOLDS["concentration_threshold_pct"]

        for supplier_id, regions in by_supplier.items():
            if len(regions) < 2:
                continue

            total_wins = sum(r["win_count"] for r in regions)

            # Controlla se c'e concentrazione anomala in una regione
            for region_data in regions:
                pct = (region_data["win_count"] / total_wins) * 100
                if pct >= threshold and region_data["win_count"] >= 3:
                    raw_score = (pct / 100) * ANOMALY_WEIGHTS["CONCENTRAZIONE_GEOGRAFICA"]

                    anomalies.append({
                        "cig": None,
                        "anomaly_type": "CONCENTRAZIONE_GEOGRAFICA",
                        "score": normalize_score(raw_score, max_score=1.0),
                        "description": (
                            f"{region_data['supplier_name']} vince {pct:.0f}% "
                            f"delle sue gare in {region_data['buyer_region']} "
                            f"({region_data['win_count']} gare, "
                            f"{region_data['total_value']:,.0f} EUR)"
                        ),
                        "data": {
                            "supplier_id": supplier_id,
                            "supplier_name": region_data["supplier_name"],
                            "region": region_data["buyer_region"],
                            "win_count": region_data["win_count"],
                            "concentration_pct": pct,
                            "total_value": region_data["total_value"],
                        }
                    })

        return anomalies

    # ========================================================
    # Main Detection
    # ========================================================

    def detect_all(
        self,
        min_score: float = 0.3,
        types: list[str] | None = None
    ) -> list[dict]:
        """
        Esegue tutte le detection e restituisce anomalie ordinate per score.

        Args:
            min_score: Score minimo per includere (0.0-1.0)
            types: Lista di tipi da rilevare (None = tutti)

        Returns:
            Lista di anomalie ordinate per score decrescente
        """
        all_anomalies = []

        detectors = {
            "RIBASSO_ANOMALO": self.detect_anomalous_rebates,
            "ZERO_CONCORRENZA": self.detect_zero_competition,
            "VINCITORE_RICORRENTE": self.detect_recurring_winners,
            "VARIANTE_ECCESSIVA": self.detect_variant_excessive,
            "CONCENTRAZIONE_GEOGRAFICA": self.detect_geographic_concentration,
        }

        for anomaly_type, detector_func in detectors.items():
            if types and anomaly_type not in types:
                continue

            try:
                anomalies = detector_func()
                all_anomalies.extend(anomalies)
            except Exception as e:
                print(f"[AnomalyDetector] Errore in {anomaly_type}: {e}")

        # Filtra per score minimo e ordina
        filtered = [a for a in all_anomalies if a["score"] >= min_score]
        filtered.sort(key=lambda x: -x["score"])

        return filtered

    def save_anomalies_to_kg(self, anomalies: list[dict]):
        """
        Salva le anomalie nel Knowledge Graph database.
        """
        if not self.kg_db.exists():
            print("[AnomalyDetector] KG database non esiste, skip save")
            return

        now = now_iso()

        with self._get_kg_conn() as conn:
            for anomaly in anomalies:
                try:
                    conn.execute("""
                        INSERT INTO anomaly_flags
                            (cig, anomaly_type, anomaly_score, description, data_json, detected_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        anomaly.get("cig"),
                        anomaly["anomaly_type"],
                        anomaly["score"],
                        anomaly["description"],
                        json.dumps(anomaly["data"], ensure_ascii=False),
                        now
                    ))
                except sqlite3.IntegrityError:
                    pass  # Duplicato
            conn.commit()

    def get_anomalies_for_entity(
        self,
        entity_name: str,
        entity_type: str = "supplier"
    ) -> list[dict]:
        """
        Trova anomalie relative a un'entita specifica.

        Args:
            entity_name: Nome entita (LIKE search)
            entity_type: "supplier" o "buyer"

        Returns:
            Lista di anomalie correlate
        """
        anomalies = []
        pattern = f"%{entity_name.upper()}%"

        with self._get_anac_conn() as conn:
            if entity_type == "supplier":
                # Cerca gare flaggate del fornitore
                rows = conn.execute("""
                    SELECT * FROM anac_tenders
                    WHERE UPPER(supplier_name) LIKE ?
                      AND flagged = 1
                """, (pattern,)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM anac_tenders
                    WHERE UPPER(buyer_name) LIKE ?
                      AND flagged = 1
                """, (pattern,)).fetchall()

            for row in rows:
                anomalies.append({
                    "cig": row["cig"],
                    "flag_reason": row["flag_reason"],
                    "tender_title": row["title"],
                    "amount": row["amount_value"],
                })

        # Cerca anche nel KG
        if self.kg_db.exists():
            with self._get_kg_conn() as conn:
                rows = conn.execute("""
                    SELECT * FROM anomaly_flags
                    WHERE data_json LIKE ?
                      AND dismissed = 0
                    ORDER BY anomaly_score DESC
                """, (pattern,)).fetchall()

                for row in rows:
                    anomalies.append({
                        "cig": row["cig"],
                        "anomaly_type": row["anomaly_type"],
                        "score": row["anomaly_score"],
                        "description": row["description"],
                    })

        return anomalies

    def export_for_pipeline(
        self,
        anomalies: list[dict],
        max_items: int = 5
    ) -> list[dict]:
        """
        Formatta le anomalie per inserimento nel backlog di pipeline.json.

        Args:
            anomalies: Lista anomalie da formattare
            max_items: Numero massimo di item da generare

        Returns:
            Lista di item pronti per pipeline["backlog"]
        """
        # Raggruppa per entita/buyer per evitare duplicati
        grouped: dict[str, list] = defaultdict(list)

        for a in anomalies:
            data = a.get("data", {})
            # Usa supplier o buyer come chiave
            key = data.get("supplier_name") or data.get("buyer_name") or a.get("cig", "unknown")
            grouped[key].append(a)

        items = []
        for key, group_anomalies in list(grouped.items())[:max_items]:
            # Prendi l'anomalia con score piu alto
            top = max(group_anomalies, key=lambda x: x["score"])
            data = top.get("data", {})

            # Genera titolo investigativo
            if "supplier_name" in data:
                title = f"Indagine: {data['supplier_name']}"
            elif "buyer_name" in data:
                title = f"Anomalie gare: {data['buyer_name']}"
            else:
                title = f"Anomalia CIG {top.get('cig', 'N/A')}"

            # Genera slug
            slug = title.lower()
            for char in " ,:;'\"()[]{}":
                slug = slug.replace(char, "-")
            slug = "-".join(filter(None, slug.split("-")))[:60]

            # Descrizione
            descriptions = [a["description"] for a in group_anomalies[:3]]

            items.append({
                "title": title,
                "slug": slug,
                "status": "idea",
                "category": "Appalti pubblici",
                "description": " | ".join(descriptions),
                "priority_score": round(top["score"] * 10),
                "research_notes": [
                    f"ANOMALIA RILEVATA: {top['anomaly_type']} (score: {top['score']:.2f})",
                    *descriptions,
                ],
                "sources": [],
                "added": datetime.now().strftime("%Y-%m-%d"),
                "anomaly_source": "aiena_anomaly_detector",
            })

        return items


# ============================================================
# Test self-contained
# ============================================================

if __name__ == "__main__":
    print("=== Test AnomalyDetector ===\n")

    detector = AnomalyDetector()

    # Prima importa dati di test
    print("1. Verifica dati ANAC disponibili...")
    fetcher = ANACFetcher()
    stats = fetcher.get_stats()
    print(f"   Gare nel database: {stats['total_tenders']}")

    if stats['total_tenders'] == 0:
        print("\n   NOTA: Database vuoto. Importo dati di test...")

        # Crea dati test con anomalie
        test_releases = []

        # Gara con zero concorrenza
        test_releases.append({
            "ocid": "ocds-test-zero-001",
            "tender": {
                "id": "ZERO001",
                "title": "Pulizia Scuole Elementari",
                "procurementMethod": "open",
                "status": "complete",
                "numberOfTenderers": 1,
                "value": {"amount": 200000},
                "items": [{"classification": {"id": "90910000", "description": "Servizi di pulizia"}}]
            },
            "awards": [{
                "date": "2025-06-01",
                "value": {"amount": 195000},
                "suppliers": [{"name": "CleanCo SRL", "identifier": {"id": "11111111111"}}]
            }],
            "parties": [{"name": "Comune di Testville", "roles": ["buyer"], "identifier": {"id": "TESTCOMUNE"}}]
        })

        # Gara con ribasso anomalo
        test_releases.append({
            "ocid": "ocds-test-rebate-001",
            "tender": {
                "id": "REBATE001",
                "title": "Manutenzione Edifici Comunali",
                "procurementMethod": "open",
                "status": "complete",
                "numberOfTenderers": 5,
                "value": {"amount": 500000},
                "items": [{"classification": {"id": "45000000", "description": "Lavori edili"}}]
            },
            "awards": [{
                "date": "2025-05-15",
                "value": {"amount": 200000},  # 60% ribasso!
                "suppliers": [{"name": "Costruzioni Anomale SRL", "identifier": {"id": "22222222222"}}]
            }],
            "parties": [{"name": "Provincia di Testlandia", "roles": ["buyer"], "identifier": {"id": "TESTPROV"}}]
        })

        # Importa
        import json
        fetcher.import_json_data(
            json.dumps({"releases": test_releases}).encode(),
            "test://anomaly"
        )
        print(f"   Importati {len(test_releases)} record di test")

    # Esegui detection
    print("\n2. Rilevamento anomalie...")
    anomalies = detector.detect_all(min_score=0.0)
    print(f"   Trovate {len(anomalies)} anomalie totali")

    # Mostra top 5
    print("\n3. Top 5 anomalie per score:")
    for i, a in enumerate(anomalies[:5], 1):
        print(f"   {i}. [{a['anomaly_type']}] Score: {a['score']:.2f}")
        print(f"      {a['description'][:80]}...")
        if a.get("cig"):
            print(f"      CIG: {a['cig']}")

    # Genera items per pipeline
    print("\n4. Generazione items per pipeline.json:")
    items = detector.export_for_pipeline(anomalies, max_items=3)
    for item in items:
        print(f"   - {item['title']}")
        print(f"     Priority: {item['priority_score']}/10")

    print("\n=== Test completati ===")
