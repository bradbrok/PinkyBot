#!/usr/bin/env python3
"""
AIena ANAC-KG Bridge — Collega anomalie ANAC al Knowledge Graph.

Prende le anomalie rilevate dall'AnomalyDetector e:
1. Crea/aggiorna entita nel KG (buyer -> ENTE_PUBBLICO, supplier -> AZIENDA)
2. Aggiunge flag anomalia alle entita coinvolte
3. Crea relazioni VINCE tra supplier e buyer
4. Crea entita APPALTO per ogni CIG e relazioni RICEVE/VINCE

Uso:
    python aiena_anac_kg_bridge.py              # Esegue sync
    python aiena_anac_kg_bridge.py --dry-run    # Solo preview
    python aiena_anac_kg_bridge.py --min-score 0.5  # Soglia score
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Path setup per import locali
sys.path.insert(0, str(Path(__file__).parent.parent))

from aiena_kg.aiena_kg import KnowledgeGraph, normalize_name, levenshtein_distance
from aiena_anac.aiena_anomaly_detector import AnomalyDetector

# Database paths
KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")

# Fuzzy match threshold: distanza Levenshtein massima per considerare match
FUZZY_THRESHOLD = 3


def now_iso() -> str:
    """Timestamp ISO corrente in UTC."""
    return datetime.now(timezone.utc).isoformat()


def log(msg: str):
    """Log con timestamp."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


class AnacKgBridge:
    """
    Bridge tra ANAC Anomaly Detector e Knowledge Graph.

    Sincronizza le anomalie rilevate creando/aggiornando entita,
    relazioni e flag nel Knowledge Graph.
    """

    def __init__(
        self,
        kg_db_path: Path | str | None = None,
        anac_db_path: Path | str | None = None
    ):
        """
        Inizializza il bridge.

        Args:
            kg_db_path: Path al database KG (default: KG_DB_PATH)
            anac_db_path: Path al database ANAC (default: quello del detector)
        """
        self.kg = KnowledgeGraph(kg_db_path or KG_DB_PATH)
        self.detector = AnomalyDetector(anac_db_path=anac_db_path)

        # Stats
        self.stats = {
            "entities_created": 0,
            "entities_found": 0,
            "flags_added": 0,
            "relations_created": 0,
            "appalti_created": 0,
            "anomalies_processed": 0,
            "anomalies_skipped": 0,
        }

    def _find_or_create_entity(
        self,
        name: str,
        entity_type: str,
        metadata: dict[str, Any] | None = None,
        dry_run: bool = False
    ) -> int | None:
        """
        Cerca un'entita nel KG con fuzzy match, o la crea se non esiste.

        Args:
            name: Nome dell'entita
            entity_type: Tipo (ENTE_PUBBLICO, AZIENDA, APPALTO)
            metadata: Metadati aggiuntivi
            dry_run: Se True, non modifica il DB

        Returns:
            ID dell'entita trovata/creata, None se dry_run
        """
        if not name or not name.strip():
            return None

        # Cerca con fuzzy match
        results = self.kg.search_entity(name, entity_type=entity_type, limit=1)

        if results:
            best_match = results[0]
            fuzzy_dist = best_match.get("_fuzzy_distance", 0)

            # Se non ha _fuzzy_distance, e un match esatto da LIKE
            if fuzzy_dist == 0 or (fuzzy_dist is not None and fuzzy_dist <= FUZZY_THRESHOLD):
                log(f"  Trovata entita esistente: {best_match['name']} (dist={fuzzy_dist})")
                self.stats["entities_found"] += 1
                return best_match["id"]

        # Non trovata, crea nuova
        if dry_run:
            log(f"  [DRY-RUN] Creerebbe entita: {name} ({entity_type})")
            self.stats["entities_created"] += 1
            return None

        entity_id = self.kg.add_entity(
            name=name,
            entity_type=entity_type,
            metadata=metadata,
            confidence=0.8  # Confidenza da ANAC
        )
        log(f"  Creata entita: {name} ({entity_type}) -> ID {entity_id}")
        self.stats["entities_created"] += 1

        # Aggiungi fonte ANAC
        self.kg.add_entity_source(
            entity_id=entity_id,
            source_type="ANAC",
            source_name="ANAC Anomaly Detector",
            info_type="ESISTENZA",
            info_value=f"Rilevato in anomalie ANAC",
            confidence=0.8
        )

        return entity_id

    def _add_anomaly_flag(
        self,
        entity_id: int | None,
        entity_name: str,
        anomaly: dict,
        dry_run: bool = False
    ) -> bool:
        """
        Aggiunge un flag anomalia alla tabella anomaly_flags.

        Args:
            entity_id: ID dell'entita nel KG (puo essere None)
            entity_name: Nome dell'entita (per logging)
            anomaly: Dict anomalia dal detector
            dry_run: Se True, non modifica il DB

        Returns:
            True se aggiunto, False se duplicato o errore
        """
        cig = anomaly.get("cig")
        anomaly_type = anomaly["anomaly_type"]
        score = anomaly["score"]
        description = anomaly["description"]
        data = anomaly.get("data", {})

        if dry_run:
            log(f"  [DRY-RUN] Aggiungerebbe flag {anomaly_type} (score={score:.2f}) a {entity_name}")
            self.stats["flags_added"] += 1
            return True

        # Inserisci in anomaly_flags
        now = now_iso()
        data_json = json.dumps(data, ensure_ascii=False)

        try:
            with self.kg._get_conn() as conn:
                # Controlla duplicati (stessa entita, stesso tipo, stesso CIG se presente)
                if entity_id:
                    existing = conn.execute("""
                        SELECT id FROM anomaly_flags
                        WHERE entity_id = ? AND anomaly_type = ?
                          AND (cig IS NULL OR cig = ?)
                    """, (entity_id, anomaly_type, cig)).fetchone()
                else:
                    existing = conn.execute("""
                        SELECT id FROM anomaly_flags
                        WHERE cig = ? AND anomaly_type = ?
                    """, (cig, anomaly_type)).fetchone() if cig else None

                if existing:
                    log(f"  Flag {anomaly_type} gia presente per {entity_name}")
                    return False

                conn.execute("""
                    INSERT INTO anomaly_flags
                        (entity_id, cig, anomaly_type, anomaly_score, description, data_json, detected_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (entity_id, cig, anomaly_type, score, description, data_json, now))
                conn.commit()

            log(f"  Aggiunto flag {anomaly_type} (score={score:.2f}) a {entity_name}")
            self.stats["flags_added"] += 1
            return True

        except sqlite3.IntegrityError as e:
            log(f"  Errore inserimento flag: {e}")
            return False

    def _create_relation(
        self,
        entity1_name: str,
        entity2_name: str,
        relation_type: str,
        source_ref: str | None = None,
        description: str | None = None,
        dry_run: bool = False
    ) -> int | None:
        """
        Crea una relazione tra due entita.

        Args:
            entity1_name: Nome prima entita
            entity2_name: Nome seconda entita
            relation_type: Tipo relazione (VINCE, RICEVE)
            source_ref: Riferimento fonte (es. CIG)
            description: Descrizione relazione
            dry_run: Se True, non modifica il DB

        Returns:
            ID relazione creata, None se dry_run o errore
        """
        if dry_run:
            log(f"  [DRY-RUN] Creerebbe relazione {entity1_name} --{relation_type}--> {entity2_name}")
            self.stats["relations_created"] += 1
            return None

        try:
            rel_id = self.kg.add_relation(
                entity1_name=entity1_name,
                entity2_name=entity2_name,
                relation_type=relation_type,
                source_type="ANAC",
                source_url=source_ref,
                description=description,
                confidence=0.9
            )
            if rel_id:
                log(f"  Creata relazione: {entity1_name} --{relation_type}--> {entity2_name}")
                self.stats["relations_created"] += 1
            return rel_id
        except Exception as e:
            log(f"  Errore creazione relazione: {e}")
            return None

    def run(
        self,
        min_score: float = 0.3,
        dry_run: bool = False,
        limit: int | None = None
    ) -> dict:
        """
        Esegue la sincronizzazione ANAC -> KG.

        Args:
            min_score: Score minimo per processare anomalie
            dry_run: Se True, non modifica i database
            limit: Numero massimo anomalie da processare (None = tutte)

        Returns:
            Dict con statistiche del run
        """
        # Reset stats
        self.stats = {
            "entities_created": 0,
            "entities_found": 0,
            "flags_added": 0,
            "relations_created": 0,
            "appalti_created": 0,
            "anomalies_processed": 0,
            "anomalies_skipped": 0,
        }

        log("=" * 60)
        log("ANAC-KG Bridge - Inizio sincronizzazione")
        log(f"  min_score: {min_score}")
        log(f"  dry_run: {dry_run}")
        log("=" * 60)

        # Ottieni anomalie
        log("Recupero anomalie da AnomalyDetector...")
        anomalies = self.detector.detect_all(min_score=min_score)
        log(f"Trovate {len(anomalies)} anomalie con score >= {min_score}")

        if limit:
            anomalies = anomalies[:limit]
            log(f"Limitato a {limit} anomalie")

        # Processa ogni anomalia
        for i, anomaly in enumerate(anomalies, 1):
            log(f"\n[{i}/{len(anomalies)}] {anomaly['anomaly_type']} (score={anomaly['score']:.2f})")
            log(f"  {anomaly['description'][:100]}...")

            data = anomaly.get("data", {})
            buyer_name = data.get("buyer_name", "").strip()
            supplier_name = data.get("supplier_name", "").strip()
            cig = anomaly.get("cig")

            # Skip se mancano entrambi buyer e supplier
            if not buyer_name and not supplier_name:
                log("  Skip: mancano buyer_name e supplier_name")
                self.stats["anomalies_skipped"] += 1
                continue

            self.stats["anomalies_processed"] += 1

            buyer_id = None
            supplier_id = None

            # 1. Gestisci buyer (ENTE_PUBBLICO)
            if buyer_name:
                buyer_metadata = {
                    "buyer_id": data.get("buyer_id"),
                    "source": "ANAC",
                }
                buyer_id = self._find_or_create_entity(
                    name=buyer_name,
                    entity_type="ENTE_PUBBLICO",
                    metadata=buyer_metadata,
                    dry_run=dry_run
                )

                # Aggiungi flag al buyer se e l'entita principale dell'anomalia
                if buyer_id and not supplier_name:
                    self._add_anomaly_flag(
                        entity_id=buyer_id,
                        entity_name=buyer_name,
                        anomaly=anomaly,
                        dry_run=dry_run
                    )

            # 2. Gestisci supplier (AZIENDA)
            if supplier_name:
                supplier_metadata = {
                    "supplier_id": data.get("supplier_id"),
                    "source": "ANAC",
                }
                if data.get("win_count"):
                    supplier_metadata["win_count"] = data["win_count"]
                if data.get("total_value"):
                    supplier_metadata["total_value"] = data["total_value"]

                supplier_id = self._find_or_create_entity(
                    name=supplier_name,
                    entity_type="AZIENDA",
                    metadata=supplier_metadata,
                    dry_run=dry_run
                )

                # Aggiungi flag al supplier
                if supplier_id:
                    self._add_anomaly_flag(
                        entity_id=supplier_id,
                        entity_name=supplier_name,
                        anomaly=anomaly,
                        dry_run=dry_run
                    )

            # 3. Se entrambi presenti, crea relazione VINCE
            if buyer_name and supplier_name:
                rel_desc = f"Anomalia {anomaly['anomaly_type']}"
                if cig:
                    rel_desc += f" (CIG: {cig})"

                self._create_relation(
                    entity1_name=supplier_name,
                    entity2_name=buyer_name,
                    relation_type="VINCE",
                    source_ref=f"CIG:{cig}" if cig else "ANAC-anomaly",
                    description=rel_desc,
                    dry_run=dry_run
                )

            # 4. Se c'e un CIG, crea entita APPALTO e relazioni
            if cig:
                tender_title = data.get("tender_title", "")
                amount = data.get("amount") or data.get("amount_value")

                appalto_metadata = {
                    "title": tender_title,
                    "amount": amount,
                    "anomaly_type": anomaly["anomaly_type"],
                    "anomaly_score": anomaly["score"],
                }

                appalto_name = f"CIG:{cig}"
                appalto_id = self._find_or_create_entity(
                    name=appalto_name,
                    entity_type="APPALTO",
                    metadata=appalto_metadata,
                    dry_run=dry_run
                )

                if appalto_id or dry_run:
                    self.stats["appalti_created"] += 1

                    # Relazione buyer RICEVE appalto
                    if buyer_name:
                        self._create_relation(
                            entity1_name=buyer_name,
                            entity2_name=appalto_name,
                            relation_type="RICEVE",
                            source_ref=f"CIG:{cig}",
                            description=f"Ente riceve appalto {tender_title[:50] if tender_title else cig}",
                            dry_run=dry_run
                        )

                    # Relazione supplier VINCE appalto
                    if supplier_name:
                        self._create_relation(
                            entity1_name=supplier_name,
                            entity2_name=appalto_name,
                            relation_type="VINCE",
                            source_ref=f"CIG:{cig}",
                            description=f"Fornitore vince appalto {tender_title[:50] if tender_title else cig}",
                            dry_run=dry_run
                        )

                    # Aggiungi flag all'appalto
                    self._add_anomaly_flag(
                        entity_id=appalto_id,
                        entity_name=appalto_name,
                        anomaly=anomaly,
                        dry_run=dry_run
                    )

        # Report finale
        log("\n" + "=" * 60)
        log("STATISTICHE FINALI")
        log("=" * 60)
        log(f"  Anomalie processate:  {self.stats['anomalies_processed']}")
        log(f"  Anomalie skippate:    {self.stats['anomalies_skipped']}")
        log(f"  Entita create:        {self.stats['entities_created']}")
        log(f"  Entita trovate:       {self.stats['entities_found']}")
        log(f"  Flag aggiunti:        {self.stats['flags_added']}")
        log(f"  Relazioni create:     {self.stats['relations_created']}")
        log(f"  Appalti creati:       {self.stats['appalti_created']}")

        if dry_run:
            log("\n[DRY-RUN] Nessuna modifica effettuata ai database")

        log("=" * 60)

        return self.stats


def main():
    """Entry point CLI."""
    parser = argparse.ArgumentParser(
        description="Bridge ANAC Anomaly Detector -> Knowledge Graph",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  %(prog)s                     # Sync completo
  %(prog)s --dry-run           # Preview senza modifiche
  %(prog)s --min-score 0.5     # Solo anomalie con score >= 0.5
  %(prog)s --limit 10          # Processa solo 10 anomalie
        """
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stampa cosa farebbe senza modificare i database"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.3,
        help="Score minimo per processare anomalie (default: 0.3)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Numero massimo di anomalie da processare"
    )
    parser.add_argument(
        "--kg-db",
        type=str,
        default=None,
        help=f"Path al database KG (default: {KG_DB_PATH})"
    )
    parser.add_argument(
        "--anac-db",
        type=str,
        default=None,
        help="Path al database ANAC cache"
    )

    args = parser.parse_args()

    bridge = AnacKgBridge(
        kg_db_path=args.kg_db,
        anac_db_path=args.anac_db
    )

    stats = bridge.run(
        min_score=args.min_score,
        dry_run=args.dry_run,
        limit=args.limit
    )

    # Exit code basato su risultati
    if stats["anomalies_processed"] == 0:
        sys.exit(1)  # Nessuna anomalia processata
    sys.exit(0)


if __name__ == "__main__":
    main()
