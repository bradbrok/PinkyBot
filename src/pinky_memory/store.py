from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from pinky_memory.types import (
    MemoryQueryFilters,
    Reflection,
    ReflectionLink,
    ReflectionType,
    resolve_preset,
)

if TYPE_CHECKING:
    pass

# ── Memory linking constants ──

LINK_THRESHOLD = 0.78
MAX_LINKS_PER_MEMORY = 5
MAX_NEIGHBORS_PER_RESULT = 2
NEIGHBOR_DISCOUNT = 0.7


SCHEMA = """
CREATE TABLE IF NOT EXISTS reflections (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    salience INTEGER NOT NULL DEFAULT 3,
    active INTEGER NOT NULL DEFAULT 1,
    no_recall INTEGER NOT NULL DEFAULT 0,
    supersedes TEXT NOT NULL DEFAULT '',
    embedding TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    weight REAL NOT NULL DEFAULT 1.0
);

CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(type);
CREATE INDEX IF NOT EXISTS idx_reflections_project ON reflections(project);
CREATE INDEX IF NOT EXISTS idx_reflections_active ON reflections(active);
CREATE INDEX IF NOT EXISTS idx_reflections_salience ON reflections(salience);
CREATE INDEX IF NOT EXISTS idx_reflections_created_at ON reflections(created_at);

CREATE TABLE IF NOT EXISTS daemon_sync_counts (
    session_id TEXT PRIMARY KEY,
    message_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""

# FTS5 index for BM25-ranked keyword search
_FTS5_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS reflections_fts USING fts5(
    id UNINDEXED,
    content,
    context,
    project,
    content='reflections',
    content_rowid='rowid'
);
"""

# Triggers to keep FTS5 in sync with the main table
_FTS5_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS reflections_ai AFTER INSERT ON reflections BEGIN
    INSERT INTO reflections_fts(rowid, id, content, context, project)
    VALUES (new.rowid, new.id, new.content, new.context, new.project);
END;

CREATE TRIGGER IF NOT EXISTS reflections_ad AFTER DELETE ON reflections BEGIN
    INSERT INTO reflections_fts(reflections_fts, rowid, id, content, context, project)
    VALUES ('delete', old.rowid, old.id, old.content, old.context, old.project);
END;

CREATE TRIGGER IF NOT EXISTS reflections_au AFTER UPDATE ON reflections BEGIN
    INSERT INTO reflections_fts(reflections_fts, rowid, id, content, context, project)
    VALUES ('delete', old.rowid, old.id, old.content, old.context, old.project);
    INSERT INTO reflections_fts(rowid, id, content, context, project)
    VALUES (new.rowid, new.id, new.content, new.context, new.project);
END;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return uuid.uuid4().hex[:12]


class ReflectionStore:
    """Synchronous SQLite store for reflections with WAL mode."""

    # Default embedding dimensions (text-embedding-3-small)
    _DEFAULT_VEC_DIMENSIONS = 1536

    def __init__(self, db_path: str = "data/reflections.db") -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._vec_available = False
        self._vec_dimensions = 0
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # Migrate: add event_date column (nullable, for temporal reasoning)
        self._migrate_add_column("event_date", "TEXT DEFAULT NULL")
        # Migrate: add superseded_by column (nullable FK to track supersession chain)
        self._migrate_add_column("superseded_by", "TEXT NOT NULL DEFAULT ''")
        # Migrate: add last_auto_recalled column (for auto-recall dampening)
        self._migrate_add_column("last_auto_recalled", "TEXT DEFAULT NULL")
        # Migrate: add entities column (JSON array of person names)
        self._migrate_add_column("entities", "TEXT DEFAULT NULL")
        # Migrate: add no_recall column (suppress from search results)
        self._migrate_add_column("no_recall", "INTEGER NOT NULL DEFAULT 0")
        # Migrate: add source tracking columns (memory → conversation bridge)
        self._migrate_add_column("source_session_id", "TEXT DEFAULT NULL")
        self._migrate_add_column("source_channel", "TEXT DEFAULT NULL")
        self._migrate_add_column("source_message_ids", "TEXT DEFAULT NULL")
        try:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_reflections_source_session "
                "ON reflections(source_session_id)"
            )
            self._conn.commit()
        except Exception:
            pass
        # Migrate: spaced review columns (memory review system)
        self._migrate_add_column("next_review_date", "TEXT DEFAULT NULL")
        self._migrate_add_column("review_interval_days", "INTEGER DEFAULT 7")
        self._migrate_backfill_review_schedule()
        # Migrate: memory_events audit table (memory hygiene system)
        self._migrate_create_memory_events()
        # Migrate: reflection_links table (memory linking system)
        self._migrate_create_reflection_links()
        # FTS5 index (separate try — graceful if FTS5 not compiled in)
        try:
            self._conn.executescript(_FTS5_SCHEMA)
            self._conn.executescript(_FTS5_TRIGGERS)
            self._conn.commit()
            self._fts5_available = True
        except Exception:
            self._fts5_available = False
        # sqlite-vec virtual table (separate try — graceful if not available)
        self._init_vec()

    def _migrate_add_column(self, column: str, definition: str) -> None:
        """Add a column to the reflections table if it doesn't exist."""
        try:
            self._conn.execute(f"ALTER TABLE reflections ADD COLUMN {column} {definition}")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists

    def _migrate_backfill_review_schedule(self) -> None:
        """Backfill next_review_date for existing active memories on first run."""
        with self._lock:
            # Only backfill rows that have NULL next_review_date AND salience < 4
            # (salience >= 4 are protected and stay NULL = never auto-reviewed)
            self._conn.execute("""
                UPDATE reflections
                SET next_review_date = date(created_at, '+30 days')
                WHERE active = 1
                  AND next_review_date IS NULL
                  AND salience < 4
                  AND type != 'continuation'
                  AND no_recall = 0
            """)
            self._conn.commit()

    def _migrate_create_memory_events(self) -> None:
        """Create the memory_events audit table for the memory hygiene system."""
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                source_ids TEXT NOT NULL,
                target_id INTEGER,
                prior_content TEXT,
                prior_salience INTEGER,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                reversed_at TIMESTAMP
            )
        """)
        self._conn.commit()

    def _migrate_create_reflection_links(self) -> None:
        """Create the reflection_links table for memory linking."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS reflection_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id   TEXT NOT NULL,
                target_id   TEXT NOT NULL,
                similarity  REAL NOT NULL,
                created_at  DATETIME DEFAULT (datetime('now')),
                UNIQUE(source_id, target_id)
            );
            CREATE INDEX IF NOT EXISTS idx_reflection_links_source
                ON reflection_links(source_id);
            CREATE INDEX IF NOT EXISTS idx_reflection_links_target
                ON reflection_links(target_id);
        """)
        self._conn.commit()

    def _init_vec(self) -> None:
        """Load sqlite-vec extension and create/backfill the vector index."""
        try:
            import sqlite_vec
            ext_path = sqlite_vec.loadable_path()
            self._conn.enable_load_extension(True)
            self._conn.load_extension(ext_path)
            self._conn.enable_load_extension(False)
        except Exception:
            return  # sqlite-vec not available — fall back to numpy

        # Detect dimensions from existing embeddings
        dims = self._detect_embedding_dimensions()
        if dims == 0:
            # No embeddings yet — extension is loaded but vec table will be
            # created lazily on first insert with an embedding.
            self._vec_available = True
            return

        self._vec_dimensions = dims

        # Create vec0 virtual table if it doesn't exist
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS reflections_vec "
                f"USING vec0(embedding float[{dims}] distance_metric=cosine)"
            )
            self._conn.commit()
        except Exception:
            return

        # Vec table created successfully — mark as available
        self._vec_available = True

        # Backfill: sync any reflections that have embeddings but aren't in the vec table
        self._backfill_vec()

    def _create_vec_table(self, dims: int) -> None:
        """Create the vec0 virtual table with the given dimensions."""
        try:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS reflections_vec "
                f"USING vec0(embedding float[{dims}] distance_metric=cosine)"
            )
            self._conn.commit()
            self._vec_dimensions = dims
        except Exception:
            pass

    def _detect_embedding_dimensions(self) -> int:
        """Detect embedding dimensions from the first non-empty embedding in the DB."""
        row = self._conn.execute(
            "SELECT embedding FROM reflections WHERE embedding != '[]' LIMIT 1"
        ).fetchone()
        if row:
            emb = json.loads(row[0])
            if emb:
                return len(emb)
        return 0

    def _backfill_vec(self) -> None:
        """Sync existing embeddings into the vec0 table (idempotent)."""
        if not self._vec_available or self._vec_dimensions == 0:
            return
        # Find reflections with embeddings that are NOT yet in the vec table
        rows = self._conn.execute(
            "SELECT r.rowid, r.embedding FROM reflections r "
            "WHERE r.embedding != '[]' AND r.rowid NOT IN "
            "(SELECT rowid FROM reflections_vec)"
        ).fetchall()
        if not rows:
            return
        inserted = 0
        for row in rows:
            emb = json.loads(row[1])
            if len(emb) != self._vec_dimensions:
                continue  # skip mismatched dimensions
            blob = struct.pack(f"{len(emb)}f", *emb)
            try:
                self._conn.execute(
                    "INSERT INTO reflections_vec(rowid, embedding) VALUES (?, ?)",
                    (row[0], blob),
                )
                inserted += 1
            except Exception:
                pass  # rowid conflict or other issue — skip
        if inserted:
            self._conn.commit()
            log.info("vec_backfill_complete", inserted=inserted, skipped=len(rows) - inserted)

    @staticmethod
    def _embedding_to_blob(embedding: list[float]) -> bytes:
        """Convert a float list to a binary blob for sqlite-vec."""
        return struct.pack(f"{len(embedding)}f", *embedding)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── Insert ──

    def insert(self, reflection: Reflection) -> Reflection:
        with self._lock:
            if not reflection.id:
                reflection.id = _generate_id()
            now = _now_iso()
            reflection.created_at = datetime.fromisoformat(now)
            reflection.accessed_at = datetime.fromisoformat(now)

            entities_json = json.dumps(reflection.entities) if reflection.entities else None
            msg_ids_json = json.dumps(reflection.source_message_ids) if reflection.source_message_ids else None
            cursor = self._conn.execute(
                """INSERT INTO reflections
                   (id, type, content, context, project, salience, active,
                    no_recall, supersedes, superseded_by, event_date, entities,
                    source_session_id, source_channel, source_message_ids,
                    embedding, created_at, accessed_at, access_count, weight)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    reflection.id,
                    reflection.type.value,
                    reflection.content,
                    reflection.context,
                    reflection.project,
                    reflection.salience,
                    int(reflection.active),
                    int(reflection.no_recall),
                    reflection.supersedes,
                    reflection.superseded_by,
                    reflection.event_date,
                    entities_json,
                    reflection.source_session_id,
                    reflection.source_channel,
                    msg_ids_json,
                    json.dumps(reflection.embedding),
                    now,
                    now,
                    reflection.access_count,
                    reflection.weight,
                ),
            )
            # Also insert into sqlite-vec virtual table
            if self._vec_available and reflection.embedding:
                # Lazily create vec table on first embedding insert
                if self._vec_dimensions == 0:
                    self._create_vec_table(len(reflection.embedding))
                if len(reflection.embedding) == self._vec_dimensions:
                    try:
                        blob = self._embedding_to_blob(reflection.embedding)
                        self._conn.execute(
                            "INSERT INTO reflections_vec(rowid, embedding) VALUES (?, ?)",
                            (cursor.lastrowid, blob),
                        )
                    except Exception:
                        pass  # non-fatal — vector search degrades to numpy fallback

            self._conn.commit()
            return reflection

    # ── Get ──

    def get(self, reflection_id: str) -> Reflection | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_reflection(row)

    # ── Supersession ──

    def deactivate_superseded(self, superseded_id: str, superseded_by: str = "") -> None:
        with self._lock:
            if superseded_by:
                self._conn.execute(
                    "UPDATE reflections SET active = 0, superseded_by = ? WHERE id = ?",
                    (superseded_by, superseded_id),
                )
            else:
                self._conn.execute(
                    "UPDATE reflections SET active = 0 WHERE id = ?", (superseded_id,)
                )
            self._conn.commit()

    # ── No-recall flag ──

    def set_no_recall(self, reflection_id: str, no_recall: bool) -> None:
        """Set the no_recall flag on a reflection."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET no_recall = ? WHERE id = ?",
                (int(no_recall), reflection_id),
            )
            self._conn.commit()

    # ── Memory linking ──

    def create_link(
        self,
        source_id: str,
        target_id: str,
        similarity: float,
    ) -> bool:
        """Create a bidirectional link between two reflections.

        Inserts both (source→target) and (target→source) rows.
        Returns True if at least one new link was created, False if both already existed.
        """
        created = False
        with self._lock:
            for a, b in [(source_id, target_id), (target_id, source_id)]:
                try:
                    self._conn.execute(
                        "INSERT INTO reflection_links (source_id, target_id, similarity) "
                        "VALUES (?, ?, ?)",
                        (a, b, similarity),
                    )
                    created = True
                except sqlite3.IntegrityError:
                    pass  # link already exists
            self._conn.commit()
        return created

    def get_links(
        self,
        reflection_id: str,
        limit: int = MAX_LINKS_PER_MEMORY,
    ) -> list[ReflectionLink]:
        """Get links for a reflection, ordered by similarity descending."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, source_id, target_id, similarity, created_at "
                "FROM reflection_links "
                "WHERE source_id = ? "
                "ORDER BY similarity DESC LIMIT ?",
                (reflection_id, limit),
            ).fetchall()
            return [
                ReflectionLink(
                    id=row["id"],
                    source_id=row["source_id"],
                    target_id=row["target_id"],
                    similarity=row["similarity"],
                    created_at=datetime.fromisoformat(row["created_at"])
                    if row["created_at"]
                    else datetime.now(timezone.utc),
                )
                for row in rows
            ]

    def get_active_with_embeddings(
        self,
        since: datetime | None = None,
    ) -> list[Reflection]:
        """Fetch active reflections that have non-empty embeddings.

        Args:
            since: If provided, only return reflections created at or after this time.
        """
        with self._lock:
            if since:
                rows = self._conn.execute(
                    "SELECT * FROM reflections "
                    "WHERE active = 1 AND embedding != '[]' AND created_at >= ?",
                    (since.isoformat(),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM reflections WHERE active = 1 AND embedding != '[]'"
                ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def prune_orphan_links(self) -> int:
        """Delete links where either endpoint is inactive or missing.

        Returns the number of pruned link rows.
        """
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM reflection_links "
                "WHERE source_id NOT IN (SELECT id FROM reflections WHERE active = 1) "
                "   OR target_id NOT IN (SELECT id FROM reflections WHERE active = 1)"
            )
            self._conn.commit()
            return cursor.rowcount

    # ── Search (vector cosine similarity) ──

    def search_by_embedding_scored(
        self,
        query_embedding: list[float],
        limit: int = 10,
        active_only: bool = True,
        type_filter: ReflectionType | None = None,
        project_filter: str = "",
        min_weight: float = 0.0,
        recency_factor: float = 0.0,
        access_boost: float = 0.0,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Vector search returning (final_score, Reflection) pairs.

        Uses sqlite-vec indexed search when available, falls back to O(n) numpy scan.

        If recency_factor > 0, applies retrieval-time scoring:
            final_score = similarity * weight * (recency_factor ^ hours_since_access)
        If access_boost > 0, boosts weight on each accessed reflection.
        If entity_filter is set, only returns reflections tagged with that entity.
        """
        with self._lock:
            if (
                self._vec_available
                and len(query_embedding) == self._vec_dimensions
            ):
                return self._search_by_vec(
                    query_embedding, limit, active_only,
                    type_filter, project_filter, min_weight,
                    recency_factor, access_boost, entity_filter,
                    type_exclude,
                )
            return self._search_by_numpy(
                query_embedding, limit, active_only,
                type_filter, project_filter, min_weight,
                recency_factor, access_boost, entity_filter,
                type_exclude,
            )

    def _search_by_vec(
        self,
        query_embedding: list[float],
        limit: int,
        active_only: bool,
        type_filter: ReflectionType | None,
        project_filter: str,
        min_weight: float,
        recency_factor: float = 0.0,
        access_boost: float = 0.0,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """sqlite-vec indexed vector search (cosine distance)."""
        # Fetch more candidates than needed so post-filtering still returns enough
        fetch_k = limit * 5
        query_blob = self._embedding_to_blob(query_embedding)

        try:
            vec_rows = self._conn.execute(
                "SELECT rowid, distance FROM reflections_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query_blob, fetch_k),
            ).fetchall()
        except Exception:
            # Fall back to numpy on any vec query failure
            return self._search_by_numpy(
                query_embedding, limit, active_only,
                type_filter, project_filter, min_weight,
                recency_factor, access_boost, entity_filter,
            )

        if not vec_rows:
            return []

        # Build filter conditions for the main table
        where_clauses = []
        params: list = []
        if active_only:
            where_clauses.append("active = 1")
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter.value)
        if type_exclude:
            placeholders = ",".join("?" * len(type_exclude))
            where_clauses.append(f"type NOT IN ({placeholders})")
            params.extend(t.value for t in type_exclude)
        if project_filter:
            where_clauses.append("project = ?")
            params.append(project_filter)
        if min_weight > 0:
            where_clauses.append("weight >= ?")
            params.append(min_weight)
        if entity_filter:
            where_clauses.append("entities LIKE ?")
            params.append(f'%"{entity_filter.lower()}"%')

        # Always exclude no_recall reflections from search
        where_clauses.append("no_recall = 0")

        candidates = []
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        touch_ids = []

        for rowid, distance in vec_rows:
            similarity = 1.0 - distance  # cosine distance → cosine similarity
            # Fetch the full reflection row and apply filters
            filter_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
            row = self._conn.execute(
                f"SELECT * FROM reflections WHERE rowid = ? AND {filter_sql}",
                [rowid, *params],
            ).fetchone()
            if row is None:
                continue
            ref = self._row_to_reflection(row)
            touch_ids.append(ref.id)

            # Apply retrieval-time recency scoring
            if recency_factor > 0:
                hours_since = max(0, (now_dt - ref.accessed_at).total_seconds() / 3600)
                recency_boost = recency_factor ** hours_since
                final_score = similarity * ref.weight * recency_boost
            else:
                final_score = similarity

            candidates.append((final_score, ref))

        # Re-sort by final score and take top-limit
        candidates.sort(key=lambda x: x[0], reverse=True)
        results = candidates[:limit]
        result_ids = {ref.id for _, ref in results}

        # Batch access tracking (only for returned results)
        for rid in touch_ids:
            if rid in result_ids:
                if access_boost > 0:
                    self._conn.execute(
                        "UPDATE reflections SET accessed_at = ?, access_count = access_count + 1, "
                        "weight = MIN(1.0, weight + ?) WHERE id = ?",
                        (now, access_boost, rid),
                    )
                else:
                    self._conn.execute(
                        "UPDATE reflections SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                        (now, rid),
                    )
        if touch_ids:
            self._conn.commit()

        return results

    def _search_by_numpy(
        self,
        query_embedding: list[float],
        limit: int,
        active_only: bool,
        type_filter: ReflectionType | None,
        project_filter: str,
        min_weight: float,
        recency_factor: float = 0.0,
        access_boost: float = 0.0,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Fallback O(n) numpy cosine similarity scan."""
        where_clauses = []
        params: list = []

        if active_only:
            where_clauses.append("active = 1")
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter.value)
        if type_exclude:
            placeholders = ",".join("?" * len(type_exclude))
            where_clauses.append(f"type NOT IN ({placeholders})")
            params.extend(t.value for t in type_exclude)
        if project_filter:
            where_clauses.append("project = ?")
            params.append(project_filter)
        if min_weight > 0:
            where_clauses.append("weight >= ?")
            params.append(min_weight)
        if entity_filter:
            where_clauses.append("entities LIKE ?")
            params.append(f'%"{entity_filter.lower()}"%')
        # Always exclude no_recall reflections from search
        where_clauses.append("no_recall = 0")

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        rows = self._conn.execute(
            f"SELECT * FROM reflections WHERE {where_sql}", params
        ).fetchall()

        if not rows:
            return []

        query_vec = np.array(query_embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            emb = json.loads(row["embedding"])
            if not emb:
                continue
            row_vec = np.array(emb, dtype=np.float32)
            row_norm = np.linalg.norm(row_vec)
            if row_norm == 0:
                continue
            similarity = float(np.dot(query_vec, row_vec) / (query_norm * row_norm))

            # Apply retrieval-time recency scoring
            if recency_factor > 0:
                try:
                    accessed = datetime.fromisoformat(row["accessed_at"])
                except (ValueError, TypeError):
                    accessed = now_dt
                hours_since = max(0, (now_dt - accessed).total_seconds() / 3600)
                recency_boost = recency_factor ** hours_since
                final_score = similarity * row["weight"] * recency_boost
            else:
                final_score = similarity

            scored.append((final_score, row))

        scored.sort(key=lambda x: x[0], reverse=True)

        results = []
        touch_ids = []
        for score, row in scored[:limit]:
            ref = self._row_to_reflection(row)
            touch_ids.append(ref.id)
            results.append((score, ref))

        # Batch access tracking into a single commit
        for rid in touch_ids:
            if access_boost > 0:
                self._conn.execute(
                    "UPDATE reflections SET accessed_at = ?, access_count = access_count + 1, "
                    "weight = MIN(1.0, weight + ?) WHERE id = ?",
                    (now, access_boost, rid),
                )
            else:
                self._conn.execute(
                    "UPDATE reflections SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                    (now, rid),
                )
        if touch_ids:
            self._conn.commit()

        return results

    def search_by_embedding(
        self,
        query_embedding: list[float],
        limit: int = 10,
        active_only: bool = True,
        type_filter: ReflectionType | None = None,
        project_filter: str = "",
        min_weight: float = 0.0,
        entity_filter: str = "",
    ) -> list[Reflection]:
        """Vector search returning Reflections (drops scores)."""
        return [
            ref for _, ref in self.search_by_embedding_scored(
                query_embedding, limit, active_only, type_filter, project_filter, min_weight,
                entity_filter=entity_filter,
            )
        ]

    # ── Search (keyword — FTS5/BM25 with LIKE fallback) ──

    def search_by_keyword_scored(
        self,
        query: str,
        limit: int = 10,
        active_only: bool = True,
        type_filter: ReflectionType | None = None,
        project_filter: str = "",
        min_weight: float = 0.0,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Keyword search returning (score, Reflection) pairs.

        Uses FTS5/BM25 when available, falls back to LIKE-based token matching.
        Empty query returns all matching reflections (browse mode).
        """
        with self._lock:
            # Empty query → browse mode (no FTS5 needed)
            tokens = [t.strip() for t in query.split() if len(t.strip()) > 1]
            if not tokens:
                return self._browse_all(limit, active_only, type_filter, project_filter, min_weight, entity_filter, type_exclude)

            if self._fts5_available:
                return self._search_by_fts5(
                    query, limit, active_only, type_filter, project_filter, min_weight, entity_filter, type_exclude,
                )
            return self._search_by_like(
                query, limit, active_only, type_filter, project_filter, min_weight, entity_filter, type_exclude,
            )

    def _browse_all(
        self,
        limit: int,
        active_only: bool,
        type_filter: ReflectionType | None,
        project_filter: str,
        min_weight: float,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Return all reflections matching filters (no text search)."""
        where_clauses: list[str] = []
        params: list = []
        if active_only:
            where_clauses.append("active = 1")
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter.value)
        if type_exclude:
            placeholders = ",".join("?" * len(type_exclude))
            where_clauses.append(f"type NOT IN ({placeholders})")
            params.extend(t.value for t in type_exclude)
        if project_filter:
            where_clauses.append("project = ?")
            params.append(project_filter)
        if min_weight > 0:
            where_clauses.append("weight >= ?")
            params.append(min_weight)
        if entity_filter:
            where_clauses.append("entities LIKE ?")
            params.append(f'%"{entity_filter.lower()}"%')
        # Always exclude no_recall reflections from search
        where_clauses.append("no_recall = 0")
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM reflections WHERE {where_sql} ORDER BY salience DESC, created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()
        results = []
        for row in rows:
            ref = self._row_to_reflection(row)
            self._touch(ref.id)
            results.append((1.0, ref))
        return results

    def _search_by_fts5(
        self,
        query: str,
        limit: int,
        active_only: bool,
        type_filter: ReflectionType | None,
        project_filter: str,
        min_weight: float,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """FTS5/BM25-ranked keyword search."""
        # Build an FTS5 match expression from query tokens
        tokens = [t.strip() for t in query.split() if len(t.strip()) > 1]
        if not tokens:
            return []

        # Use OR matching so partial keyword hits still return results
        fts_query = " OR ".join(f'"{t}"' for t in tokens)

        # Join FTS5 with the main table for filtering + full row data.
        # FTS5 MATCH goes in the WHERE clause alongside other filters.
        where_clauses = [
            "reflections_fts MATCH ?",
            "reflections.id = reflections_fts.id",
        ]
        params: list = [fts_query]

        if active_only:
            where_clauses.append("reflections.active = 1")
        if type_filter:
            where_clauses.append("reflections.type = ?")
            params.append(type_filter.value)
        if type_exclude:
            placeholders = ",".join("?" * len(type_exclude))
            where_clauses.append(f"reflections.type NOT IN ({placeholders})")
            params.extend(t.value for t in type_exclude)
        if project_filter:
            where_clauses.append("reflections.project = ?")
            params.append(project_filter)
        if min_weight > 0:
            where_clauses.append("reflections.weight >= ?")
            params.append(min_weight)
        if entity_filter:
            where_clauses.append("reflections.entities LIKE ?")
            params.append(f'%"{entity_filter.lower()}"%')
        # Always exclude no_recall reflections from search
        where_clauses.append("reflections.no_recall = 0")

        where_sql = " AND ".join(where_clauses)

        sql = (
            "SELECT reflections.*, bm25(reflections_fts) AS rank "
            "FROM reflections_fts, reflections "
            f"WHERE {where_sql} "
            "ORDER BY rank "
            "LIMIT ?"
        )
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
        except Exception:
            # FTS5 query syntax error — fall back to LIKE
            return self._search_by_like(
                query, limit, active_only, type_filter, project_filter, min_weight, entity_filter,
            )

        if not rows:
            return []

        # Normalise BM25 scores to [0, 1] range (bm25 returns negative, lower is better)
        # Filter out rows with None rank (can happen with corrupted DB / malformed FTS index)
        valid_rows = [row for row in rows if row["rank"] is not None]
        if not valid_rows:
            return []

        raw_scores = [row["rank"] for row in valid_rows]
        worst = min(raw_scores)  # most negative = worst match
        best = max(raw_scores)   # closest to 0 = best match
        score_range = best - worst if best != worst else 1.0

        results = []
        for row in valid_rows:
            ref = self._row_to_reflection(row)
            self._touch(ref.id)
            # Normalise: best match → 1.0, worst → 0.0
            normalised = (row["rank"] - worst) / score_range if score_range else 1.0
            results.append((normalised, ref))

        return results

    def _search_by_like(
        self,
        query: str,
        limit: int,
        active_only: bool,
        type_filter: ReflectionType | None,
        project_filter: str,
        min_weight: float,
        entity_filter: str = "",
        type_exclude: list[ReflectionType] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Fallback LIKE-based keyword search (no FTS5)."""
        where_clauses: list[str] = []
        params: list = []

        if active_only:
            where_clauses.append("active = 1")
        if type_filter:
            where_clauses.append("type = ?")
            params.append(type_filter.value)
        if type_exclude:
            placeholders = ",".join("?" * len(type_exclude))
            where_clauses.append(f"type NOT IN ({placeholders})")
            params.extend(t.value for t in type_exclude)
        if project_filter:
            where_clauses.append("project = ?")
            params.append(project_filter)
        if min_weight > 0:
            where_clauses.append("weight >= ?")
            params.append(min_weight)
        if entity_filter:
            where_clauses.append("entities LIKE ?")
            params.append(f'%"{entity_filter.lower()}"%')
        # Always exclude no_recall reflections from search
        where_clauses.append("no_recall = 0")

        tokens = [t.strip() for t in query.lower().split() if len(t.strip()) > 2]
        if tokens:
            like_parts = []
            for token in tokens:
                like_parts.append("(LOWER(content) LIKE ? OR LOWER(context) LIKE ?)")
                params.extend([f"%{token}%", f"%{token}%"])
            where_clauses.append(f"({' OR '.join(like_parts)})")

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        rows = self._conn.execute(
            f"SELECT * FROM reflections WHERE {where_sql} ORDER BY salience DESC, created_at DESC LIMIT ?",
            [*params, limit],
        ).fetchall()

        num_tokens = len(tokens) if tokens else 1
        results = []
        for row in rows:
            ref = self._row_to_reflection(row)
            self._touch(ref.id)
            combined = (ref.content + " " + ref.context).lower()
            hits = sum(1 for t in tokens if t in combined) if tokens else 0
            score = hits / num_tokens
            results.append((score, ref))

        return results

    def search_by_keyword(
        self,
        query: str,
        limit: int = 10,
        active_only: bool = True,
        type_filter: ReflectionType | None = None,
        project_filter: str = "",
        min_weight: float = 0.0,
        entity_filter: str = "",
    ) -> list[Reflection]:
        """Keyword search returning Reflections (drops scores)."""
        return [
            ref for _, ref in self.search_by_keyword_scored(
                query, limit, active_only, type_filter, project_filter, min_weight,
                entity_filter=entity_filter,
            )
        ]

    # ── Access tracking ──

    def _touch(self, reflection_id: str) -> None:
        self._conn.execute(
            "UPDATE reflections SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
            (_now_iso(), reflection_id),
        )
        self._conn.commit()

    # ── High-salience retrieval ──

    def get_by_min_salience(
        self,
        min_salience: int = 4,
        limit: int = 50,
        active_only: bool = True,
    ) -> list[Reflection]:
        """Return active reflections with salience >= threshold.

        Ordered by salience DESC, then created_at DESC.
        Uses idx_reflections_salience index.
        """
        with self._lock:
            where_clauses = ["salience >= ?", "no_recall = 0"]
            params: list = [min_salience]
            if active_only:
                where_clauses.append("active = 1")
            where_sql = " AND ".join(where_clauses)
            rows = self._conn.execute(
                f"SELECT * FROM reflections WHERE {where_sql} "
                "ORDER BY salience DESC, created_at DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    # ── Near-duplicate detection ──

    def find_near_duplicate(
        self,
        embedding: list[float],
        threshold: float = 0.90,
        active_only: bool = True,
    ) -> tuple[float, Reflection] | None:
        """Find the closest active reflection by cosine similarity.

        Returns (similarity, reflection) if best match > threshold, else None.
        Uses sqlite-vec when available, falls back to O(n) numpy scan.
        Does not update access tracking (internal check only).
        """
        if not embedding:
            return None

        with self._lock:
            if (
                self._vec_available
                and len(embedding) == self._vec_dimensions
            ):
                return self._find_near_duplicate_vec(embedding, threshold, active_only)
            return self._find_near_duplicate_numpy(embedding, threshold, active_only)

    def _find_near_duplicate_vec(
        self,
        embedding: list[float],
        threshold: float,
        active_only: bool,
    ) -> tuple[float, Reflection] | None:
        """sqlite-vec based near-duplicate check."""
        query_blob = self._embedding_to_blob(embedding)
        max_distance = 1.0 - threshold  # cosine distance threshold

        try:
            # Fetch top-5 candidates — we just need the best match above threshold
            vec_rows = self._conn.execute(
                "SELECT rowid, distance FROM reflections_vec "
                "WHERE embedding MATCH ? AND k = 5 ORDER BY distance",
                (query_blob,),
            ).fetchall()
        except Exception:
            return self._find_near_duplicate_numpy(embedding, threshold, active_only)

        for rowid, distance in vec_rows:
            if distance > max_distance:
                break  # results are ordered by distance — no more matches
            where_sql = "rowid = ? AND active = 1" if active_only else "rowid = ?"
            row = self._conn.execute(
                f"SELECT * FROM reflections WHERE {where_sql}", (rowid,)
            ).fetchone()
            if row is not None:
                similarity = 1.0 - distance
                return (similarity, self._row_to_reflection(row))

        return None

    def _find_near_duplicate_numpy(
        self,
        embedding: list[float],
        threshold: float,
        active_only: bool,
    ) -> tuple[float, Reflection] | None:
        """Fallback O(n) numpy near-duplicate scan."""
        where_sql = "active = 1" if active_only else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM reflections WHERE {where_sql}"
        ).fetchall()

        if not rows:
            return None

        query_vec = np.array(embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return None

        best_sim = -1.0
        best_row = None
        for row in rows:
            emb = json.loads(row["embedding"])
            if not emb:
                continue
            row_vec = np.array(emb, dtype=np.float32)
            row_norm = np.linalg.norm(row_vec)
            if row_norm == 0:
                continue
            similarity = float(np.dot(query_vec, row_vec) / (query_norm * row_norm))
            if similarity > best_sim:
                best_sim = similarity
                best_row = row

        if best_row is not None and best_sim > threshold:
            return (best_sim, self._row_to_reflection(best_row))
        return None

    # ── Introspect ──

    def introspect(
        self,
        timeframe: str = "all",
        type_filter: ReflectionType | None = None,
        project_filter: str = "",
    ) -> dict:
        with self._lock:
            where_clauses = ["active = 1"]
            params: list = []

            if timeframe != "all":
                cutoff = self._timeframe_cutoff(timeframe)
                where_clauses.append("created_at >= ?")
                params.append(cutoff.isoformat())

            if type_filter:
                where_clauses.append("type = ?")
                params.append(type_filter.value)
            if project_filter:
                where_clauses.append("project = ?")
                params.append(project_filter)

            where_sql = " AND ".join(where_clauses)

            # Total count
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM reflections WHERE {where_sql}", params
            ).fetchone()[0]

            # By type
            by_type = {}
            rows = self._conn.execute(
                f"SELECT type, COUNT(*) as cnt FROM reflections WHERE {where_sql} GROUP BY type ORDER BY cnt DESC",
                params,
            ).fetchall()
            for row in rows:
                by_type[row["type"]] = row["cnt"]

            # By project
            by_project = {}
            rows = self._conn.execute(
                f"SELECT project, COUNT(*) as cnt FROM reflections WHERE {where_sql} AND project != '' GROUP BY project ORDER BY cnt DESC",
                params,
            ).fetchall()
            for row in rows:
                by_project[row["project"]] = row["cnt"]

            # By salience
            by_salience = {}
            rows = self._conn.execute(
                f"SELECT salience, COUNT(*) as cnt FROM reflections WHERE {where_sql} GROUP BY salience ORDER BY salience DESC",
                params,
            ).fetchall()
            for row in rows:
                by_salience[str(row["salience"])] = row["cnt"]

            # Recent (last 5)
            recent_rows = self._conn.execute(
                f"SELECT * FROM reflections WHERE {where_sql} ORDER BY created_at DESC LIMIT 5",
                params,
            ).fetchall()
            recent = [
                {"id": r["id"], "type": r["type"], "content": r["content"][:100], "salience": r["salience"]}
                for r in recent_rows
            ]

            return {
                "timeframe": timeframe,
                "total_reflections": total,
                "by_type": by_type,
                "by_project": by_project,
                "by_salience": by_salience,
                "recent": recent,
            }

    @staticmethod
    def _timeframe_cutoff(timeframe: str) -> datetime:
        now = datetime.now(timezone.utc)
        if timeframe == "day":
            return now - timedelta(days=1)
        elif timeframe == "week":
            return now - timedelta(weeks=1)
        elif timeframe == "month":
            return now - timedelta(days=30)
        return datetime.min.replace(tzinfo=timezone.utc)

    # ── Helpers ──

    @staticmethod
    def _row_to_reflection(row: sqlite3.Row) -> Reflection:
        # Handle optional columns that may not exist in older schemas
        keys = row.keys() if hasattr(row, "keys") else []
        superseded_by = row["superseded_by"] if "superseded_by" in keys else ""
        event_date = row["event_date"] if "event_date" in keys else None
        raw_entities = row["entities"] if "entities" in keys else None
        entities = json.loads(raw_entities) if raw_entities else []
        no_recall = bool(row["no_recall"]) if "no_recall" in keys else False
        source_session_id = row["source_session_id"] if "source_session_id" in keys else None
        source_channel = row["source_channel"] if "source_channel" in keys else None
        raw_msg_ids = row["source_message_ids"] if "source_message_ids" in keys else None
        source_message_ids = json.loads(raw_msg_ids) if raw_msg_ids else []
        next_review_date = row["next_review_date"] if "next_review_date" in keys else None
        review_interval_days = row["review_interval_days"] if "review_interval_days" in keys else 7
        return Reflection(
            id=row["id"],
            type=ReflectionType(row["type"]),
            content=row["content"],
            context=row["context"],
            project=row["project"],
            salience=row["salience"],
            active=bool(row["active"]),
            no_recall=no_recall,
            supersedes=row["supersedes"],
            superseded_by=superseded_by,
            event_date=event_date,
            entities=entities,
            source_session_id=source_session_id,
            source_channel=source_channel,
            source_message_ids=source_message_ids,
            embedding=json.loads(row["embedding"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            accessed_at=datetime.fromisoformat(row["accessed_at"]),
            access_count=row["access_count"],
            weight=row["weight"],
            next_review_date=next_review_date,
            review_interval_days=review_interval_days or 7,
        )

    # ── Garbage collection ──

    def gc_inactive(self, max_age_days: int = 30) -> int:
        """Hard-delete inactive (active=0) reflections older than max_age_days.

        Respects supersession chains: only deletes an inactive reflection if
        its superseding replacement (superseded_by) is still active, ensuring
        we never lose the last record of something.

        Also cleans up corresponding entries in the vec table.
        Returns the number of deleted rows.
        """
        with self._lock:
            cutoff = (
                datetime.now(timezone.utc) - timedelta(days=max_age_days)
            ).isoformat()

            # Find candidates for deletion
            candidates = self._conn.execute(
                "SELECT id, rowid, superseded_by FROM reflections "
                "WHERE active = 0 AND created_at < ?",
                (cutoff,),
            ).fetchall()

            delete_ids = []
            delete_rowids = []
            for row in candidates:
                ref_id = row["id"]
                rowid = row["rowid"]
                replacement_id = row["superseded_by"]

                # If there's a supersession chain, only GC if the replacement exists and is active
                if replacement_id:
                    replacement = self._conn.execute(
                        "SELECT active FROM reflections WHERE id = ?",
                        (replacement_id,),
                    ).fetchone()
                    if replacement is None or not replacement["active"]:
                        # Replacement is gone or also inactive — keep this record
                        continue

                delete_ids.append(ref_id)
                delete_rowids.append(rowid)

            if not delete_ids:
                return 0

            # Clean up vec table entries
            if self._vec_available:
                for rowid in delete_rowids:
                    try:
                        self._conn.execute(
                            "DELETE FROM reflections_vec WHERE rowid = ?", (rowid,)
                        )
                    except Exception:
                        pass

            # Delete the reflections
            placeholders = ",".join("?" for _ in delete_ids)
            cursor = self._conn.execute(
                f"DELETE FROM reflections WHERE id IN ({placeholders})",
                delete_ids,
            )
            deleted = cursor.rowcount
            if deleted:
                self._conn.commit()
            return deleted

    # ── Post-extraction consolidation (Phase 3a) ──

    def consolidate_batch(
        self,
        reflection_ids: list[str],
        *,
        merge_threshold: float = 0.85,
        review_band: float = 0.75,
        llm_classify: "callable | None" = None,
        max_batch: int = 20,
    ) -> int:
        """Merge near-duplicate reflections from a recently inserted batch.

        For each new reflection, queries k-NN (k=5) against the full store.
        Computes affinity = 0.7 * cosine + 0.3 * temporal_proximity.

        - affinity >= merge_threshold: auto-dedup (keep higher salience / longer)
        - review_band <= affinity < merge_threshold: call llm_classify (if provided)
        - affinity < review_band: keep both

        Returns count of deduplicated reflections.
        """
        if not reflection_ids:
            return 0

        with self._lock:
            # Fetch the batch reflections
            placeholders = ",".join("?" for _ in reflection_ids)
            rows = self._conn.execute(
                f"SELECT * FROM reflections WHERE id IN ({placeholders}) AND active = 1",
                reflection_ids,
            ).fetchall()

            batch = [self._row_to_reflection(row) for row in rows]
            if len(batch) < 1:
                return 0

            # Cap batch size
            batch = batch[:max_batch]

            merged = 0
            deactivated_ids: set[str] = set()

            for ref in batch:
                if ref.id in deactivated_ids or not ref.embedding:
                    continue

                # Find k-NN candidates from the FULL store (excluding self)
                candidates = self._knn_for_consolidation(ref, k=5, exclude_ids=deactivated_ids)

                for sim, candidate in candidates:
                    if candidate.id in deactivated_ids or candidate.id == ref.id:
                        continue

                    # Compute temporal proximity
                    temporal = self._temporal_proximity(ref.created_at, candidate.created_at)
                    affinity = 0.7 * sim + 0.3 * temporal

                    if affinity >= merge_threshold:
                        # Auto-dedup: keep higher salience, or longer content on tie
                        winner, loser = self._pick_winner(ref, candidate)
                        self._conn.execute(
                            "UPDATE reflections SET active = 0, superseded_by = ? WHERE id = ?",
                            (winner.id, loser.id),
                        )
                        deactivated_ids.add(loser.id)
                        merged += 1

                    elif affinity >= review_band and llm_classify is not None:
                        # LLM review band
                        try:
                            verdict = llm_classify(ref.content, candidate.content)
                        except Exception:
                            verdict = "different"

                        if verdict == "same":
                            winner, loser = self._pick_winner(ref, candidate)
                            self._conn.execute(
                                "UPDATE reflections SET active = 0, superseded_by = ? WHERE id = ?",
                                (winner.id, loser.id),
                            )
                            deactivated_ids.add(loser.id)
                            merged += 1
                        elif verdict == "updated":
                            # New supersedes old
                            self._conn.execute(
                                "UPDATE reflections SET active = 0, superseded_by = ? WHERE id = ?",
                                (ref.id, candidate.id),
                            )
                            deactivated_ids.add(candidate.id)
                            merged += 1
                        # "different" → keep both

            if merged:
                self._conn.commit()
            return merged

    def _knn_for_consolidation(
        self,
        ref: Reflection,
        k: int = 5,
        exclude_ids: set[str] | None = None,
    ) -> list[tuple[float, Reflection]]:
        """Find k nearest neighbors for consolidation (no access tracking)."""
        if not ref.embedding:
            return []

        exclude = exclude_ids or set()
        # Try sqlite-vec first, fall back to numpy
        if self._vec_available and len(ref.embedding) == self._vec_dimensions:
            return self._knn_vec(ref, k, exclude)
        return self._knn_numpy(ref, k, exclude)

    def _knn_vec(
        self,
        ref: Reflection,
        k: int,
        exclude: set[str],
    ) -> list[tuple[float, Reflection]]:
        """sqlite-vec based k-NN for consolidation."""
        query_blob = self._embedding_to_blob(ref.embedding)
        fetch_k = k * 3  # over-fetch for filtering

        try:
            vec_rows = self._conn.execute(
                "SELECT rowid, distance FROM reflections_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query_blob, fetch_k),
            ).fetchall()
        except Exception:
            return self._knn_numpy(ref, k, exclude)

        results = []
        for rowid, distance in vec_rows:
            if len(results) >= k:
                break
            similarity = 1.0 - distance
            row = self._conn.execute(
                "SELECT * FROM reflections WHERE rowid = ? AND active = 1",
                (rowid,),
            ).fetchone()
            if row is None:
                continue
            candidate = self._row_to_reflection(row)
            if candidate.id == ref.id or candidate.id in exclude:
                continue
            results.append((similarity, candidate))

        return results

    def _knn_numpy(
        self,
        ref: Reflection,
        k: int,
        exclude: set[str],
    ) -> list[tuple[float, Reflection]]:
        """Fallback O(n) numpy k-NN for consolidation."""
        rows = self._conn.execute(
            "SELECT * FROM reflections WHERE active = 1"
        ).fetchall()

        query_vec = np.array(ref.embedding, dtype=np.float32)
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        scored: list[tuple[float, sqlite3.Row]] = []
        for row in rows:
            rid = row["id"]
            if rid == ref.id or rid in exclude:
                continue
            emb = json.loads(row["embedding"])
            if not emb:
                continue
            row_vec = np.array(emb, dtype=np.float32)
            row_norm = np.linalg.norm(row_vec)
            if row_norm == 0:
                continue
            similarity = float(np.dot(query_vec, row_vec) / (query_norm * row_norm))
            scored.append((similarity, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [(sim, self._row_to_reflection(r)) for sim, r in scored[:k]]

    @staticmethod
    def _temporal_proximity(dt1: datetime, dt2: datetime) -> float:
        """Compute temporal proximity: 1.0 same day, 0.5 same week, 0.0 otherwise."""
        delta = abs((dt1 - dt2).total_seconds())
        if delta < 86400:  # same day
            return 1.0
        elif delta < 604800:  # same week
            return 0.5
        return 0.0

    @staticmethod
    def _pick_winner(a: Reflection, b: Reflection) -> tuple[Reflection, Reflection]:
        """Pick winner for dedup: higher salience wins, then longer content."""
        if a.salience > b.salience:
            return a, b
        elif b.salience > a.salience:
            return b, a
        # Equal salience — keep the longer/more detailed one
        if len(a.content) >= len(b.content):
            return a, b
        return b, a

    # ── Salience decay (Phase 3b) ──

    def apply_decay(
        self,
        *,
        decay_factor: float = 0.97,
        decay_factor_continuation: float = 0.90,
        immunity_salience: int = 4,
        archive_threshold: float = 0.1,
    ) -> int:
        """Apply daily weight decay to all eligible active reflections.

        - fact/insight with salience >= immunity_salience: immune
        - continuation: uses decay_factor_continuation
        - all others: uses decay_factor
        - weight < archive_threshold: soft-archived (active=False)

        Returns count of reflections whose weight was updated.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, type, salience, weight, accessed_at FROM reflections "
                "WHERE active = 1 AND weight > ?",
                (archive_threshold,),
            ).fetchall()

            updates: list[tuple] = []

            for row in rows:
                rtype = row["type"]
                salience = row["salience"]

                # Immunity: high-salience facts and insights
                if salience >= immunity_salience and rtype in ("fact", "insight"):
                    continue

                # Days since last access
                try:
                    accessed = datetime.fromisoformat(row["accessed_at"])
                except (ValueError, TypeError):
                    accessed = now
                days_since = max(0, (now - accessed).total_seconds() / 86400)

                if days_since < 1:
                    continue  # No decay within the first day

                # Type-specific factor
                factor = decay_factor_continuation if rtype == "continuation" else decay_factor

                new_weight = row["weight"] * (factor ** days_since)

                if new_weight < archive_threshold:
                    # Soft-archive
                    updates.append((0.0, 0, row["id"]))
                else:
                    updates.append((round(new_weight, 6), 1, row["id"]))

            if updates:
                self._conn.executemany(
                    "UPDATE reflections SET weight = ?, active = ? WHERE id = ?",
                    updates,
                )
                self._conn.commit()

            return len(updates)

    # ── Retrieval-time access boost (Phase 3b) ──

    def boost_weight_on_access(
        self,
        reflection_id: str,
        boost: float = 0.05,
    ) -> None:
        """Boost a reflection's weight on access (spaced repetition effect)."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET weight = MIN(1.0, weight + ?) WHERE id = ? AND active = 1",
                (boost, reflection_id),
            )
            self._conn.commit()

    # ── Memory hygiene helpers ──

    def log_memory_event(
        self,
        event_type: str,
        source_ids: list[str],
        *,
        target_id: str | None = None,
        prior_content: str | None = None,
        prior_salience: int | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Insert a row into memory_events. Returns the event ID."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO memory_events "
                "(event_type, source_ids, target_id, prior_content, prior_salience, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_type,
                    json.dumps(source_ids),
                    target_id,
                    prior_content,
                    prior_salience,
                    json.dumps(metadata) if metadata else None,
                ),
            )
            self._conn.commit()
            return cursor.lastrowid  # type: ignore[return-value]

    def get_memory_event(self, event_id: int) -> dict | None:
        """Fetch a single memory_event row by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_events WHERE id = ?", (event_id,)
            ).fetchone()
            if not row:
                return None
            keys = row.keys()
            return {k: row[k] for k in keys}

    def revert_memory_event(self, event_id: int) -> bool:
        """Revert a memory hygiene event.

        - decay: restore prior salience/weight and reactivate if archived
        - dedup_merge: restore merged reflection content and reactivate
        - promotion: delete promoted insight and clear promoted_to from sources

        Returns True if reverted, False if event not found or already reversed.
        """
        event = self.get_memory_event(event_id)
        if not event or event.get("reversed_at"):
            return False

        event_type = event["event_type"]
        source_ids = json.loads(event["source_ids"])
        target_id = event.get("target_id")
        meta = json.loads(event["metadata"]) if event.get("metadata") else {}

        with self._lock:
            if event_type == "decay":
                # Restore prior salience and reactivate
                prior_salience = event.get("prior_salience")
                was_archived = meta.get("archived", False)
                for sid in source_ids:
                    updates = []
                    params: list = []
                    if prior_salience is not None:
                        updates.append("weight = ?")
                        params.append(prior_salience)
                    if was_archived:
                        updates.append("active = 1")
                    if updates:
                        params.append(sid)
                        self._conn.execute(
                            f"UPDATE reflections SET {', '.join(updates)} WHERE id = ?",
                            params,
                        )

            elif event_type == "dedup_merge":
                # Reactivate the archived duplicate
                for sid in source_ids:
                    self._conn.execute(
                        "UPDATE reflections SET active = 1, superseded_by = '' WHERE id = ?",
                        (sid,),
                    )
                # Restore prior content if target was modified
                if target_id and meta.get("target_prior_content"):
                    self._conn.execute(
                        "UPDATE reflections SET content = ? WHERE id = ?",
                        (meta["target_prior_content"], target_id),
                    )

            elif event_type == "promotion":
                # Delete promoted insight
                if target_id:
                    self._conn.execute(
                        "DELETE FROM reflections WHERE id = ?", (target_id,)
                    )
                # Clear promoted_to from source metadata (stored in context field)
                for sid in source_ids:
                    ref = self.get(sid)
                    if ref and ref.context:
                        try:
                            ctx = json.loads(ref.context)
                            ctx.pop("promoted_to", None)
                            self._conn.execute(
                                "UPDATE reflections SET context = ? WHERE id = ?",
                                (json.dumps(ctx), sid),
                            )
                        except (json.JSONDecodeError, AttributeError):
                            pass

            else:
                return False

            # Mark event as reversed
            self._conn.execute(
                "UPDATE memory_events SET reversed_at = ? WHERE id = ?",
                (_now_iso(), event_id),
            )
            self._conn.commit()
            return True

    def get_active_reflections_for_decay(
        self,
        min_age_days: int = 0,
    ) -> list[Reflection]:
        """Fetch all active reflections, optionally filtered by minimum age."""
        with self._lock:
            if min_age_days > 0:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=min_age_days)
                ).isoformat()
                rows = self._conn.execute(
                    "SELECT * FROM reflections WHERE active = 1 AND created_at <= ?",
                    (cutoff,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM reflections WHERE active = 1"
                ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def update_salience_weight(
        self,
        reflection_id: str,
        new_weight: float,
        active: bool = True,
    ) -> None:
        """Update a reflection's weight and optionally deactivate."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET weight = ?, active = ? WHERE id = ?",
                (round(new_weight, 6), int(active), reflection_id),
            )
            self._conn.commit()

    def update_content(
        self,
        reflection_id: str,
        content: str,
    ) -> None:
        """Update a reflection's content."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET content = ? WHERE id = ?",
                (content, reflection_id),
            )
            self._conn.commit()

    # ── Spaced review methods ──

    def get_memories_due_for_review(self, limit: int = 20) -> list[Reflection]:
        """Return active memories where next_review_date <= today, oldest due first."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM reflections
                   WHERE active = 1
                     AND next_review_date IS NOT NULL
                     AND next_review_date <= ?
                   ORDER BY next_review_date ASC
                   LIMIT ?""",
                (today, limit),
            ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def confirm_review(self, reflection_id: str) -> None:
        """Mark reviewed: double the interval (capped at 180) and set next review date."""
        with self._lock:
            row = self._conn.execute(
                "SELECT review_interval_days FROM reflections WHERE id = ?",
                (reflection_id,),
            ).fetchone()
            if not row:
                return
            current_interval = row["review_interval_days"] or 7
            new_interval = min(current_interval * 2, 180)
            next_date = (
                datetime.now(timezone.utc) + timedelta(days=new_interval)
            ).strftime("%Y-%m-%d")
            self._conn.execute(
                """UPDATE reflections
                   SET review_interval_days = ?,
                       next_review_date = ?
                   WHERE id = ?""",
                (new_interval, next_date, reflection_id),
            )
            self._conn.commit()

    def schedule_review(
        self,
        reflection_id: str,
        interval_days: int = 7,
    ) -> None:
        """Set initial review schedule on a new or updated memory."""
        next_date = (
            datetime.now(timezone.utc) + timedelta(days=interval_days)
        ).strftime("%Y-%m-%d")
        with self._lock:
            self._conn.execute(
                """UPDATE reflections
                   SET review_interval_days = ?,
                       next_review_date = ?
                   WHERE id = ?""",
                (interval_days, next_date, reflection_id),
            )
            self._conn.commit()

    def get_orphan_memories(
        self,
        min_age_days: int = 30,
        protect_salience: int = 4,
    ) -> list[Reflection]:
        """Find active memories with no entities, never accessed, old enough, low salience."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=min_age_days)
        ).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM reflections
                   WHERE active = 1
                     AND (entities IS NULL OR entities = '[]')
                     AND access_count = 0
                     AND created_at < ?
                     AND salience < ?""",
                (cutoff, protect_salience),
            ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def archive_reflection(self, reflection_id: str, reason: str = "") -> None:
        """Deactivate a reflection (soft-delete). Logs a memory event."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET active = 0, next_review_date = NULL WHERE id = ?",
                (reflection_id,),
            )
            self._conn.commit()
        if reason:
            self.log_memory_event(
                "archive",
                [reflection_id],
                metadata={"reason": reason},
            )

    # ── Structured query ──

    def query(self, filters: MemoryQueryFilters) -> tuple[list[Reflection], int]:
        """Execute a structured query against reflections.

        Returns (results, total_count) where total_count is the unLIMITed count.
        All filtering is parameterized — no raw SQL from callers.
        """
        if filters.preset:
            filters = resolve_preset(filters)

        clauses: list[str] = []
        params: dict[str, object] = {}

        # active filter
        clauses.append("active = :active")
        params["active"] = 1 if filters.active else 0

        # type filter
        if filters.type:
            clauses.append("type = :type")
            params["type"] = filters.type

        # project filter (LIKE for partial match)
        if filters.project:
            clauses.append("project LIKE :project")
            params["project"] = f"%{filters.project}%"

        # entity filter (JSON array stored as TEXT)
        if filters.entity:
            clauses.append("entities LIKE :entity")
            params["entity"] = f"%{filters.entity.lower()}%"

        # salience range
        if filters.salience_min is not None:
            clauses.append("salience >= :salience_min")
            params["salience_min"] = filters.salience_min

        if filters.salience_max is not None:
            clauses.append("salience <= :salience_max")
            params["salience_max"] = filters.salience_max

        # date filters
        if filters.created_after:
            clauses.append("created_at >= :created_after")
            params["created_after"] = filters.created_after

        if filters.created_before:
            clauses.append("created_at <= :created_before")
            params["created_before"] = filters.created_before

        if filters.accessed_after:
            clauses.append("accessed_at >= :accessed_after")
            params["accessed_after"] = filters.accessed_after

        if filters.accessed_before:
            clauses.append("accessed_at <= :accessed_before")
            params["accessed_before"] = filters.accessed_before

        # due_for_review: next_review_date <= today
        if filters.due_for_review:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            clauses.append("next_review_date IS NOT NULL AND next_review_date <= :today")
            params["today"] = today

        # has_links: check reflection_links table
        if filters.has_links is True:
            clauses.append(
                "id IN (SELECT DISTINCT source_id FROM reflection_links "
                "UNION SELECT DISTINCT target_id FROM reflection_links)"
            )
        elif filters.has_links is False:
            clauses.append(
                "id NOT IN (SELECT DISTINCT source_id FROM reflection_links "
                "UNION SELECT DISTINCT target_id FROM reflection_links)"
            )

        # orphan mode: access_count=0, no entities
        if filters.orphan_mode:
            clauses.append("access_count = 0")
            clauses.append("(entities IS NULL OR entities = '[]')")

        where = " AND ".join(clauses) if clauses else "1=1"

        # Validate sort_by against allowlist (defense in depth)
        allowed_sorts = {"created_at", "accessed_at", "salience", "access_count"}
        sort_col = filters.sort_by if filters.sort_by in allowed_sorts else "created_at"
        sort_direction = "ASC" if filters.sort_dir == "asc" else "DESC"

        with self._lock:
            # Get total count (without LIMIT/OFFSET)
            count_sql = f"SELECT COUNT(*) FROM reflections WHERE {where}"
            total = self._conn.execute(count_sql, params).fetchone()[0]

            # Get results
            query_sql = (
                f"SELECT * FROM reflections WHERE {where} "
                f"ORDER BY {sort_col} {sort_direction} "
                f"LIMIT :limit OFFSET :offset"
            )
            params["limit"] = filters.limit
            params["offset"] = filters.offset

            rows = self._conn.execute(query_sql, params).fetchall()
            results = [self._row_to_reflection(row) for row in rows]

        return results, total

    def get_recent_reflections(
        self,
        hours: int = 2,
        type_filter: ReflectionType | None = None,
    ) -> list[Reflection]:
        """Fetch active reflections created in the last N hours."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=hours)
        ).isoformat()
        with self._lock:
            if type_filter:
                rows = self._conn.execute(
                    "SELECT * FROM reflections WHERE active = 1 AND created_at >= ? AND type = ?",
                    (cutoff, type_filter.value),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM reflections WHERE active = 1 AND created_at >= ?",
                    (cutoff,),
                ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def get_all_active_by_type(
        self,
        reflection_type: ReflectionType,
    ) -> list[Reflection]:
        """Fetch all active reflections of a given type."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reflections WHERE active = 1 AND type = ?",
                (reflection_type.value,),
            ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def get_recent_active_by_type(
        self,
        reflection_type: ReflectionType,
        limit: int = 500,
    ) -> list[Reflection]:
        """Fetch the most recent active reflections of a given type (capped)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reflections WHERE active = 1 AND type = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (reflection_type.value, limit),
            ).fetchall()
            return [self._row_to_reflection(row) for row in rows]

    def set_context_json(self, reflection_id: str, context_data: dict) -> None:
        """Set the context field to a JSON blob."""
        with self._lock:
            self._conn.execute(
                "UPDATE reflections SET context = ? WHERE id = ?",
                (json.dumps(context_data), reflection_id),
            )
            self._conn.commit()

    # ── Daemon sync counts (persisted across restarts) ──

    def load_sync_counts(self) -> dict[str, int]:
        """Load persisted extraction daemon sync counts."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id, message_count FROM daemon_sync_counts"
            ).fetchall()
            return {row["session_id"]: row["message_count"] for row in rows}

    def save_sync_count(self, session_id: str, message_count: int) -> None:
        """Persist a single sync count (upsert)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO daemon_sync_counts (session_id, message_count, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET message_count = ?, updated_at = ?",
                (session_id, message_count, _now_iso(), message_count, _now_iso()),
            )
            self._conn.commit()

    def rebuild_fts(self) -> int:
        """Rebuild the FTS5 index from the reflections table.

        Returns the number of rows indexed, or -1 if FTS5 is not available.
        If the FTS5 table is corrupt, drops and recreates it.
        """
        with self._lock:
            if not self._fts5_available:
                return -1
            try:
                self._conn.execute("DELETE FROM reflections_fts")
            except Exception:
                # FTS5 table itself is corrupt — nuke and recreate
                self._conn.executescript(
                    "DROP TRIGGER IF EXISTS reflections_ai;"
                    "DROP TRIGGER IF EXISTS reflections_ad;"
                    "DROP TRIGGER IF EXISTS reflections_au;"
                    "DROP TABLE IF EXISTS reflections_fts;"
                )
                self._conn.commit()
                self._conn.executescript(_FTS5_SCHEMA)
                self._conn.executescript(_FTS5_TRIGGERS)
                self._conn.commit()
            self._conn.execute(
                "INSERT INTO reflections_fts(rowid, id, content, context, project) "
                "SELECT rowid, id, content, context, project FROM reflections"
            )
            self._conn.commit()
            count = self._conn.execute("SELECT COUNT(*) FROM reflections_fts").fetchone()[0]
            return count

    def count(self, active_only: bool = True) -> int:
        with self._lock:
            if active_only:
                return self._conn.execute(
                    "SELECT COUNT(*) FROM reflections WHERE active = 1"
                ).fetchone()[0]
            return self._conn.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]

    # ── Integrity checks ──

    def check_integrity(self) -> tuple[bool, str]:
        """Run PRAGMA integrity_check. Returns (ok, message)."""
        with self._lock:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
            result = row[0] if row else "unknown"
            return (result == "ok", result)

    def check_fts_integrity(self) -> tuple[bool, str]:
        """Run FTS5 integrity-check. Returns (ok, message)."""
        if not self._fts5_available:
            return (True, "fts5_not_available")
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO reflections_fts(reflections_fts) VALUES('integrity-check')"
                )
                return (True, "ok")
            except Exception as exc:
                return (False, str(exc))

    def reopen(self) -> None:
        """Close and reopen the connection (triggers WAL recovery)."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            # Reload sqlite-vec extension after reconnect
            self._vec_available = False
            self._init_vec()

    def backup_corrupt(self) -> str:
        """Back up all DB files (.db, -wal, -shm) with a timestamp suffix.

        Returns the backup path of the main .db file.
        """
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        db = Path(self._db_path)
        backup_path = db.with_suffix(f".db.corrupt.{ts}")
        for suffix in ("", "-wal", "-shm"):
            src = db.parent / (db.name + suffix) if suffix else db
            if src.exists():
                dst = db.parent / (backup_path.name + suffix)
                shutil.copy2(str(src), str(dst))
        return str(backup_path)
