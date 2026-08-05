#!/usr/bin/env python3
"""
AIena Knowledge Graph — classe principale per gestione grafo entita/relazioni.

Usa SQLite per persistenza e NetworkX per query di connettivita.
Design: load-on-demand, non carica tutto il grafo in memoria.

Decisioni architetturali:
- SQLite per persistenza (coerenza con stack esistente)
- NetworkX per traversal (algoritmi built-in per path, centrality, clusters)
- Normalizzazione nomi aggressive (uppercase, strip, collasso spazi)
- Fuzzy search con LIKE + Levenshtein fallback
"""
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# NetworkX importato lazy per permettere installazione separata
_nx = None


def _get_networkx():
    """Import lazy di NetworkX."""
    global _nx
    if _nx is None:
        try:
            import networkx as nx
            _nx = nx
        except ImportError:
            raise ImportError(
                "NetworkX non installato. Esegui: pip install networkx"
            )
    return _nx


# ============================================================
# Costanti e tipi
# ============================================================

ENTITY_TYPES = {
    "PERSONA", "AZIENDA", "ENTE_PUBBLICO", "INDIRIZZO", "APPALTO", "ALTRO"
}

RELATION_TYPES = {
    "VINCE",           # Azienda vince appalto
    "AMMINISTRA",      # Persona amministra azienda
    "POSSIEDE",        # Persona/azienda possiede altra azienda
    "LAVORA_PER",      # Persona lavora per ente/azienda
    "COLLABORA",       # Relazione generica tra entita
    "PAGA",            # Flusso di denaro A -> B
    "RICEVE",          # Riceve pagamento/appalto
    "LOCALIZZATO_IN",  # Entita ha sede/indirizzo
    "CORRELATO",       # Correlazione generica
    "SUCCESSORE_DI",   # Azienda succede a altra
    "CONTROLLATO_DA",  # Azienda controllata da altra
}

SOURCE_TYPES = {
    "ANAC", "SEGNALAZIONE", "ARTICOLO", "REGISTRO_IMPRESE",
    "OPENPOLIS", "OPENSANCTIONS", "RICERCA", "ALTRO"
}

DB_PATH = Path("/home/pinky/.pinkybot/data/aiena_knowledge_graph.db")


# ============================================================
# Utilities
# ============================================================

def normalize_name(name: str) -> str:
    """
    Normalizza un nome per matching consistente.
    - Uppercase
    - Strip
    - Collassa spazi multipli
    - Rimuove punteggiatura finale
    """
    if not name:
        return ""
    # Uppercase e strip
    n = name.upper().strip()
    # Collassa spazi multipli
    n = re.sub(r"\s+", " ", n)
    # Rimuove punteggiatura finale
    n = re.sub(r"[.,;:]+$", "", n)
    return n


def normalize_cf(cf: str | None) -> str | None:
    """Normalizza codice fiscale/P.IVA (uppercase, solo alfanumerici)."""
    if not cf:
        return None
    return re.sub(r"[^A-Z0-9]", "", cf.upper())


def now_iso() -> str:
    """Timestamp ISO corrente in UTC."""
    return datetime.now(timezone.utc).isoformat()


def levenshtein_distance(s1: str, s2: str) -> int:
    """Calcola distanza di Levenshtein tra due stringhe."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    return prev_row[-1]


# ============================================================
# Classe principale
# ============================================================

class KnowledgeGraph:
    """
    Knowledge Graph per AIena.

    Esempio d'uso:
        kg = KnowledgeGraph()
        kg.add_entity("Edilizia Calabria SRL", "AZIENDA", {"sede": "Crotone"})
        kg.add_entity("Ospedale di Crotone", "ENTE_PUBBLICO")
        kg.add_relation("Edilizia Calabria SRL", "Ospedale di Crotone",
                        "VINCE", source_type="ANAC", confidence=0.9)
        connections = kg.get_connections("Edilizia Calabria SRL")
    """

    def __init__(self, db_path: Path | str | None = None):
        """
        Inizializza il Knowledge Graph.

        Args:
            db_path: Path al database SQLite. Default: DB_PATH globale.
        """
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Inizializza database se non esiste
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Ottiene connessione al database con WAL mode."""
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self):
        """Inizializza il database con lo schema."""
        schema_path = Path(__file__).parent / "schema.sql"
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema non trovato: {schema_path}")

        with self._get_conn() as conn:
            conn.executescript(schema_path.read_text())

    # ========================================================
    # CRUD Entita
    # ========================================================

    def add_entity(
        self,
        name: str,
        entity_type: str,
        metadata: dict[str, Any] | None = None,
        codice_fiscale: str | None = None,
        partita_iva: str | None = None,
        cig: str | None = None,
        confidence: float = 1.0
    ) -> int:
        """
        Aggiunge o aggiorna un'entita nel grafo (upsert).

        Args:
            name: Nome dell'entita
            entity_type: Tipo (PERSONA, AZIENDA, ENTE_PUBBLICO, etc.)
            metadata: Dizionario con dati aggiuntivi
            codice_fiscale: CF persona o P.IVA
            partita_iva: P.IVA se diversa da CF
            cig: Codice gara (per appalti)
            confidence: Livello di confidenza (0.0-1.0)

        Returns:
            ID dell'entita creata/aggiornata
        """
        if entity_type not in ENTITY_TYPES:
            raise ValueError(f"Tipo entita non valido: {entity_type}")

        norm_name = normalize_name(name)
        if not norm_name:
            raise ValueError("Nome entita vuoto dopo normalizzazione")

        now = now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        cf = normalize_cf(codice_fiscale)
        piva = normalize_cf(partita_iva)

        with self._get_conn() as conn:
            # Cerca entita esistente
            row = conn.execute(
                "SELECT id FROM entities WHERE name = ? AND entity_type = ?",
                (norm_name, entity_type)
            ).fetchone()

            if row:
                # Update esistente
                conn.execute("""
                    UPDATE entities SET
                        name_original = COALESCE(?, name_original),
                        metadata = COALESCE(?, metadata),
                        codice_fiscale = COALESCE(?, codice_fiscale),
                        partita_iva = COALESCE(?, partita_iva),
                        cig = COALESCE(?, cig),
                        confidence = MAX(confidence, ?),
                        last_updated_at = ?
                    WHERE id = ?
                """, (name, meta_json, cf, piva, cig, confidence, now, row["id"]))
                return row["id"]
            else:
                # Insert nuovo
                cursor = conn.execute("""
                    INSERT INTO entities
                        (name, name_original, entity_type, codice_fiscale,
                         partita_iva, cig, metadata, confidence,
                         first_seen_at, last_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (norm_name, name, entity_type, cf, piva, cig,
                      meta_json, confidence, now, now))
                return cursor.lastrowid

    def get_entity(self, entity_id: int) -> dict | None:
        """Ottiene un'entita per ID."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if row:
                return dict(row)
        return None

    def get_entity_by_name(
        self, name: str, entity_type: str | None = None
    ) -> dict | None:
        """
        Cerca entita per nome (normalizzato).

        Args:
            name: Nome da cercare
            entity_type: Filtra per tipo (opzionale)
        """
        norm_name = normalize_name(name)
        with self._get_conn() as conn:
            if entity_type:
                row = conn.execute(
                    "SELECT * FROM entities WHERE name = ? AND entity_type = ?",
                    (norm_name, entity_type)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM entities WHERE name = ?", (norm_name,)
                ).fetchone()
            if row:
                return dict(row)
        return None

    def search_entity(
        self,
        query: str,
        entity_type: str | None = None,
        limit: int = 10,
        fuzzy_threshold: int = 3
    ) -> list[dict]:
        """
        Cerca entita con matching fuzzy.

        Prima prova LIKE, poi fallback a Levenshtein.

        Args:
            query: Testo da cercare
            entity_type: Filtra per tipo (opzionale)
            limit: Numero massimo risultati
            fuzzy_threshold: Distanza Levenshtein massima per match fuzzy

        Returns:
            Lista di entita ordinate per rilevanza
        """
        norm_query = normalize_name(query)
        results = []

        with self._get_conn() as conn:
            # Prima: match esatto o LIKE
            sql = "SELECT * FROM entities WHERE name LIKE ?"
            params: list[Any] = [f"%{norm_query}%"]

            if entity_type:
                sql += " AND entity_type = ?"
                params.append(entity_type)

            sql += " ORDER BY confidence DESC, investigation_count DESC LIMIT ?"
            params.append(limit * 2)  # Prendi di piu per merge con fuzzy

            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]

            # Se pochi risultati, aggiungi fuzzy search
            if len(results) < limit:
                # Prendi tutte le entita del tipo richiesto
                sql2 = "SELECT * FROM entities"
                params2 = []
                if entity_type:
                    sql2 += " WHERE entity_type = ?"
                    params2.append(entity_type)

                all_entities = conn.execute(sql2, params2).fetchall()

                # Calcola distanza Levenshtein
                seen_ids = {r["id"] for r in results}
                for row in all_entities:
                    if row["id"] in seen_ids:
                        continue
                    dist = levenshtein_distance(norm_query, row["name"])
                    if dist <= fuzzy_threshold:
                        d = dict(row)
                        d["_fuzzy_distance"] = dist
                        results.append(d)

        # Ordina: prima match esatti (senza _fuzzy_distance), poi per distanza
        def sort_key(r):
            if "_fuzzy_distance" not in r:
                return (0, -r.get("confidence", 0), -r.get("investigation_count", 0))
            return (1, r["_fuzzy_distance"], -r.get("confidence", 0))

        results.sort(key=sort_key)
        return results[:limit]

    def flag_suspicious(self, entity_id: int, suspicious: bool = True):
        """Marca un'entita come sospetta."""
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE entities SET is_suspicious = ?, last_updated_at = ? WHERE id = ?",
                (1 if suspicious else 0, now_iso(), entity_id)
            )

    def increment_investigation_count(self, entity_id: int):
        """Incrementa il contatore indagini per un'entita."""
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE entities SET
                    investigation_count = investigation_count + 1,
                    last_updated_at = ?
                   WHERE id = ?""",
                (now_iso(), entity_id)
            )

    # ========================================================
    # CRUD Relazioni
    # ========================================================

    def add_relation(
        self,
        entity1_name: str,
        entity2_name: str,
        relation_type: str,
        source_type: str = "ALTRO",
        source_url: str | None = None,
        source_date: str | None = None,
        description: str | None = None,
        weight: float = 1.0,
        confidence: float = 1.0,
        entity1_type: str | None = None,
        entity2_type: str | None = None
    ) -> int | None:
        """
        Aggiunge una relazione tra due entita.

        Se le entita non esistono, le crea come tipo "ALTRO"
        (o usa il tipo specificato).

        Args:
            entity1_name: Nome prima entita
            entity2_name: Nome seconda entita
            relation_type: Tipo relazione (VINCE, AMMINISTRA, etc.)
            source_type: Fonte dell'informazione
            source_url: URL fonte
            source_date: Data documento fonte
            description: Descrizione testuale
            weight: Peso relazione (default 1.0)
            confidence: Confidenza (0.0-1.0)
            entity1_type: Tipo entity1 se non esiste
            entity2_type: Tipo entity2 se non esiste

        Returns:
            ID della relazione creata, None se fallisce
        """
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"Tipo relazione non valido: {relation_type}")
        if source_type not in SOURCE_TYPES:
            raise ValueError(f"Tipo fonte non valido: {source_type}")

        # Trova o crea entita
        e1 = self.get_entity_by_name(entity1_name)
        if not e1:
            e1_type = entity1_type or "ALTRO"
            e1_id = self.add_entity(entity1_name, e1_type, confidence=0.5)
        else:
            e1_id = e1["id"]

        e2 = self.get_entity_by_name(entity2_name)
        if not e2:
            e2_type = entity2_type or "ALTRO"
            e2_id = self.add_entity(entity2_name, e2_type, confidence=0.5)
        else:
            e2_id = e2["id"]

        with self._get_conn() as conn:
            # Controlla se relazione esiste gia
            existing = conn.execute("""
                SELECT id, confidence FROM relations
                WHERE entity1_id = ? AND entity2_id = ? AND relation_type = ?
            """, (e1_id, e2_id, relation_type)).fetchone()

            if existing:
                # Aggiorna confidenza se maggiore
                if confidence > existing["confidence"]:
                    conn.execute("""
                        UPDATE relations SET confidence = ?, source_url = COALESCE(?, source_url)
                        WHERE id = ?
                    """, (confidence, source_url, existing["id"]))
                return existing["id"]

            # Crea nuova relazione
            cursor = conn.execute("""
                INSERT INTO relations
                    (entity1_id, entity2_id, relation_type, description, weight,
                     source_type, source_url, source_date, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (e1_id, e2_id, relation_type, description, weight,
                  source_type, source_url, source_date, confidence, now_iso()))

            return cursor.lastrowid

    def get_relations(
        self,
        entity_id: int,
        direction: str = "both"
    ) -> list[dict]:
        """
        Ottiene le relazioni di un'entita.

        Args:
            entity_id: ID entita
            direction: "outgoing", "incoming", o "both"

        Returns:
            Lista di relazioni con dettagli entita collegate
        """
        results = []
        with self._get_conn() as conn:
            if direction in ("outgoing", "both"):
                rows = conn.execute("""
                    SELECT r.*, e.name as target_name, e.entity_type as target_type
                    FROM relations r
                    JOIN entities e ON r.entity2_id = e.id
                    WHERE r.entity1_id = ?
                """, (entity_id,)).fetchall()
                for r in rows:
                    d = dict(r)
                    d["direction"] = "outgoing"
                    results.append(d)

            if direction in ("incoming", "both"):
                rows = conn.execute("""
                    SELECT r.*, e.name as target_name, e.entity_type as target_type
                    FROM relations r
                    JOIN entities e ON r.entity1_id = e.id
                    WHERE r.entity2_id = ?
                """, (entity_id,)).fetchall()
                for r in rows:
                    d = dict(r)
                    d["direction"] = "incoming"
                    results.append(d)

        return results

    # ========================================================
    # Graph Traversal con NetworkX
    # ========================================================

    def get_connections(
        self,
        entity_name: str,
        max_depth: int = 2,
        min_confidence: float = 0.3
    ) -> Any:  # ritorna nx.Graph
        """
        Restituisce il sottografo delle connessioni di un'entita.

        Usa BFS per esplorare fino a max_depth livelli di connessioni.

        Args:
            entity_name: Nome entita di partenza
            max_depth: Profondita massima di esplorazione
            min_confidence: Confidenza minima per includere relazioni

        Returns:
            NetworkX Graph con nodi e archi del sottografo
        """
        nx = _get_networkx()

        # Trova entita di partenza
        entity = self.get_entity_by_name(entity_name)
        if not entity:
            # Prova fuzzy search
            results = self.search_entity(entity_name, limit=1)
            if not results:
                return nx.Graph()
            entity = results[0]

        G = nx.Graph()
        visited = set()
        queue = [(entity["id"], 0)]  # (entity_id, depth)

        with self._get_conn() as conn:
            while queue:
                current_id, depth = queue.pop(0)

                if current_id in visited or depth > max_depth:
                    continue
                visited.add(current_id)

                # Aggiungi nodo
                e = conn.execute(
                    "SELECT * FROM entities WHERE id = ?", (current_id,)
                ).fetchone()
                if e:
                    G.add_node(
                        e["name"],
                        entity_type=e["entity_type"],
                        suspicious=bool(e["is_suspicious"]),
                        confidence=e["confidence"],
                        metadata=json.loads(e["metadata"] or "{}")
                    )

                # Ottieni relazioni
                relations = conn.execute("""
                    SELECT r.*, e1.name as name1, e2.name as name2,
                           e1.entity_type as type1, e2.entity_type as type2
                    FROM relations r
                    JOIN entities e1 ON r.entity1_id = e1.id
                    JOIN entities e2 ON r.entity2_id = e2.id
                    WHERE (r.entity1_id = ? OR r.entity2_id = ?)
                      AND r.confidence >= ?
                """, (current_id, current_id, min_confidence)).fetchall()

                for rel in relations:
                    # Aggiungi arco
                    G.add_edge(
                        rel["name1"],
                        rel["name2"],
                        relation_type=rel["relation_type"],
                        weight=rel["weight"],
                        confidence=rel["confidence"],
                        source=rel["source_type"]
                    )

                    # Aggiungi vicini alla coda
                    other_id = rel["entity2_id"] if rel["entity1_id"] == current_id else rel["entity1_id"]
                    if other_id not in visited and depth < max_depth:
                        queue.append((other_id, depth + 1))

        return G

    def find_suspicious_patterns(
        self,
        min_degree: int = 5,
        min_suspicion_score: float = 0.6
    ) -> list[dict]:
        """
        Trova entita con pattern sospetti:
        - Alto numero di connessioni
        - Connesse ad altre entita sospette
        - Alta centralita nel grafo

        Args:
            min_degree: Grado minimo per considerare un nodo
            min_suspicion_score: Score minimo per includere nei risultati

        Returns:
            Lista di entita sospette con score e motivazione
        """
        nx = _get_networkx()
        results = []

        with self._get_conn() as conn:
            # Costruisci grafo completo (per grafi piccoli)
            # Per grafi grandi, usa sampling
            count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            if count > 10000:
                print(f"[KG] Grafo grande ({count} entita), usa sampling")
                # TODO: implementare sampling per grafi grandi
                return []

            # Carica tutte le entita
            entities = {
                r["id"]: dict(r)
                for r in conn.execute("SELECT * FROM entities").fetchall()
            }

            # Carica tutte le relazioni
            relations = conn.execute("SELECT * FROM relations").fetchall()

            # Costruisci grafo NetworkX
            G = nx.Graph()
            for e in entities.values():
                G.add_node(e["id"], **e)

            for r in relations:
                G.add_edge(
                    r["entity1_id"],
                    r["entity2_id"],
                    weight=r["weight"],
                    relation_type=r["relation_type"]
                )

            if G.number_of_nodes() == 0:
                return []

            # Calcola metriche
            degrees = dict(G.degree())
            if G.number_of_nodes() > 1:
                betweenness = nx.betweenness_centrality(G)
            else:
                betweenness = {n: 0 for n in G.nodes()}

            # Trova nodi con connessioni sospette
            for node_id in G.nodes():
                entity = entities.get(node_id)
                if not entity:
                    continue

                degree = degrees.get(node_id, 0)
                if degree < min_degree:
                    continue

                # Calcola score sospetto
                score = 0.0
                reasons = []

                # Fattore 1: Grado alto
                if degree >= 10:
                    score += 0.3
                    reasons.append(f"Molte connessioni ({degree})")
                elif degree >= 5:
                    score += 0.15
                    reasons.append(f"Connessioni significative ({degree})")

                # Fattore 2: Betweenness centrality alta (nodo "ponte")
                bc = betweenness.get(node_id, 0)
                if bc > 0.1:
                    score += 0.3
                    reasons.append(f"Alta centralita ({bc:.2f})")

                # Fattore 3: Gia marcato sospetto
                if entity.get("is_suspicious"):
                    score += 0.2
                    reasons.append("Gia segnalato come sospetto")

                # Fattore 4: Connesso ad altri sospetti
                neighbors = list(G.neighbors(node_id))
                suspicious_neighbors = sum(
                    1 for n in neighbors
                    if entities.get(n, {}).get("is_suspicious")
                )
                if suspicious_neighbors >= 2:
                    score += 0.2
                    reasons.append(f"Connesso a {suspicious_neighbors} entita sospette")

                if score >= min_suspicion_score:
                    results.append({
                        "entity_id": node_id,
                        "name": entity["name"],
                        "entity_type": entity["entity_type"],
                        "suspicion_score": round(score, 2),
                        "reasons": reasons,
                        "degree": degree,
                        "betweenness": round(bc, 3)
                    })

        # Ordina per score
        results.sort(key=lambda x: -x["suspicion_score"])
        return results

    # ========================================================
    # Export per AIena
    # ========================================================

    def export_for_aiena(
        self,
        entity_name: str,
        max_connections: int = 20,
        max_depth: int = 2
    ) -> str:
        """
        Esporta il contesto di un'entita in formato leggibile per AIena.

        Genera un testo strutturato che puo essere incluso nel prompt
        del backlog scout.

        Args:
            entity_name: Nome entita
            max_connections: Numero massimo connessioni da includere
            max_depth: Profondita esplorazione

        Returns:
            Stringa formattata per il prompt di AIena
        """
        entity = self.get_entity_by_name(entity_name)
        if not entity:
            results = self.search_entity(entity_name, limit=1)
            if not results:
                return f"[KG] Nessuna informazione nota su: {entity_name}"
            entity = results[0]

        lines = [
            f"=== KNOWLEDGE GRAPH: {entity['name']} ===",
            f"Tipo: {entity['entity_type']}",
        ]

        # Metadata
        meta = json.loads(entity.get("metadata") or "{}")
        if meta:
            lines.append("Dati noti:")
            for k, v in meta.items():
                lines.append(f"  - {k}: {v}")

        # Identificatori
        if entity.get("codice_fiscale"):
            lines.append(f"CF/P.IVA: {entity['codice_fiscale']}")
        if entity.get("cig"):
            lines.append(f"CIG: {entity['cig']}")

        # Flags
        if entity.get("is_suspicious"):
            lines.append("FLAG: Entita marcata come SOSPETTA")
        if entity.get("investigation_count", 0) > 0:
            lines.append(f"Menzionata in {entity['investigation_count']} indagini precedenti")

        # Connessioni
        G = self.get_connections(entity_name, max_depth=max_depth)
        if G.number_of_edges() > 0:
            lines.append(f"\nConnessioni note ({G.number_of_edges()} totali):")

            # Raggruppa per tipo relazione
            edges_by_type: dict[str, list] = {}
            for u, v, data in list(G.edges(data=True))[:max_connections]:
                rel_type = data.get("relation_type", "CORRELATO")
                if rel_type not in edges_by_type:
                    edges_by_type[rel_type] = []
                edges_by_type[rel_type].append((u, v, data))

            for rel_type, edges in edges_by_type.items():
                lines.append(f"  [{rel_type}]:")
                for u, v, data in edges[:5]:  # Max 5 per tipo
                    other = v if u == entity["name"] else u
                    conf = data.get("confidence", 1.0)
                    src = data.get("source", "?")
                    lines.append(f"    - {other} (fonte: {src}, conf: {conf:.0%})")
                if len(edges) > 5:
                    lines.append(f"    ... e altre {len(edges) - 5}")

        # Fonti
        with self._get_conn() as conn:
            sources = conn.execute("""
                SELECT DISTINCT source_type, source_name, source_url
                FROM entity_sources WHERE entity_id = ?
                ORDER BY extracted_at DESC LIMIT 5
            """, (entity["id"],)).fetchall()

            if sources:
                lines.append("\nFonti:")
                for s in sources:
                    url_part = f" ({s['source_url']})" if s["source_url"] else ""
                    lines.append(f"  - [{s['source_type']}] {s['source_name']}{url_part}")

        lines.append("=== FINE KNOWLEDGE GRAPH ===")
        return "\n".join(lines)

    # ========================================================
    # Add Source
    # ========================================================

    def add_entity_source(
        self,
        entity_id: int,
        source_type: str,
        source_name: str,
        info_type: str,
        info_value: str | None = None,
        source_url: str | None = None,
        source_date: str | None = None,
        confidence: float = 1.0
    ):
        """
        Aggiunge una fonte per un'entita.

        Args:
            entity_id: ID entita
            source_type: Tipo fonte (ANAC, SEGNALAZIONE, etc.)
            source_name: Nome descrittivo
            info_type: Tipo informazione (ESISTENZA, INDIRIZZO, etc.)
            info_value: Valore specifico appreso
            source_url: URL fonte
            source_date: Data documento
            confidence: Livello confidenza
        """
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO entity_sources
                    (entity_id, source_type, source_name, source_url, source_date,
                     info_type, info_value, confidence, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (entity_id, source_type, source_name, source_url, source_date,
                  info_type, info_value, confidence, now_iso()))

    # ========================================================
    # Stats
    # ========================================================

    def get_stats(self) -> dict:
        """Restituisce statistiche sul knowledge graph."""
        with self._get_conn() as conn:
            stats = {
                "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
                "relations": conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0],
                "sources": conn.execute("SELECT COUNT(*) FROM entity_sources").fetchone()[0],
                "anomalies": conn.execute("SELECT COUNT(*) FROM anomaly_flags").fetchone()[0],
                "suspicious_entities": conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE is_suspicious = 1"
                ).fetchone()[0],
            }

            # Breakdown per tipo
            stats["by_type"] = {}
            for row in conn.execute(
                "SELECT entity_type, COUNT(*) as c FROM entities GROUP BY entity_type"
            ).fetchall():
                stats["by_type"][row["entity_type"]] = row["c"]

            return stats


# ============================================================
# Test self-contained
# ============================================================

if __name__ == "__main__":
    import tempfile

    print("=== Test KnowledgeGraph ===\n")

    # Usa DB temporaneo per test
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_kg.db"

        # Copia schema nella directory temp
        schema_src = Path(__file__).parent / "schema.sql"
        schema_dst = Path(tmpdir) / "schema.sql"
        schema_dst.write_text(schema_src.read_text())

        # Patch per usare schema nella directory temp
        import aiena_kg as kg_module
        original_init = KnowledgeGraph._init_db

        def patched_init(self):
            with self._get_conn() as conn:
                conn.executescript(schema_dst.read_text())

        KnowledgeGraph._init_db = patched_init

        kg = KnowledgeGraph(test_db)

        # Test 1: Add entities
        print("1. Aggiunta entita...")
        e1_id = kg.add_entity(
            "Edilizia Calabria SRL",
            "AZIENDA",
            {"sede": "Crotone", "anno_fondazione": 2020},
            partita_iva="12345678901"
        )
        print(f"   Creata azienda ID: {e1_id}")

        e2_id = kg.add_entity(
            "Ospedale San Giovanni di Dio",
            "ENTE_PUBBLICO",
            {"citta": "Crotone", "tipo": "ASP"}
        )
        print(f"   Creato ente ID: {e2_id}")

        e3_id = kg.add_entity(
            "Mario Rossi",
            "PERSONA",
            {"ruolo": "Amministratore"},
            codice_fiscale="RSSMRA80A01H501Z"
        )
        print(f"   Creata persona ID: {e3_id}")

        # Test 2: Add relations
        print("\n2. Aggiunta relazioni...")
        r1 = kg.add_relation(
            "Edilizia Calabria SRL",
            "Ospedale San Giovanni di Dio",
            "VINCE",
            source_type="ANAC",
            source_url="https://dati.anticorruzione.it/cig/123456",
            description="Appalto manutenzione 2024",
            confidence=0.95
        )
        print(f"   Relazione VINCE ID: {r1}")

        r2 = kg.add_relation(
            "Mario Rossi",
            "Edilizia Calabria SRL",
            "AMMINISTRA",
            source_type="REGISTRO_IMPRESE",
            confidence=1.0
        )
        print(f"   Relazione AMMINISTRA ID: {r2}")

        # Test 3: Search
        print("\n3. Ricerca entita...")
        results = kg.search_entity("Edilizia", limit=5)
        print(f"   Trovate {len(results)} entita per 'Edilizia':")
        for r in results:
            print(f"     - {r['name']} ({r['entity_type']})")

        # Test 4: Connections
        print("\n4. Connessioni...")
        G = kg.get_connections("Edilizia Calabria SRL", max_depth=2)
        print(f"   Grafo: {G.number_of_nodes()} nodi, {G.number_of_edges()} archi")

        # Test 5: Export for AIena
        print("\n5. Export per AIena:")
        export = kg.export_for_aiena("Edilizia Calabria SRL")
        print(export)

        # Test 6: Stats
        print("\n6. Statistiche:")
        stats = kg.get_stats()
        for k, v in stats.items():
            if isinstance(v, dict):
                print(f"   {k}:")
                for k2, v2 in v.items():
                    print(f"     - {k2}: {v2}")
            else:
                print(f"   {k}: {v}")

        print("\n=== Test completati con successo ===")
