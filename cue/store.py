"""SQLite-backed storage layer.

Tables:
  exact_cache     — verbatim query → command mapping (keyed by query + context)
  semantic_cache  — embedding + command pairs for similarity search
  history_index   — shell history with embeddings for Tier-2 search
  telemetry       — local opt-in usage stats (never transmitted)

Embeddings are stored as raw float32 bytes (BLOB) and loaded back via numpy.
Cosine similarity is computed in Python/numpy — no vector extension needed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import numpy as np

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS exact_cache (
    cache_key    TEXT PRIMARY KEY,
    query_norm   TEXT NOT NULL,
    command      TEXT NOT NULL,
    context_hash TEXT,
    provenance   TEXT NOT NULL DEFAULT 'proven',
    hits         INTEGER DEFAULT 1,
    created_at   INTEGER,
    last_used    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_exact_query ON exact_cache(query_norm);

CREATE TABLE IF NOT EXISTS semantic_cache (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    embedding     BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL DEFAULT 384,
    command       TEXT NOT NULL,
    context_hash  TEXT,
    provider      TEXT,
    model         TEXT,
    provenance    TEXT NOT NULL DEFAULT 'proven',
    hits          INTEGER DEFAULT 1,
    created_at    INTEGER,
    last_used     INTEGER
);

CREATE TABLE IF NOT EXISTS pending_cache (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    query_norm        TEXT NOT NULL,
    query             TEXT NOT NULL,
    embedding         BLOB NOT NULL,
    embedding_dim     INTEGER NOT NULL DEFAULT 384,
    suggested_command TEXT NOT NULL,
    context_hash      TEXT,
    provider          TEXT,
    model             TEXT,
    created_at        INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_cache(created_at DESC);

CREATE TABLE IF NOT EXISTS rejected_cache (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query_norm       TEXT NOT NULL,
    query            TEXT NOT NULL,
    embedding        BLOB NOT NULL,
    embedding_dim    INTEGER NOT NULL DEFAULT 384,
    rejected_command TEXT NOT NULL,
    context_hash     TEXT,
    created_at       INTEGER NOT NULL,
    last_seen        INTEGER NOT NULL,
    hits             INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rejected_created ON rejected_cache(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rejected_query ON rejected_cache(query_norm);

CREATE TABLE IF NOT EXISTS history_index (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command       TEXT NOT NULL UNIQUE,
    embedding     BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL DEFAULT 384,
    source        TEXT,
    freq          INTEGER DEFAULT 1,
    indexed_at    INTEGER
);

CREATE TABLE IF NOT EXISTS telemetry (
    ts          INTEGER,
    op          TEXT,
    tier        INTEGER,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_semantic_context ON semantic_cache(context_hash);
CREATE INDEX IF NOT EXISTS idx_history_freq ON history_index(freq DESC);
CREATE INDEX IF NOT EXISTS idx_history_indexed ON history_index(indexed_at ASC);
"""


def exact_cache_key(query_norm: str, context_hash: str | None) -> str:
    """Composite cache key — context_hash empty string when not context-sensitive."""
    return f"{query_norm}|{context_hash or ''}"


def blob_to_vec(blob: bytes | None, expected_dim: int | None = None) -> np.ndarray | None:
    """Convert a stored BLOB back into a numpy float32 array."""
    if blob is None:
        return None
    vec = np.frombuffer(blob, dtype=np.float32).copy()
    if expected_dim is not None and vec.shape[0] != expected_dim:
        return None
    return vec


@dataclass
class _MatrixCache:
    """In-memory embedding matrix + row metadata for fast similarity search."""

    rows: list[dict] = field(default_factory=list)
    matrix: np.ndarray | None = None  # shape (N, D)

    def invalidate(self) -> None:
        self.rows = []
        self.matrix = None


class Store:
    """Thread-safe SQLite store for all cue persistence."""

    def __init__(self, db_path: Path | str, *, history_max_entries: int = 10_000) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_max_entries = history_max_entries
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._semantic_caches: dict[object, _MatrixCache] = {}
        self._history_cache = _MatrixCache()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate_schema()
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """Migrate legacy schemas from earlier cue versions."""
        tables = {
            row[0]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # Legacy exact_cache used query_norm as sole PRIMARY KEY
        if "exact_cache" in tables:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(exact_cache)")}
            if "cache_key" not in cols:
                self._conn.executescript(
                    """
                    CREATE TABLE exact_cache_new (
                        cache_key    TEXT PRIMARY KEY,
                        query_norm   TEXT NOT NULL,
                        command      TEXT NOT NULL,
                        context_hash TEXT,
                        provenance   TEXT NOT NULL DEFAULT 'llm',
                        hits         INTEGER DEFAULT 1,
                        created_at   INTEGER,
                        last_used    INTEGER
                    );
                    INSERT INTO exact_cache_new(cache_key, query_norm, command, context_hash, provenance, hits, created_at, last_used)
                    SELECT query_norm || '|' || COALESCE(context_hash, ''), query_norm, command, context_hash, 'llm', hits, created_at, last_used
                    FROM exact_cache;
                    DROP TABLE exact_cache;
                    ALTER TABLE exact_cache_new RENAME TO exact_cache;
                    CREATE INDEX IF NOT EXISTS idx_exact_query ON exact_cache(query_norm);
                    """
                )
            else:
                if "provenance" not in cols:
                    self._conn.execute(
                        "ALTER TABLE exact_cache ADD COLUMN provenance TEXT NOT NULL DEFAULT 'llm'"
                    )
                    self._conn.execute(
                        "UPDATE exact_cache SET provenance = 'llm' WHERE provenance IS NULL OR provenance = ''"
                    )

        if "semantic_cache" in tables:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(semantic_cache)")}
            if "embedding_dim" not in cols:
                self._conn.execute(
                    "ALTER TABLE semantic_cache ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 384"
                )
                self._conn.execute(
                    "UPDATE semantic_cache SET embedding_dim = length(embedding) / 4 WHERE embedding_dim = 384"
                )
            if "provenance" not in cols:
                self._conn.execute(
                    "ALTER TABLE semantic_cache ADD COLUMN provenance TEXT NOT NULL DEFAULT 'llm'"
                )
                self._conn.execute(
                    "UPDATE semantic_cache SET provenance = 'llm' WHERE provenance IS NULL OR provenance = ''"
                )
                self._semantic_caches.clear()

        if "history_index" in tables:
            cols = {row[1] for row in self._conn.execute("PRAGMA table_info(history_index)")}
            if "embedding_dim" not in cols:
                self._conn.execute(
                    "ALTER TABLE history_index ADD COLUMN embedding_dim INTEGER NOT NULL DEFAULT 384"
                )
                self._conn.execute(
                    "UPDATE history_index SET embedding_dim = length(embedding) / 4 WHERE embedding_dim = 384"
                )

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _invalidate_semantic_cache(self, context_hash: str | None = None) -> None:
        del context_hash
        self._semantic_caches.clear()

    def _invalidate_history_cache(self) -> None:
        self._history_cache.invalidate()

    # ------------------------------------------------------------------
    # Exact cache
    # ------------------------------------------------------------------

    def exact_get(self, query_norm: str, context_hash: str | None = None) -> str | None:
        """Return a cached command for a normalized query, or None on miss."""
        key = exact_cache_key(query_norm, context_hash)
        with self._lock:
            row = self._conn.execute(
                "SELECT command FROM exact_cache WHERE cache_key=? AND provenance = 'proven'",
                (key,),
            ).fetchone()
            if row:
                now = int(time.time())
                self._conn.execute(
                    "UPDATE exact_cache SET hits=hits+1, last_used=? WHERE cache_key=?",
                    (now, key),
                )
                self._conn.commit()
                return row["command"]
        return None

    def exact_put(self, query_norm: str, command: str, context_hash: str | None = None) -> None:
        key = exact_cache_key(query_norm, context_hash)
        now = int(time.time())
        with self._tx():
            self._conn.execute(
                """INSERT INTO exact_cache
                   (cache_key, query_norm, command, context_hash, provenance, created_at, last_used)
                   VALUES (?, ?, ?, ?, 'proven', ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       command=excluded.command,
                       provenance='proven',
                       hits=hits+1,
                       last_used=excluded.last_used""",
                (key, query_norm, command, context_hash, now, now),
            )

    def exact_delete(
        self,
        query_norm: str,
        command: str | None = None,
        context_hash: str | None = None,
    ) -> int:
        key = exact_cache_key(query_norm, context_hash)
        with self._tx():
            if command is None:
                cur = self._conn.execute("DELETE FROM exact_cache WHERE cache_key=?", (key,))
            else:
                cur = self._conn.execute(
                    "DELETE FROM exact_cache WHERE cache_key=? AND command=?",
                    (key, command),
                )
            return cur.rowcount

    def exact_delete_command(self, command: str) -> int:
        with self._tx():
            cur = self._conn.execute("DELETE FROM exact_cache WHERE command=?", (command,))
            return cur.rowcount

    # ------------------------------------------------------------------
    # Semantic cache
    # ------------------------------------------------------------------

    def semantic_get_all(self, context_hash: str | None = None) -> list[dict]:
        """Load user-proven semantic cache rows (embedding, command, context_hash)."""
        cache_key = ("proven", context_hash if context_hash else None)
        cached = self._semantic_caches.get(cache_key)
        if cached is not None:
            return cached.rows

        with self._lock:
            if context_hash:
                rows = self._conn.execute(
                    """SELECT id, embedding, embedding_dim, command, context_hash
                       FROM semantic_cache
                       WHERE provenance = 'proven'
                         AND (context_hash IS NULL OR context_hash = ?)""",
                    (context_hash,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT id, embedding, embedding_dim, command, context_hash
                       FROM semantic_cache
                       WHERE provenance = 'proven'"""
                ).fetchall()
            result = [dict(r) for r in rows]

        mc = _MatrixCache(rows=result)
        self._semantic_caches[cache_key] = mc
        return result

    def semantic_get_matrix(
        self, query_dim: int, context_hash: str | None = None
    ) -> tuple[list[dict], np.ndarray]:
        """Return rows and stacked embedding matrix, filtering dim mismatches."""
        rows = self.semantic_get_all(context_hash)
        cache_key = ("proven", context_hash if context_hash else None)
        mc = self._semantic_caches.setdefault(cache_key, _MatrixCache())

        if mc.matrix is not None and len(mc.rows) == len(rows):
            return mc.rows, mc.matrix

        valid_rows: list[dict] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            dim = row.get("embedding_dim") or 0
            vec = blob_to_vec(row["embedding"], expected_dim=query_dim if dim else query_dim)
            if vec is None:
                if dim and dim != query_dim:
                    continue
                vec = blob_to_vec(row["embedding"])
                if vec is None or vec.shape[0] != query_dim:
                    continue
            valid_rows.append(row)
            vectors.append(vec)

        if not vectors:
            mc.rows = []
            mc.matrix = None
            return [], np.empty((0, query_dim), dtype=np.float32)

        matrix = np.stack(vectors, axis=0)
        mc.rows = valid_rows
        mc.matrix = matrix
        return valid_rows, matrix

    def semantic_put(
        self,
        query: str,
        embedding: np.ndarray,
        command: str,
        context_hash: str | None = None,
        provider: str = "",
        model: str = "",
        *,
        provenance: str = "proven",
    ) -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        dim = int(embedding.shape[0])
        with self._tx():
            self._conn.execute(
                """INSERT INTO semantic_cache
                   (query, embedding, embedding_dim, command, context_hash, provider, model,
                    provenance, created_at, last_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (query, blob, dim, command, context_hash, provider, model, provenance, now, now),
            )
        self._invalidate_semantic_cache(context_hash)

    def semantic_delete_command(self, command: str) -> int:
        with self._tx():
            cur = self._conn.execute("DELETE FROM semantic_cache WHERE command=?", (command,))
            count = cur.rowcount
        if count:
            self._invalidate_semantic_cache()
        return count

    # ------------------------------------------------------------------
    # Pending cache (LLM suggestions awaiting user execution)
    # ------------------------------------------------------------------

    def pending_put(
        self,
        query_norm: str,
        query: str,
        embedding: np.ndarray,
        suggested_command: str,
        context_hash: str | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        dim = int(embedding.shape[0])
        with self._tx():
            self._conn.execute(
                """INSERT INTO pending_cache
                   (query_norm, query, embedding, embedding_dim, suggested_command,
                    context_hash, provider, model, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    query_norm,
                    query,
                    blob,
                    dim,
                    suggested_command,
                    context_hash,
                    provider,
                    model,
                    now,
                ),
            )

    def pending_list_recent(self, max_age_seconds: int) -> list[dict]:
        cutoff = int(time.time()) - max_age_seconds
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, query_norm, query, embedding, embedding_dim, suggested_command,
                          context_hash, provider, model, created_at
                   FROM pending_cache
                   WHERE created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()
            return [dict(r) for r in rows]

    def pending_delete(self, row_id: int) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM pending_cache WHERE id=?", (row_id,))

    def pending_delete_for_query(self, query_norm: str) -> int:
        with self._tx():
            cur = self._conn.execute(
                "DELETE FROM pending_cache WHERE query_norm=?", (query_norm,)
            )
            return cur.rowcount

    def pending_prune_older_than(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        with self._tx():
            cur = self._conn.execute(
                "DELETE FROM pending_cache WHERE created_at < ?", (cutoff,)
            )
            return cur.rowcount

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM pending_cache").fetchone()
            return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Rejected cache (query, command pairs the user did not accept)
    # ------------------------------------------------------------------

    def rejection_put(
        self,
        query_norm: str,
        query: str,
        embedding: np.ndarray,
        rejected_command: str,
        context_hash: str | None = None,
    ) -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        dim = int(embedding.shape[0])
        with self._tx():
            self._conn.execute(
                """INSERT INTO rejected_cache
                   (query_norm, query, embedding, embedding_dim, rejected_command,
                    context_hash, created_at, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   """,
                (
                    query_norm,
                    query,
                    blob,
                    dim,
                    rejected_command,
                    context_hash,
                    now,
                    now,
                ),
            )

    def rejection_list_recent(
        self,
        max_age_seconds: int,
        context_hash: str | None = None,
    ) -> list[dict]:
        cutoff = int(time.time()) - max_age_seconds
        with self._lock:
            if context_hash:
                rows = self._conn.execute(
                    """SELECT id, query_norm, query, embedding, embedding_dim,
                              rejected_command, context_hash, created_at, last_seen, hits
                       FROM rejected_cache
                       WHERE created_at >= ?
                         AND (context_hash IS NULL OR context_hash = ?)
                       ORDER BY last_seen DESC""",
                    (cutoff, context_hash),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """SELECT id, query_norm, query, embedding, embedding_dim,
                              rejected_command, context_hash, created_at, last_seen, hits
                       FROM rejected_cache
                       WHERE created_at >= ?
                       ORDER BY last_seen DESC""",
                    (cutoff,),
                ).fetchall()
            return [dict(r) for r in rows]

    def rejection_has_exact(
        self,
        query_norm: str,
        rejected_command: str,
        context_hash: str | None = None,
        *,
        max_age_seconds: int,
    ) -> bool:
        cutoff = int(time.time()) - max_age_seconds
        with self._lock:
            if context_hash:
                row = self._conn.execute(
                    """SELECT id FROM rejected_cache
                       WHERE query_norm = ?
                         AND rejected_command = ?
                         AND created_at >= ?
                         AND (context_hash IS NULL OR context_hash = ?)
                       LIMIT 1""",
                    (query_norm, rejected_command, cutoff, context_hash),
                ).fetchone()
            else:
                row = self._conn.execute(
                    """SELECT id FROM rejected_cache
                       WHERE query_norm = ?
                         AND rejected_command = ?
                         AND created_at >= ?
                       LIMIT 1""",
                    (query_norm, rejected_command, cutoff),
                ).fetchone()
            return row is not None

    def rejection_prune_older_than(self, max_age_seconds: int) -> int:
        cutoff = int(time.time()) - max_age_seconds
        with self._tx():
            cur = self._conn.execute(
                "DELETE FROM rejected_cache WHERE created_at < ?", (cutoff,)
            )
            return cur.rowcount

    def rejection_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM rejected_cache").fetchone()
            return int(row["n"]) if row else 0

    def semantic_update_hit(self, row_id: int) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                "UPDATE semantic_cache SET hits=hits+1, last_used=? WHERE id=?",
                (now, row_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # History index
    # ------------------------------------------------------------------

    def history_get_all(self) -> list[dict]:
        """Load all history embeddings."""
        if self._history_cache.rows:
            return self._history_cache.rows

        with self._lock:
            rows = self._conn.execute(
                "SELECT id, command, embedding, embedding_dim, freq FROM history_index ORDER BY freq DESC"
            ).fetchall()
            result = [dict(r) for r in rows]

        self._history_cache.rows = result
        return result

    def history_get_matrix(self, query_dim: int) -> tuple[list[dict], np.ndarray]:
        """Return history rows and stacked embedding matrix, filtering dim mismatches."""
        rows = self.history_get_all()
        mc = self._history_cache

        if mc.matrix is not None and len(mc.rows) == len(rows):
            return mc.rows, mc.matrix

        valid_rows: list[dict] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            dim = row.get("embedding_dim") or 0
            vec = blob_to_vec(row["embedding"], expected_dim=query_dim if dim else query_dim)
            if vec is None:
                if dim and dim != query_dim:
                    continue
                vec = blob_to_vec(row["embedding"])
                if vec is None or vec.shape[0] != query_dim:
                    continue
            valid_rows.append(row)
            vectors.append(vec)

        if not vectors:
            mc.rows = []
            mc.matrix = None
            return [], np.empty((0, query_dim), dtype=np.float32)

        matrix = np.stack(vectors, axis=0)
        mc.rows = valid_rows
        mc.matrix = matrix
        return valid_rows, matrix

    def history_put(self, command: str, embedding: np.ndarray, source: str = "zsh_history") -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        dim = int(embedding.shape[0])
        with self._tx():
            self._conn.execute(
                """INSERT INTO history_index(command, embedding, embedding_dim, source, freq, indexed_at)
                   VALUES (?, ?, ?, ?, 1, ?)
                   ON CONFLICT(command) DO UPDATE SET
                       freq=freq+1,
                       indexed_at=excluded.indexed_at,
                       embedding=excluded.embedding,
                       embedding_dim=excluded.embedding_dim""",
                (command, blob, dim, source, now),
            )
            self._history_prune_locked()
        self._invalidate_history_cache()

    def history_put_batch(self, entries: list[tuple[str, np.ndarray, str]]) -> None:
        """Bulk insert (command, embedding, source) tuples."""
        now = int(time.time())
        with self._tx():
            for command, emb, source in entries:
                blob = emb.astype(np.float32).tobytes()
                dim = int(emb.shape[0])
                self._conn.execute(
                    """INSERT INTO history_index(command, embedding, embedding_dim, source, freq, indexed_at)
                       VALUES (?, ?, ?, ?, 1, ?)
                       ON CONFLICT(command) DO UPDATE SET
                           freq=freq+1,
                           indexed_at=excluded.indexed_at,
                           embedding=excluded.embedding,
                           embedding_dim=excluded.embedding_dim""",
                    (command, blob, dim, source, now),
                )
            self._history_prune_locked()
        self._invalidate_history_cache()

    def _history_prune_locked(self) -> None:
        """Drop lowest-freq / oldest history rows when over cap. Caller holds lock via _tx."""
        if self.history_max_entries <= 0:
            return
        count = self._conn.execute("SELECT COUNT(*) as n FROM history_index").fetchone()["n"]
        excess = count - self.history_max_entries
        if excess <= 0:
            return
        self._conn.execute(
            """DELETE FROM history_index WHERE id IN (
                SELECT id FROM history_index
                ORDER BY freq ASC, indexed_at ASC
                LIMIT ?
            )""",
            (excess,),
        )
        log.debug("Pruned %d history_index rows (cap=%d)", excess, self.history_max_entries)

    def history_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) as n FROM history_index").fetchone()
            return row["n"] if row else 0

    def history_known_commands(self) -> frozenset[str]:
        with self._lock:
            rows = self._conn.execute("SELECT command FROM history_index").fetchall()
            return frozenset(r["command"] for r in rows)

    def history_purge_non_commands(self) -> int:
        """Remove natural-language lines mistakenly indexed as shell history."""
        from cue.validator import is_likely_shell_command  # noqa: PLC0415

        with self._lock:
            rows = self._conn.execute("SELECT id, command FROM history_index").fetchall()
            bad_ids = [r["id"] for r in rows if not is_likely_shell_command(r["command"])]
            if not bad_ids:
                return 0
            placeholders = ",".join("?" * len(bad_ids))
            self._conn.execute(
                f"DELETE FROM history_index WHERE id IN ({placeholders})",
                bad_ids,
            )
            self._conn.commit()
        self._invalidate_history_cache()
        log.info("Purged %d non-command rows from history_index", len(bad_ids))
        return len(bad_ids)

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def telemetry_log(
        self,
        op: str,
        tier: int,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: int = 0,
    ) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO telemetry(ts, op, tier, tokens_in, tokens_out, latency_ms) VALUES (?,?,?,?,?,?)",
                (int(time.time()), op, tier, tokens_in, tokens_out, latency_ms),
            )

    def telemetry_stats(self) -> dict:
        """Aggregate stats for `cue stats` command."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT tier, COUNT(*) as n, SUM(tokens_in) as ti, SUM(tokens_out) as to_ "
                "FROM telemetry GROUP BY tier"
            ).fetchall()
        total = sum(r["n"] for r in rows)
        tier_counts = {r["tier"]: r["n"] for r in rows}
        tier_tokens_in = {r["tier"]: r["ti"] or 0 for r in rows}
        tier_tokens_out = {r["tier"]: r["to_"] or 0 for r in rows}
        local_hits = sum(v for k, v in tier_counts.items() if k < 3)
        return {
            "total": total,
            "tier_counts": tier_counts,
            "local_hit_rate": local_hits / total if total else 0.0,
            "total_tokens_in": sum(tier_tokens_in.values()),
            "total_tokens_out": sum(tier_tokens_out.values()),
            "history_entries": self.history_count(),
        }
