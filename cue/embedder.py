"""Local embedding model — loaded once in the daemon.

Uses fastembed (ONNX Runtime) with a small CPU-friendly model (~70 MB on disk).
Embeddings are L2-normalized at write time so similarity reduces to a dot product.

The model is loaded lazily on first call and cached as a module-level singleton.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from fastembed import TextEmbedding

log = logging.getLogger(__name__)

_MODEL_LOCK = threading.Lock()
_MODEL_INSTANCE: "TextEmbedding | None" = None
_LOADED_MODEL_NAME: str = ""

# Legacy config values → fastembed model ids
_MODEL_ALIASES: dict[str, str] = {
    "all-MiniLM-L6-v2": "sentence-transformers/all-MiniLM-L6-v2",
}


def _suppress_model_progress() -> None:
    """Keep embedding model load quiet when the daemon detaches from the terminal."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    os.environ.setdefault("TQDM_DISABLE", "1")


def resolve_model_name(model_name: str) -> str:
    """Map config model names to fastembed-supported identifiers."""
    return _MODEL_ALIASES.get(model_name, model_name)


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    flat = vec.astype(np.float32).flatten()
    norm = float(np.linalg.norm(flat))
    if norm < 1e-12:
        return flat
    return (flat / norm).astype(np.float32)


def _l2_normalize_batch(matrix: np.ndarray) -> np.ndarray:
    if matrix.size == 0:
        return matrix.astype(np.float32)
    arr = matrix.astype(np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.clip(norms, 1e-12, None)


def _get_model(model_name: str) -> "TextEmbedding":
    global _MODEL_INSTANCE, _LOADED_MODEL_NAME
    resolved = resolve_model_name(model_name)
    with _MODEL_LOCK:
        if _MODEL_INSTANCE is None or _LOADED_MODEL_NAME != resolved:
            log.info("Loading embedding model: %s", resolved)
            _suppress_model_progress()
            from fastembed import TextEmbedding  # noqa: PLC0415

            _MODEL_INSTANCE = TextEmbedding(model_name=resolved)
            _LOADED_MODEL_NAME = resolved
            log.info("Embedding model loaded.")
        return _MODEL_INSTANCE


def embed(text: str, model_name: str = "BAAI/bge-small-en-v1.5") -> np.ndarray:
    """Embed a single string, returning an L2-normalized float32 vector."""
    model = _get_model(model_name)
    vec = next(model.embed([text]))
    return _l2_normalize(np.asarray(vec))


def embed_batch(texts: list[str], model_name: str = "BAAI/bge-small-en-v1.5") -> np.ndarray:
    """Embed a list of strings; returns shape (N, D) float32, L2-normalized."""
    if not texts:
        return np.empty((0,), dtype=np.float32)
    model = _get_model(model_name)
    vecs = [_l2_normalize(np.asarray(v)) for v in model.embed(texts)]
    return np.stack(vecs)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Dot product of two L2-normalized vectors == cosine similarity."""
    return float(np.dot(a.flatten(), b.flatten()))


def top_k_similar(
    query_vec: np.ndarray,
    matrix: np.ndarray,
    k: int = 1,
) -> list[tuple[int, float]]:
    """Return (index, score) pairs for the k most similar rows in matrix."""
    if matrix.ndim == 1 or matrix.shape[0] == 0:
        return []
    scores = matrix @ query_vec.flatten()
    if k >= len(scores):
        indices = np.argsort(scores)[::-1]
    else:
        indices = np.argpartition(scores, -k)[-k:]
        indices = indices[np.argsort(scores[indices])[::-1]]
    return [(int(i), float(scores[i])) for i in indices]


def preload(model_name: str = "BAAI/bge-small-en-v1.5") -> None:
    """Eagerly load the model so the first query is fast. Call at daemon startup."""
    _get_model(model_name)
