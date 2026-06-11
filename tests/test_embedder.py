"""Tests for fastembed embedder helpers."""

from __future__ import annotations

import numpy as np

from cue.embedder import (
    _l2_normalize,
    _l2_normalize_batch,
    cosine_similarity,
    resolve_model_name,
    top_k_similar,
)


class TestResolveModelName:
    def test_legacy_minilm_alias(self):
        assert resolve_model_name("all-MiniLM-L6-v2") == "sentence-transformers/all-MiniLM-L6-v2"

    def test_passthrough(self):
        assert resolve_model_name("BAAI/bge-small-en-v1.5") == "BAAI/bge-small-en-v1.5"


class TestL2Normalize:
    def test_unit_length(self):
        vec = _l2_normalize(np.array([3.0, 4.0], dtype=np.float32))
        assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-6

    def test_batch(self):
        mat = _l2_normalize_batch(np.array([[3.0, 4.0], [1.0, 0.0]], dtype=np.float32))
        norms = np.linalg.norm(mat, axis=1)
        assert np.allclose(norms, [1.0, 1.0], atol=1e-6)


class TestTopKSimilar:
    def test_returns_best_index(self):
        query = np.array([1.0, 0.0], dtype=np.float32)
        matrix = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        hits = top_k_similar(query, matrix, k=1)
        assert hits == [(0, 1.0)]

    def test_cosine(self):
        a = _l2_normalize(np.array([1.0, 2.0]))
        b = _l2_normalize(np.array([2.0, 4.0]))
        assert cosine_similarity(a, b) > 0.99
