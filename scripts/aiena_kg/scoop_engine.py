#!/usr/bin/env python3
"""
AIena Scoop Engine (C8) — mina il Knowledge Graph per leads investigativi.

Pipeline:
    1. Esegue 5 pattern investigativi (P1..P5) su KG + ANAC
    2. Boost score per entita gia in pipeline.json (P6)
    3. Deduplica leads sullo stesso cluster di entita
    4. Mergia con leads.json esistente (preserva status != "new")
    5. Scrive output in /var/www/aiena.it/data/leads.json

Pattern:
    P1 ANOMALY_PEP        - azienda con anomalia ANAC + admin in OpenSanctions
    P2 HIDDEN_GROUP       - stesso admin in N aziende che vincono dallo stesso ente
    P3 AZIENDA_NEONATA    - azienda neonata flaggata dall'enricher
    P4 RECIDIVO           - entita con >=2 anomaly_flags di tipo diverso
    P5 CONCENTRAZIONE     - fornitore riceve >30% degli appalti da un singolo ente
    P6 PIPELINE_MATCH     - boost se entita gia menzionata in pipeline.json

Uso:
    python3 scoop_engine.py
    python3 scoop_engine.py --dry-run
    python3 scoop_engine.py --min-score 0.4
    python3 scoop_engine.py --pattern P1,P2
    python3 scoop_engine.py --stats

Decisioni architetturali:
- Robust to KG sparse: ogni pattern logga e ritorna [] se dati insufficienti
- Mai genera leads fasulli: zero relazioni -> zero leads (P1/P2/P5)
- Idempotente: rieseguire e' sicuro (merge per ID)
- title_seed sempre con linguaggio investigativo (mai accuse dirette)
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ============================================================
# Path setup
# ============================================================

SCRIPTS_DIR = Path(__file__).resolve().parent.parent  # -> scripts/
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from aiena_kg.aiena_kg import KnowledgeGraph, normalize_name  # noqa: E402
from aiena_anac.aiena_anomaly_detector import AnomalyDetector  # noqa: E402

# ============================================================
# Costanti
# ============================================================

KG_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")
ANAC_DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_anac_cache.db")
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
LEADS_JSON = Path("/var/www/aiena.it/data/leads.json")

# Score base per pattern (range, non punto fisso)
PATTERN_SCORES = {
    "ANOMALY_PEP": (0.85, 0.95),
    "HIDDEN_GROUP": (0.70, 0.85),
    "AZIENDA_NEONATA": (0.60, 0.75),
    "RECIDIVO": (0.70, 0.80),
    "CONCENTRAZIONE_VALORE": (0.50, 0.70),
}

PIPELINE_BOOST = 0.20
SCORE_CAP = 1.0
CONCENTRATION_THRESHOLD = 0.30  # 30% del valore totale
HIDDEN_GROUP_MIN_COMPANIES = 2
RECIDIVO_MIN_FLAGS = 2

# Pattern che richiedono review umana per default
HUMAN_REVIEW_PATTERNS = {"ANOMALY_PEP"}

logger = logging.getLogger("scoop_engine")
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def strip_html(text: Any) -> str:
    """Rimuove tag HTML basilari. Accetta anche list/dict (best-effort flatten)."""
    if not text:
        return ""
    if isinstance(text, list):
        text = " ".join(strip_html(x) for x in text)
    elif isinstance(text, dict):
        text = " ".join(strip_html(v) for v in text.values())
    elif not isinstance(text, str):
        text = str(text)
    return re.sub(r"<[^>]+>", " ", text)


def normalize_for_match(text: str) -> str:
    """Normalizza testo per matching cross-source (lowercase, no diacritici)."""
    if not text:
        return ""
    n = unicodedata.normalize("NFD", text)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    return " ".join(n.lower().split())


def clamp(value: float, lo: float = 0.0, hi: float = SCORE_CAP) -> float:
    return max(lo, min(hi, value))


# ============================================================
# Lead dataclass
# ============================================================


@dataclass
class Lead:
    id: str
    pattern: str
    scoop_score: float
    title_seed: str
    entities: list[str]
    evidence: list[dict] = field(default_factory=list)
    suggested_angle: str = ""
    linked_investigation: str | None = None
    human_review_required: bool = False
    generated_at: str = field(default_factory=now_iso)
    status: str = "new"

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================
# Scoop Engine
# ============================================================


class ScoopEngine:
    """Genera leads investigativi minando il Knowledge Graph."""

    def __init__(
        self,
        kg_db_path: Path | str | None = None,
        pipeline_path: Path | str | None = None,
    ):
        self.kg = KnowledgeGraph(Path(kg_db_path) if kg_db_path else KG_DB_PATH)
        self.detector = AnomalyDetector()
        self.pipeline_path = Path(pipeline_path) if pipeline_path else PIPELINE_JSON

        # Cache per evitare riletture
        self._pipeline_entities: dict[str, str] | None = None  # norm_name -> slug
        self._kg_stats: dict[str, int] | None = None

    # --------------------------------------------------------
    # KG stats helpers
    # --------------------------------------------------------

    def _stats(self) -> dict[str, int]:
        if self._kg_stats is not None:
            return self._kg_stats
        with self.kg._get_conn() as conn:
            self._kg_stats = {
                "entities": conn.execute(
                    "SELECT COUNT(*) FROM entities"
                ).fetchone()[0],
                "relations": conn.execute(
                    "SELECT COUNT(*) FROM relations"
                ).fetchone()[0],
                "anomalies": conn.execute(
                    "SELECT COUNT(*) FROM anomaly_flags"
                ).fetchone()[0],
                "suspicious": conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE is_suspicious = 1"
                ).fetchone()[0],
            }
        return self._kg_stats

    def _need_relations(self, pattern: str, n: int = 1) -> bool:
        s = self._stats()
        if s["relations"] < n:
            logger.info(
                f"KG insufficiente per pattern {pattern} "
                f"({s['entities']} entita, {s['relations']} relazioni)"
            )
            return False
        return True

    def _need_anomalies(self, pattern: str, n: int = 1) -> bool:
        s = self._stats()
        if s["anomalies"] < n:
            logger.info(
                f"KG insufficiente per pattern {pattern} "
                f"({s['anomalies']} anomalie nel KG)"
            )
            return False
        return True

    # --------------------------------------------------------
    # Pipeline.json: estrae entita gia in indagine
    # --------------------------------------------------------

    def _load_pipeline_entities(self) -> dict[str, str]:
        """
        Estrae nomi entita citati nelle inchieste attive di pipeline.json.
        Ritorna {normalized_name: slug}.

        Estrae da: backlog[], pipeline[], current_investigation, published[]
        Cerca match con entities nel KG (per nome normalizzato).
        """
        if self._pipeline_entities is not None:
            return self._pipeline_entities

        result: dict[str, str] = {}

        if not self.pipeline_path.exists():
            logger.info(f"pipeline.json non trovato: {self.pipeline_path}")
            self._pipeline_entities = result
            return result

        try:
            data = json.loads(self.pipeline_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"pipeline.json non leggibile: {e}")
            self._pipeline_entities = result
            return result

        # Carica nomi entita dal KG (normalized lowercase senza diacritici)
        kg_entity_names: list[tuple[str, str]] = []  # (norm_lower, original_name)
        try:
            with self.kg._get_conn() as conn:
                rows = conn.execute(
                    "SELECT name FROM entities WHERE LENGTH(name) >= 4"
                ).fetchall()
                for r in rows:
                    nm = r["name"]
                    kg_entity_names.append((normalize_for_match(nm), nm))
        except sqlite3.Error as e:
            logger.warning(f"Lettura entities KG fallita: {e}")
            self._pipeline_entities = result
            return result

        if not kg_entity_names:
            self._pipeline_entities = result
            return result

        # Funzione per processare una sezione (lista o singolo dict)
        def scan_item(item: dict) -> None:
            slug = item.get("slug") or "unknown"
            haystack_parts = [
                item.get("title") or "",
                strip_html(item.get("diary_text") or ""),
                strip_html(item.get("diary") or ""),
            ]
            # research_notes puo essere lista di stringhe o dict
            for note in item.get("research_notes") or []:
                if isinstance(note, str):
                    haystack_parts.append(strip_html(note))
                elif isinstance(note, dict):
                    haystack_parts.append(strip_html(str(note)))
            # sources
            for src in item.get("sources") or []:
                if isinstance(src, str):
                    haystack_parts.append(src)
                elif isinstance(src, dict):
                    haystack_parts.append(
                        f"{src.get('title', '')} {src.get('url', '')}"
                    )

            haystack = normalize_for_match(" ".join(haystack_parts))
            if not haystack:
                return

            for norm_lower, original_name in kg_entity_names:
                if not norm_lower or len(norm_lower) < 4:
                    continue
                # Match esatto come substring (con boundary best-effort)
                # Per evitare false positives su token corti, match solo se norm_lower
                # appare come parola/sottofrase
                if norm_lower in haystack:
                    # Tieni il primo slug visto (priorita: ordine di scan)
                    if original_name not in result:
                        result[original_name] = slug

        # Scan delle sezioni (pipeline.json v2)
        for section_key in ("leads", "investigations", "published"):
            section = data.get(section_key) or []
            if isinstance(section, list):
                for item in section:
                    if isinstance(item, dict):
                        scan_item(item)
        # In v2 non ci sono piu current_investigation/next_investigation come chiavi singole

        logger.info(
            f"Pipeline.json: {len(result)} entita KG menzionate in inchieste attive"
        )
        self._pipeline_entities = result
        return result

    # --------------------------------------------------------
    # P1: ANOMALY_PEP
    # --------------------------------------------------------

    def _find_anomaly_pep(self) -> list[Lead]:
        """
        Azienda con anomalia ANAC il cui amministratore e' in OpenSanctions.

        Query: anomaly_flags (azienda) JOIN relations AMMINISTRA JOIN entities (persona suspicious)
        """
        if not self._need_relations("P1 ANOMALY_PEP"):
            return []
        if not self._need_anomalies("P1 ANOMALY_PEP"):
            return []

        leads: list[Lead] = []
        with self.kg._get_conn() as conn:
            # Cerca persone con anomaly OPENSANCTIONS_HIT
            sql = """
                SELECT DISTINCT
                    azienda.id          AS azienda_id,
                    azienda.name        AS azienda_name,
                    azienda.partita_iva AS azienda_piva,
                    persona.id          AS persona_id,
                    persona.name        AS persona_name,
                    af_az.anomaly_type  AS azienda_anomaly_type,
                    af_az.anomaly_score AS azienda_anomaly_score,
                    af_az.description   AS azienda_anomaly_desc,
                    af_pe.description   AS persona_sanction_desc,
                    af_pe.data_json     AS persona_sanction_data
                FROM anomaly_flags af_az
                JOIN entities azienda
                    ON azienda.id = af_az.entity_id
                    AND azienda.entity_type = 'AZIENDA'
                JOIN relations r
                    ON r.entity2_id = azienda.id
                    AND r.relation_type = 'AMMINISTRA'
                JOIN entities persona
                    ON persona.id = r.entity1_id
                    AND persona.entity_type = 'PERSONA'
                JOIN anomaly_flags af_pe
                    ON af_pe.entity_id = persona.id
                    AND af_pe.anomaly_type = 'OPENSANCTIONS_HIT'
                WHERE af_az.anomaly_type != 'OPENSANCTIONS_HIT'
                  AND COALESCE(af_az.dismissed, 0) = 0
                  AND COALESCE(af_pe.dismissed, 0) = 0
                ORDER BY af_az.anomaly_score DESC, af_pe.anomaly_score DESC
            """
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"P1 query error: {e}")
                return []

        score_lo, score_hi = PATTERN_SCORES["ANOMALY_PEP"]
        seen_pairs: set[tuple[str, str]] = set()

        for r in rows:
            pair = (r["azienda_name"], r["persona_name"])
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            anac_score = float(r["azienda_anomaly_score"] or 0.0)
            # Score scala con anomaly score ANAC
            score = clamp(score_lo + (score_hi - score_lo) * anac_score)

            azienda = r["azienda_name"]
            persona = r["persona_name"]
            anom_type = r["azienda_anomaly_type"]
            piva = r["azienda_piva"]

            title_seed = (
                f"I dati pubblici mostrano che {azienda} presenta un'anomalia "
                f"ANAC ({anom_type}) e che il suo amministratore {persona} "
                f"risulta in OpenSanctions: i due fatti, presi insieme, "
                f"meritano un approfondimento."
            )

            evidence = [
                {
                    "source": "ANAC",
                    "description": (
                        f"{anom_type} su {azienda}: {r['azienda_anomaly_desc']}"
                    ),
                    "url": (
                        f"https://dati.anticorruzione.it/superset/dashboard/aggiudicatari/"
                        f"?q={piva}"
                        if piva
                        else ""
                    ),
                },
                {
                    "source": "OPENSANCTIONS",
                    "description": (
                        r["persona_sanction_desc"] or f"Match OpenSanctions per {persona}"
                    ),
                    "url": "https://www.opensanctions.org/",
                },
            ]

            lead = Lead(
                id="",  # assegnato in save
                pattern="ANOMALY_PEP",
                scoop_score=score,
                title_seed=title_seed,
                entities=[azienda, persona],
                evidence=evidence,
                suggested_angle=(
                    f"Verificare se l'attivita di {azienda} verso enti pubblici "
                    f"abbia tratto vantaggio dal profilo di {persona}: "
                    f"controllare gare vinte, importi, e relazioni con il buyer."
                ),
                human_review_required=True,
            )
            leads.append(lead)

        if leads:
            logger.info(f"P1 ANOMALY_PEP: {len(leads)} leads")
        return leads

    # --------------------------------------------------------
    # P2: HIDDEN_GROUP
    # --------------------------------------------------------

    def _find_hidden_group(self) -> list[Lead]:
        """
        Stesso amministratore in >=2 aziende che vincono dallo stesso ente.

        Strategia:
            1. trova persone con N relazioni AMMINISTRA verso AZIENDA distinte
            2. per ognuna, trova buyer comuni tra quelle aziende (relations VINCE)
        """
        if not self._need_relations("P2 HIDDEN_GROUP", n=2):
            return []

        leads: list[Lead] = []

        with self.kg._get_conn() as conn:
            # Persone che amministrano >=2 aziende
            sql_admins = """
                SELECT
                    persona.id   AS persona_id,
                    persona.name AS persona_name,
                    GROUP_CONCAT(azienda.id)   AS azienda_ids,
                    GROUP_CONCAT(azienda.name, '|') AS azienda_names,
                    COUNT(DISTINCT azienda.id) AS n_aziende
                FROM relations r
                JOIN entities persona ON persona.id = r.entity1_id AND persona.entity_type = 'PERSONA'
                JOIN entities azienda ON azienda.id = r.entity2_id AND azienda.entity_type = 'AZIENDA'
                WHERE r.relation_type = 'AMMINISTRA'
                GROUP BY persona.id
                HAVING n_aziende >= ?
            """
            try:
                admin_rows = conn.execute(
                    sql_admins, (HIDDEN_GROUP_MIN_COMPANIES,)
                ).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"P2 query admins error: {e}")
                return []

            if not admin_rows:
                logger.info(
                    "P2 HIDDEN_GROUP: nessuna persona amministra >=2 aziende"
                )
                return []

            score_lo, score_hi = PATTERN_SCORES["HIDDEN_GROUP"]

            for adm in admin_rows:
                persona_name = adm["persona_name"]
                azienda_ids = [
                    int(x) for x in (adm["azienda_ids"] or "").split(",") if x.strip()
                ]
                azienda_names = [
                    n for n in (adm["azienda_names"] or "").split("|") if n.strip()
                ]
                if len(azienda_ids) < HIDDEN_GROUP_MIN_COMPANIES:
                    continue

                # Trova buyer comuni
                placeholders = ",".join("?" * len(azienda_ids))
                sql_buyers = f"""
                    SELECT
                        buyer.id   AS buyer_id,
                        buyer.name AS buyer_name,
                        COUNT(DISTINCT r.entity1_id) AS n_aziende_che_vincono
                    FROM relations r
                    JOIN entities buyer ON buyer.id = r.entity2_id
                    WHERE r.relation_type = 'VINCE'
                      AND r.entity1_id IN ({placeholders})
                    GROUP BY buyer.id
                    HAVING n_aziende_che_vincono >= 2
                    ORDER BY n_aziende_che_vincono DESC
                """
                try:
                    buyer_rows = conn.execute(sql_buyers, azienda_ids).fetchall()
                except sqlite3.Error as e:
                    logger.warning(f"P2 query buyers error: {e}")
                    continue

                if not buyer_rows:
                    continue

                # Genera 1 lead per buyer-condiviso
                for br in buyer_rows:
                    buyer_name = br["buyer_name"]
                    n_az = int(br["n_aziende_che_vincono"])
                    # Score scala con quante aziende coinvolte
                    score = clamp(
                        score_lo + (score_hi - score_lo) * min(1.0, (n_az - 2) / 3.0)
                    )

                    title_seed = (
                        f"Secondo le fonti pubbliche, {persona_name} amministra "
                        f"{len(azienda_names)} aziende che risultano vincitrici "
                        f"di gare bandite da {buyer_name}: una concentrazione "
                        f"che merita verifica."
                    )

                    evidence = [
                        {
                            "source": "REGISTRO_IMPRESE",
                            "description": (
                                f"{persona_name} amministratore di: "
                                f"{', '.join(azienda_names)}"
                            ),
                            "url": "",
                        },
                        {
                            "source": "ANAC",
                            "description": (
                                f"{n_az} delle aziende sopra hanno vinto gare "
                                f"bandite da {buyer_name}"
                            ),
                            "url": "https://dati.anticorruzione.it/",
                        },
                    ]

                    lead = Lead(
                        id="",
                        pattern="HIDDEN_GROUP",
                        scoop_score=score,
                        title_seed=title_seed,
                        entities=[persona_name, *azienda_names, buyer_name],
                        evidence=evidence,
                        suggested_angle=(
                            f"Verificare se {persona_name} sia il punto di "
                            f"connessione tra le aziende e {buyer_name}: "
                            f"controllare assetti societari, sedi condivise, "
                            f"date di costituzione, e percentuale di gare "
                            f"vinte sul totale del buyer."
                        ),
                        human_review_required=False,
                    )
                    leads.append(lead)

        if leads:
            logger.info(f"P2 HIDDEN_GROUP: {len(leads)} leads")
        return leads

    # --------------------------------------------------------
    # P3: AZIENDA_NEONATA
    # --------------------------------------------------------

    def _find_azienda_neonata(self) -> list[Lead]:
        """
        Azienda flaggata AZIENDA_NEONATA dall'enricher.

        Query: anomaly_flags WHERE anomaly_type = 'AZIENDA_NEONATA'
        """
        if not self._need_anomalies("P3 AZIENDA_NEONATA"):
            return []

        leads: list[Lead] = []

        with self.kg._get_conn() as conn:
            sql = """
                SELECT
                    af.id              AS af_id,
                    af.anomaly_score   AS score,
                    af.description     AS af_desc,
                    af.data_json       AS af_data,
                    af.detected_at     AS detected_at,
                    azienda.id         AS azienda_id,
                    azienda.name       AS azienda_name,
                    azienda.partita_iva AS azienda_piva,
                    azienda.metadata   AS azienda_meta
                FROM anomaly_flags af
                JOIN entities azienda
                    ON azienda.id = af.entity_id
                    AND azienda.entity_type = 'AZIENDA'
                WHERE af.anomaly_type = 'AZIENDA_NEONATA'
                  AND COALESCE(af.dismissed, 0) = 0
                ORDER BY af.anomaly_score DESC
            """
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"P3 query error: {e}")
                return []

        if not rows:
            logger.info("P3 AZIENDA_NEONATA: nessuna anomalia di questo tipo")
            return []

        score_lo, score_hi = PATTERN_SCORES["AZIENDA_NEONATA"]

        for r in rows:
            base_score = float(r["score"] or 0.0)
            # Mappa lo score sorgente nel range pattern
            score = clamp(score_lo + (score_hi - score_lo) * base_score)

            azienda = r["azienda_name"]
            piva = r["azienda_piva"]

            inc_date = ""
            try:
                meta = json.loads(r["azienda_meta"] or "{}")
                inc_date = meta.get("incorporation_date") or ""
            except (json.JSONDecodeError, TypeError):
                pass

            title_seed = (
                f"I dati del Registro Imprese mostrano che {azienda} e' una "
                f"societa di recente costituzione gia presente fra i vincitori "
                f"di gare pubbliche: la tempistica suggerisce un approfondimento."
            )

            evidence = [
                {
                    "source": "REGISTRO_IMPRESE",
                    "description": (
                        f"{azienda} fondata il {inc_date}"
                        if inc_date
                        else f"{azienda}: data di costituzione recente"
                    ),
                    "url": (
                        f"https://opencorporates.com/companies/it/{piva}"
                        if piva
                        else ""
                    ),
                },
                {
                    "source": "ANAC",
                    "description": r["af_desc"] or "Anomalia AZIENDA_NEONATA",
                    "url": "",
                },
            ]

            lead = Lead(
                id="",
                pattern="AZIENDA_NEONATA",
                scoop_score=score,
                title_seed=title_seed,
                entities=[azienda],
                evidence=evidence,
                suggested_angle=(
                    f"Ricostruire la storia di {azienda}: chi ha costituito la "
                    f"societa, quando ha vinto la prima gara, e con quali enti "
                    f"ha rapporti. Verificare eventuali aziende precedenti "
                    f"degli stessi soci."
                ),
                human_review_required=False,
            )
            leads.append(lead)

        if leads:
            logger.info(f"P3 AZIENDA_NEONATA: {len(leads)} leads")
        return leads

    # --------------------------------------------------------
    # P4: RECIDIVO
    # --------------------------------------------------------

    def _find_recidivi(self) -> list[Lead]:
        """
        Entita con >=2 anomaly_flags di tipo diverso (combina segnali).

        Nota: la tabella anomaly_flags non ha source_ref; usiamo
        COUNT(DISTINCT anomaly_type) come proxy di "fonti diverse"
        (ANAC vs OPENSANCTIONS_HIT vs DOPPIO_FINANZIAMENTO etc.).
        """
        if not self._need_anomalies("P4 RECIDIVO", n=RECIDIVO_MIN_FLAGS):
            return []

        leads: list[Lead] = []

        with self.kg._get_conn() as conn:
            sql = """
                SELECT
                    e.id   AS entity_id,
                    e.name AS entity_name,
                    e.entity_type AS entity_type,
                    COUNT(*) AS total_flags,
                    COUNT(DISTINCT af.anomaly_type) AS distinct_types,
                    GROUP_CONCAT(DISTINCT af.anomaly_type) AS types,
                    AVG(af.anomaly_score) AS avg_score
                FROM anomaly_flags af
                JOIN entities e ON e.id = af.entity_id
                WHERE COALESCE(af.dismissed, 0) = 0
                  AND af.entity_id IS NOT NULL
                GROUP BY e.id
                HAVING total_flags >= ?
                  AND distinct_types >= ?
                ORDER BY distinct_types DESC, avg_score DESC
            """
            try:
                rows = conn.execute(
                    sql, (RECIDIVO_MIN_FLAGS, RECIDIVO_MIN_FLAGS)
                ).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"P4 query error: {e}")
                return []

        if not rows:
            logger.info(
                "P4 RECIDIVO: nessuna entita con >=2 flag di tipo diverso"
            )
            return []

        score_lo, score_hi = PATTERN_SCORES["RECIDIVO"]

        # Verifica se include un OPENSANCTIONS_HIT su persona -> review umana
        for r in rows:
            entity_name = r["entity_name"]
            entity_type = r["entity_type"]
            types_csv = r["types"] or ""
            types_set = {t for t in types_csv.split(",") if t}
            avg = float(r["avg_score"] or 0.0)
            distinct = int(r["distinct_types"])

            # Score scala con numero di tipi distinti
            score = clamp(
                score_lo + (score_hi - score_lo) * min(1.0, (distinct - 2) / 2.0)
            )
            # Boost ulteriore se score medio alto
            score = clamp(score + 0.05 * avg, hi=score_hi)

            human_review = (
                entity_type == "PERSONA" and "OPENSANCTIONS_HIT" in types_set
            )

            title_seed = (
                f"Le fonti pubbliche segnalano {entity_name} per "
                f"{distinct} tipologie di anomalia distinte "
                f"({', '.join(sorted(types_set))}): un cumulo di indizi che "
                f"merita un'analisi unitaria."
            )

            evidence = [
                {
                    "source": "KG_ANOMALY_FLAGS",
                    "description": (
                        f"{r['total_flags']} flag, tipi: {', '.join(sorted(types_set))}"
                    ),
                    "url": "",
                },
            ]

            lead = Lead(
                id="",
                pattern="RECIDIVO",
                scoop_score=score,
                title_seed=title_seed,
                entities=[entity_name],
                evidence=evidence,
                suggested_angle=(
                    f"Aggregare i diversi segnali su {entity_name} in un "
                    f"unico dossier: ogni tipologia di anomalia rappresenta "
                    f"una fonte indipendente. La concomitanza alza la priorita."
                ),
                human_review_required=human_review,
            )
            leads.append(lead)

        if leads:
            logger.info(f"P4 RECIDIVO: {len(leads)} leads")
        return leads

    # --------------------------------------------------------
    # P5: CONCENTRAZIONE_VALORE
    # --------------------------------------------------------

    def _find_concentrazione_valore(self) -> list[Lead]:
        """
        Fornitore (azienda) riceve >30% del valore totale appalti da un
        singolo ente. Usa weight della relation VINCE come proxy del valore
        (l'enricher mette weight=1.0 di default; quando weight reale e'
        disponibile usiamo quello).

        Se tutti i weight sono 1.0 usiamo COUNT come fallback (proxy: % di gare).
        """
        if not self._need_relations("P5 CONCENTRAZIONE_VALORE", n=2):
            return []

        leads: list[Lead] = []

        with self.kg._get_conn() as conn:
            # Aggrega VINCE per (supplier, buyer)
            sql = """
                SELECT
                    supplier.id   AS supplier_id,
                    supplier.name AS supplier_name,
                    buyer.id      AS buyer_id,
                    buyer.name    AS buyer_name,
                    SUM(COALESCE(r.weight, 1.0)) AS total_value,
                    COUNT(*) AS n_tenders
                FROM relations r
                JOIN entities supplier
                    ON supplier.id = r.entity1_id
                    AND supplier.entity_type = 'AZIENDA'
                JOIN entities buyer
                    ON buyer.id = r.entity2_id
                    AND buyer.entity_type = 'ENTE_PUBBLICO'
                WHERE r.relation_type = 'VINCE'
                GROUP BY supplier.id, buyer.id
            """
            try:
                rows = conn.execute(sql).fetchall()
            except sqlite3.Error as e:
                logger.warning(f"P5 query error: {e}")
                return []

        if not rows:
            logger.info("P5 CONCENTRAZIONE_VALORE: nessuna relazione VINCE")
            return []

        # Aggrega per supplier
        by_supplier: dict[int, dict] = {}
        for r in rows:
            sid = r["supplier_id"]
            if sid not in by_supplier:
                by_supplier[sid] = {
                    "supplier_name": r["supplier_name"],
                    "total": 0.0,
                    "buyers": [],  # list of (buyer_name, value, count)
                }
            by_supplier[sid]["total"] += float(r["total_value"])
            by_supplier[sid]["buyers"].append(
                (r["buyer_name"], float(r["total_value"]), int(r["n_tenders"]))
            )

        score_lo, score_hi = PATTERN_SCORES["CONCENTRAZIONE_VALORE"]

        for sid, sup in by_supplier.items():
            total = sup["total"]
            if total <= 0 or len(sup["buyers"]) < 2:
                # Solo 1 buyer non e' "concentrazione" significativa: e' monocliente.
                # Lo segnaliamo solo se ha >= 2 gare vinte da quel singolo buyer
                if len(sup["buyers"]) == 1 and sup["buyers"][0][2] >= 2:
                    bn, bv, bc = sup["buyers"][0]
                    pct = 1.0
                    score = clamp(score_lo + (score_hi - score_lo) * 0.5)
                    leads.append(
                        self._build_concentration_lead(
                            sup["supplier_name"], bn, pct, bc, score
                        )
                    )
                continue

            for buyer_name, value, n_tenders in sup["buyers"]:
                pct = value / total if total > 0 else 0.0
                if pct < CONCENTRATION_THRESHOLD:
                    continue
                # Score scala con percentuale (oltre il 30%)
                score = clamp(
                    score_lo + (score_hi - score_lo) * min(1.0, (pct - 0.30) / 0.50)
                )
                leads.append(
                    self._build_concentration_lead(
                        sup["supplier_name"], buyer_name, pct, n_tenders, score
                    )
                )

        if leads:
            logger.info(f"P5 CONCENTRAZIONE_VALORE: {len(leads)} leads")
        return leads

    @staticmethod
    def _build_concentration_lead(
        supplier: str, buyer: str, pct: float, n_tenders: int, score: float
    ) -> Lead:
        title_seed = (
            f"I dati ANAC indicano che {supplier} riceve circa il "
            f"{pct * 100:.0f}% del valore dei propri appalti pubblici da "
            f"{buyer}: una concentrazione che giustifica un approfondimento "
            f"sulla relazione tra fornitore e stazione appaltante."
        )
        return Lead(
            id="",
            pattern="CONCENTRAZIONE_VALORE",
            scoop_score=score,
            title_seed=title_seed,
            entities=[supplier, buyer],
            evidence=[
                {
                    "source": "ANAC",
                    "description": (
                        f"{supplier} -> {buyer}: {n_tenders} gare, "
                        f"{pct * 100:.0f}% del valore totale appalti del fornitore"
                    ),
                    "url": "https://dati.anticorruzione.it/",
                }
            ],
            suggested_angle=(
                f"Esaminare la dipendenza commerciale di {supplier} da "
                f"{buyer}: contiguita personale, ribassi medi, varianti, "
                f"e concorrenza effettiva nelle gare vinte."
            ),
            human_review_required=False,
        )

    # --------------------------------------------------------
    # P6: pipeline boost
    # --------------------------------------------------------

    def _apply_pipeline_boost(self, lead: Lead) -> None:
        """
        Se una qualsiasi entita del lead e' citata in un'inchiesta del
        pipeline.json, applica boost score e collega all'inchiesta.
        """
        pipeline_ents = self._load_pipeline_entities()
        if not pipeline_ents:
            return

        for ent_name in lead.entities:
            slug = pipeline_ents.get(ent_name)
            if slug:
                lead.scoop_score = clamp(lead.scoop_score + PIPELINE_BOOST)
                lead.linked_investigation = slug
                return

    # --------------------------------------------------------
    # Deduplicazione
    # --------------------------------------------------------

    @staticmethod
    def _cluster_key(lead: Lead) -> tuple[str, frozenset[str]]:
        """Cluster su (pattern, set entita normalizzate)."""
        ents = frozenset(normalize_name(e) for e in lead.entities if e)
        return (lead.pattern, ents)

    def _deduplicate(self, leads: list[Lead]) -> list[Lead]:
        """Stesso (pattern, cluster_entita) -> tieni il lead con score piu alto."""
        by_key: dict[tuple, Lead] = {}
        for lead in leads:
            key = self._cluster_key(lead)
            current = by_key.get(key)
            if current is None or lead.scoop_score > current.scoop_score:
                by_key[key] = lead
        deduped = list(by_key.values())
        if len(deduped) < len(leads):
            logger.info(
                f"Deduplica: {len(leads)} -> {len(deduped)} leads"
            )
        return deduped

    # --------------------------------------------------------
    # Pattern dispatch
    # --------------------------------------------------------

    _PATTERN_METHODS = {
        "P1": ("ANOMALY_PEP", "_find_anomaly_pep"),
        "P2": ("HIDDEN_GROUP", "_find_hidden_group"),
        "P3": ("AZIENDA_NEONATA", "_find_azienda_neonata"),
        "P4": ("RECIDIVO", "_find_recidivi"),
        "P5": ("CONCENTRAZIONE_VALORE", "_find_concentrazione_valore"),
    }

    def find_leads(
        self,
        min_score: float = 0.5,
        patterns: list[str] | None = None,
    ) -> list[Lead]:
        """
        Esegue i pattern e ritorna leads filtrati per score minimo.

        Args:
            min_score: soglia score minima (post-boost)
            patterns: lista codici (es. ["P1", "P2"]); None = tutti
        """
        # Reset cache stats (potrebbe essere riusato)
        self._kg_stats = None

        if patterns is None:
            wanted = list(self._PATTERN_METHODS.keys())
        else:
            wanted = [p.upper().strip() for p in patterns if p.upper().strip() in self._PATTERN_METHODS]

        all_leads: list[Lead] = []
        for code in wanted:
            _, method_name = self._PATTERN_METHODS[code]
            method = getattr(self, method_name)
            try:
                all_leads.extend(method())
            except Exception as e:
                logger.exception(f"Pattern {code} fallito: {e}")

        # Apply pipeline boost
        for lead in all_leads:
            self._apply_pipeline_boost(lead)

        # Dedup
        all_leads = self._deduplicate(all_leads)

        # Filter by min_score and sort
        all_leads = [l for l in all_leads if l.scoop_score >= min_score]
        all_leads.sort(key=lambda l: l.scoop_score, reverse=True)
        return all_leads

    # --------------------------------------------------------
    # Persistenza leads.json
    # --------------------------------------------------------

    def _load_existing_leads(self, path: Path) -> list[dict]:
        """Carica leads esistenti (lista di dict)."""
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"leads.json esistente non leggibile: {e}; ricreo")
            return []
        existing = data.get("leads") if isinstance(data, dict) else None
        if not isinstance(existing, list):
            return []
        return [x for x in existing if isinstance(x, dict)]

    @staticmethod
    def _next_lead_id(date_str: str, existing_ids: set[str]) -> str:
        """Assegna lead-{date}-{n:03d} progressivo per la data."""
        prefix = f"lead-{date_str}-"
        used_n = []
        for lid in existing_ids:
            if lid.startswith(prefix):
                tail = lid[len(prefix):]
                if tail.isdigit():
                    used_n.append(int(tail))
        n = (max(used_n) + 1) if used_n else 1
        return f"{prefix}{n:03d}"

    @staticmethod
    def _signature(lead_dict: dict) -> tuple[str, frozenset[str]]:
        """Identita logica di un lead (per dedup cross-run)."""
        ents = frozenset(
            normalize_name(e) for e in (lead_dict.get("entities") or []) if e
        )
        return (lead_dict.get("pattern", ""), ents)

    def save_leads(
        self, leads: list[Lead], output_path: Path | str = LEADS_JSON
    ) -> int:
        """
        Mergia con leads.json esistente. Ritorna numero di nuovi leads aggiunti.

        Regole merge:
        - Lead con status != "new" sono PRESERVATI (anche se non rigenerati)
        - Lead con status == "new" che NON hanno match cross-run vengono rimossi
          (sono diventati stale: il pattern non li rileva piu)
        - Lead nuovi (signature non presente) vengono aggiunti con id progressivo
        - Lead nuovi che fanno match con uno status="new" esistente: refresh
          (mantiene id, aggiorna fields)
        """
        output_path = Path(output_path)
        existing = self._load_existing_leads(output_path)

        # Indicizza esistenti per signature
        existing_by_sig: dict[tuple, dict] = {}
        existing_ids: set[str] = set()
        for ex in existing:
            existing_ids.add(ex.get("id", ""))
            existing_by_sig[self._signature(ex)] = ex

        date_str = today_str()
        new_leads_payload: list[dict] = []
        n_added = 0

        # 1. Preserva lead non-"new" (assigned/published/etc.)
        preserved_sigs: set[tuple] = set()
        for ex in existing:
            if ex.get("status", "new") != "new":
                new_leads_payload.append(ex)
                preserved_sigs.add(self._signature(ex))

        # 2. Process nuovi leads
        new_sigs: set[tuple] = set()
        for lead in leads:
            d = lead.to_dict()
            sig = self._signature(d)
            new_sigs.add(sig)

            if sig in preserved_sigs:
                # Lead gia in lavorazione, non lo ri-emettiamo come "new"
                continue

            existing_match = existing_by_sig.get(sig)
            if existing_match and existing_match.get("status", "new") == "new":
                # Refresh: tieni id e generated_at originale, aggiorna campi dinamici
                d["id"] = existing_match["id"]
                d["generated_at"] = existing_match.get(
                    "generated_at", lead.generated_at
                )
                d["status"] = "new"
            else:
                # Nuovo lead: assegna id
                lid = self._next_lead_id(date_str, existing_ids)
                existing_ids.add(lid)
                d["id"] = lid
                n_added += 1

            new_leads_payload.append(d)

        # Sort: preserved (con status diverso) finiscono in fondo per priorita "new"
        new_leads_payload.sort(
            key=lambda x: (x.get("status") != "new", -float(x.get("scoop_score", 0)))
        )

        out = {
            "generated_at": now_iso(),
            "total": len(new_leads_payload),
            "leads": new_leads_payload,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        return n_added

    # --------------------------------------------------------
    # Run pipeline completa
    # --------------------------------------------------------

    def run(
        self,
        min_score: float = 0.5,
        dry_run: bool = False,
        patterns: list[str] | None = None,
        output_path: Path | str = LEADS_JSON,
    ) -> dict:
        """Entry point principale. Ritorna summary dict."""
        logger.info(
            f"=== ScoopEngine run (min_score={min_score}, "
            f"dry_run={dry_run}, patterns={patterns or 'ALL'}) ==="
        )

        leads = self.find_leads(min_score=min_score, patterns=patterns)
        logger.info(f"Leads trovati (post-filter, post-dedup): {len(leads)}")

        n_added = 0
        if not dry_run:
            n_added = self.save_leads(leads, output_path=output_path)
            logger.info(f"Leads.json: {n_added} nuovi leads aggiunti")

        return {
            "found": len(leads),
            "added": n_added,
            "dry_run": dry_run,
            "kg_stats": self._stats(),
            "leads": [l.to_dict() for l in leads],
        }

    # --------------------------------------------------------
    # Stats helper
    # --------------------------------------------------------

    def stats(self, output_path: Path | str = LEADS_JSON) -> dict:
        """Ritorna statistiche KG + leads esistenti."""
        kg_stats = self._stats()
        existing = self._load_existing_leads(Path(output_path))

        by_status: dict[str, int] = {}
        by_pattern: dict[str, int] = {}
        for ex in existing:
            by_status[ex.get("status", "?")] = by_status.get(
                ex.get("status", "?"), 0
            ) + 1
            by_pattern[ex.get("pattern", "?")] = by_pattern.get(
                ex.get("pattern", "?"), 0
            ) + 1

        return {
            "kg": kg_stats,
            "leads_total": len(existing),
            "leads_by_status": by_status,
            "leads_by_pattern": by_pattern,
            "pipeline_entities_in_kg": len(self._load_pipeline_entities()),
        }


# ============================================================
# CLI
# ============================================================


def _format_lead_short(lead_dict: dict) -> str:
    return (
        f"  [{lead_dict.get('id', '?')}] "
        f"{lead_dict.get('pattern', '?')} "
        f"score={float(lead_dict.get('scoop_score', 0)):.2f} "
        f"review={'Y' if lead_dict.get('human_review_required') else 'n'} "
        f"linked={lead_dict.get('linked_investigation') or '-'} "
        f"entities={lead_dict.get('entities', [])}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "AIena Scoop Engine (C8): mina il KG per leads investigativi."
        )
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.5,
        help="Score minimo per includere un lead (default: 0.5)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Stampa i leads senza salvare leads.json",
    )
    parser.add_argument(
        "--pattern",
        type=str,
        default=None,
        help="Pattern da eseguire (es. P1,P2). Default: tutti",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Mostra statistiche KG + leads esistenti e termina",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(LEADS_JSON),
        help=f"Path output leads.json (default: {LEADS_JSON})",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Logging DEBUG"
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    engine = ScoopEngine()

    if args.stats:
        s = engine.stats(output_path=Path(args.output))
        print("=== KG STATS ===")
        for k, v in s["kg"].items():
            print(f"  {k}: {v}")
        print(f"\n=== LEADS ({args.output}) ===")
        print(f"  total: {s['leads_total']}")
        print(f"  by_status: {s['leads_by_status']}")
        print(f"  by_pattern: {s['leads_by_pattern']}")
        print(f"  pipeline_entities_in_kg: {s['pipeline_entities_in_kg']}")
        return 0

    patterns = None
    if args.pattern:
        patterns = [p.strip() for p in args.pattern.split(",") if p.strip()]

    result = engine.run(
        min_score=args.min_score,
        dry_run=args.dry_run,
        patterns=patterns,
        output_path=Path(args.output),
    )

    print(f"\n=== RESULT ===")
    print(f"  found: {result['found']}")
    print(f"  added: {result['added']}")
    print(f"  dry_run: {result['dry_run']}")
    print(f"  kg_stats: {result['kg_stats']}")
    if result["leads"]:
        print(f"\n=== TOP LEADS ===")
        for ld in result["leads"][:10]:
            print(_format_lead_short(ld))
    else:
        print("\n  (nessun lead generato)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
