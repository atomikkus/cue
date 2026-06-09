"""Local embedding model — loaded once in the daemon.

Uses sentence-transformers with a small CPU-friendly model (~22 MB).
Embeddings are L2-normalized at write time so similarity reduces to a dot product.

The model is loaded lazily on first call and cached as a module-level singleton
so the daemon pays the import cost once only.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODEL_INSTANCE: "SentenceTransformer | None" = None
_LOADED_MODEL_NAME: str = ""


def _suppress_model_progress() -> None:
    """Keep embedding model load quiet when the daemon detaches from the terminal."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TQDM_DISABLE", "1")


def _get_model(model_name: str) -> "SentenceTransformer":
    global _MODEL_INSTANCE, _LOADED_MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL_INSTANCE is None or _LOADED_MODEL_NAME != model_name:
            log.info("Loading embedding model: %s", model_name)
            _suppress_model_progress()
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
            _MODEL_INSTANCE = SentenceTransformer(model_name)
            _LOADED_MODEL_NAME = model_name
            log.info("Embedding model loaded.")
        return _MODEL_INSTANCE


def embed(text: str, model_name: str = "all-MiniLM-L6-v2") -> np.ndarray:
    """Embed a single string, returning an L2-normalized float32 vector."""
    model = _get_model(model_name)
    vec = model.encode(text, normalize_embeddings=True, convert_to_numpy=True)
    return vec.astype(np.float32)


def embed_batch(texts: list[str], model_name: str = "all-MiniLM-L6-v2") -> np.ndarray:
    """Embed a list of strings; returns shape (N, D) float32, L2-normalized."""
    if not texts:
        return np.empty((0,), dtype=np.float32)
    model = _get_model(model_name)
    vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=64)
    return vecs.astype(np.float32)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Dot product of two L2-normalized vectors == cosine similarity."""
    return float(np.dot(a.flatten(), b.flatten()))


def top_k_similar(
    query_vec: np.ndarray,
    matrix: np.ndarray,
    k: int = 1,
) -> list[tuple[int, float]]:
    """Return (index, score) pairs for the k most similar rows in matrix.

    matrix shape: (N, D). query_vec shape: (D,).
    Both must be L2-normalized.
    """
    if matrix.ndim == 1 or matrix.shape[0] == 0:
        return []
    scores = matrix @ query_vec.flatten()
    # Partial sort: only need top-k
    if k >= len(scores):
        indices = np.argsort(scores)[::-1]
    else:
        indices = np.argpartition(scores, -k)[-k:]
        indices = indices[np.argsort(scores[indices])[::-1]]
    return [(int(i), float(scores[i])) for i in indices]


def preload(model_name: str = "all-MiniLM-L6-v2") -> None:
    """Eagerly load the model so the first query is fast. Call at daemon startup."""
    _get_model(model_name)
