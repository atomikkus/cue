"""SQLite-backed storage layer.

Tables:
  exact_cache     — verbatim query → command mapping
  semantic_cache  — embedding + command pairs for similarity search
  history_index   — shell history with embeddings for Tier-2 search
  telemetry       — local opt-in usage stats (never transmitted)

Embeddings are stored as raw float32 bytes (BLOB) and loaded back via numpy.
Cosine similarity is computed in Python/numpy — no vector extension needed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import numpy as np

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS exact_cache (
    query_norm   TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    context_hash TEXT,
    hits         INTEGER DEFAULT 1,
    created_at   INTEGER,
    last_used    INTEGER
);

CREATE TABLE IF NOT EXISTS semantic_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    query        TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    command      TEXT NOT NULL,
    context_hash TEXT,
    provider     TEXT,
    model        TEXT,
    hits         INTEGER DEFAULT 1,
    created_at   INTEGER,
    last_used    INTEGER
);

CREATE TABLE IF NOT EXISTS history_index (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    command    TEXT NOT NULL UNIQUE,
    embedding  BLOB NOT NULL,
    source     TEXT,
    freq       INTEGER DEFAULT 1,
    indexed_at INTEGER
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
"""


class Store:
    """Thread-safe SQLite store for all ctrlk persistence."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Exact cache
    # ------------------------------------------------------------------

    def exact_get(self, query_norm: str, context_hash: str | None = None) -> str | None:
        """Return a cached command for a normalized query, or None on miss."""
        if context_hash:
            row = self._conn.execute(
                "SELECT command FROM exact_cache WHERE query_norm=? AND (context_hash IS NULL OR context_hash=?)",
                (query_norm, context_hash),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT command FROM exact_cache WHERE query_norm=?",
                (query_norm,),
            ).fetchone()
        if row:
            now = int(time.time())
            self._conn.execute(
                "UPDATE exact_cache SET hits=hits+1, last_used=? WHERE query_norm=?",
                (now, query_norm),
            )
            self._conn.commit()
            return row["command"]
        return None

    def exact_put(self, query_norm: str, command: str, context_hash: str | None = None) -> None:
        now = int(time.time())
        with self._tx():
            self._conn.execute(
                """INSERT INTO exact_cache(query_norm, command, context_hash, created_at, last_used)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(query_norm) DO UPDATE SET
                       command=excluded.command,
                       hits=hits+1,
                       last_used=excluded.last_used""",
                (query_norm, command, context_hash, now, now),
            )

    # ------------------------------------------------------------------
    # Semantic cache
    # ------------------------------------------------------------------

    def semantic_get_all(self, context_hash: str | None = None) -> list[dict]:
        """Load all semantic cache rows (embedding, command, context_hash)."""
        if context_hash:
            rows = self._conn.execute(
                "SELECT id, embedding, command, context_hash FROM semantic_cache WHERE context_hash IS NULL OR context_hash=?",
                (context_hash,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, embedding, command, context_hash FROM semantic_cache"
            ).fetchall()
        return [dict(r) for r in rows]

    def semantic_put(
        self,
        query: str,
        embedding: np.ndarray,
        command: str,
        context_hash: str | None = None,
        provider: str = "",
        model: str = "",
    ) -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        with self._tx():
            self._conn.execute(
                """INSERT INTO semantic_cache
                   (query, embedding, command, context_hash, provider, model, created_at, last_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (query, blob, command, context_hash, provider, model, now, now),
            )

    def semantic_update_hit(self, row_id: int) -> None:
        now = int(time.time())
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
        rows = self._conn.execute(
            "SELECT id, command, embedding, freq FROM history_index ORDER BY freq DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def history_put(self, command: str, embedding: np.ndarray, source: str = "zsh_history") -> None:
        now = int(time.time())
        blob = embedding.astype(np.float32).tobytes()
        with self._tx():
            self._conn.execute(
                """INSERT INTO history_index(command, embedding, source, freq, indexed_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(command) DO UPDATE SET
                       freq=freq+1,
                       indexed_at=excluded.indexed_at""",
                (command, blob, source, now),
            )

    def history_put_batch(self, entries: list[tuple[str, np.ndarray, str]]) -> None:
        """Bulk insert (command, embedding, source) tuples."""
        now = int(time.time())
        with self._tx():
            for command, emb, source in entries:
                blob = emb.astype(np.float32).tobytes()
                self._conn.execute(
                    """INSERT INTO history_index(command, embedding, source, freq, indexed_at)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(command) DO UPDATE SET
                           freq=freq+1,
                           indexed_at=excluded.indexed_at""",
                    (command, blob, source, now),
                )

    def history_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as n FROM history_index").fetchone()
        return row["n"] if row else 0

    def history_known_commands(self) -> frozenset[str]:
        rows = self._conn.execute("SELECT command FROM history_index").fetchall()
        return frozenset(r["command"] for r in rows)

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
        """Aggregate stats for `ctrlk stats` command."""
        rows = self._conn.execute("SELECT tier, COUNT(*) as n, SUM(tokens_in) as ti, SUM(tokens_out) as to_ FROM telemetry GROUP BY tier").fetchall()
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


def blob_to_vec(blob: bytes | None) -> np.ndarray | None:
    """Convert a stored BLOB back into a numpy float32 array."""
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32).copy()
