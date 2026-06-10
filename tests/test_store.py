"""Tests for the SQLite store — thread safety, exact cache keys, dim guards."""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest

from cue.store import Store, exact_cache_key


def _vec(dim: int = 384, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


class TestExactCacheKey:
    def test_context_hash_in_key(self, tmp_path: Path):
        store = Store(tmp_path / "cache.db")
        store.exact_put("restart service", "systemctl restart app", "ctx_a")
        store.exact_put("restart service", "docker compose restart", "ctx_b")

        assert store.exact_get("restart service", "ctx_a") == "systemctl restart app"
        assert store.exact_get("restart service", "ctx_b") == "docker compose restart"

    def test_exact_cache_key_helper(self):
        assert exact_cache_key("foo", None) == "foo|"
        assert exact_cache_key("foo", "abc") == "foo|abc"


class TestEmbeddingDimGuard:
    def test_semantic_matrix_skips_wrong_dim(self, tmp_path: Path):
        store = Store(tmp_path / "cache.db")
        store.semantic_put("q1", _vec(384, 1), "cmd384")
        store.semantic_put("q2", _vec(128, 2), "cmd128")

        rows, mat = store.semantic_get_matrix(384)
        assert len(rows) == 1
        assert mat.shape == (1, 384)
        assert rows[0]["command"] == "cmd384"

    def test_history_matrix_skips_wrong_dim(self, tmp_path: Path):
        store = Store(tmp_path / "cache.db")
        store.history_put("cmd384", _vec(384, 1))
        store.history_put("cmd128", _vec(128, 2))

        rows, mat = store.history_get_matrix(384)
        assert len(rows) == 1
        assert mat.shape == (1, 384)


class TestHistoryPrune:
    def test_prunes_when_over_cap(self, tmp_path: Path):
        store = Store(tmp_path / "cache.db", history_max_entries=3)
        for i in range(5):
            store.history_put(f"command-{i}", _vec(seed=i))

        assert store.history_count() == 3


class TestThreadSafety:
    def test_concurrent_writes_and_reads(self, tmp_path: Path):
        store = Store(tmp_path / "cache.db")
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                for j in range(20):
                    store.exact_put(f"query-{i}-{j}", f"cmd-{i}-{j}")
                    store.exact_get(f"query-{i}-{j}")
                    store.history_put(f"hist-{i}-{j}", _vec(seed=i + j))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert store.history_count() > 0
