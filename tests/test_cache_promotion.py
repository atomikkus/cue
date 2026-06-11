"""Tests for provenance-based cache promotion."""

from __future__ import annotations

from pathlib import Path

from cue.cache_promotion import commands_match
from cue.resolver import _normalize
from cue.store import Store
from tests.test_resolver import (
    _make_context,
    _make_mock_embedder,
    _make_mock_provider,
    _make_resolver,
    _make_store,
    _make_vec,
)


class TestCommandsMatch:
    def test_exact_match(self):
        assert commands_match("ls -la", "ls -la")

    def test_strips_danger_prefix(self):
        assert commands_match("⚠ rm -rf ./build", "rm -rf ./build")

    def test_light_edit(self):
        assert commands_match("find . -name '*.svs'", "find . -name \"*.svs\"")

    def test_different_commands(self):
        assert not commands_match("ls -la", "gsutil ls gs://bucket/*.svs")


class TestPromotion:
    def test_tier3_queues_pending_not_exact(self, tmp_path: Path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("find . -name '*.py'")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("find python files", _make_context())

        norm = _normalize("find python files")
        assert store.exact_get(norm) is None
        assert store.pending_count() == 1
        assert len(store.semantic_get_all()) == 0

    def test_promote_after_execution(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=3)
        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("find . -name '*.py'")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("find python files", _make_context())
        assert store.pending_count() == 1

        promoted = resolver.promote_from_execution("find . -name '*.py'", exit_code=0)
        assert promoted is True
        assert store.pending_count() == 0

        norm = _normalize("find python files")
        assert store.exact_get(norm) == "find . -name '*.py'"
        assert len(store.semantic_get_all()) == 1

    def test_failed_execution_does_not_promote(self, tmp_path: Path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("false")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("run false", _make_context())
        promoted = resolver.promote_from_execution("false", exit_code=1)
        assert promoted is False
        assert store.exact_get(_normalize("run false")) is None
        assert len(store.semantic_get_all()) == 0

    def test_different_command_clears_recent_pending(self, tmp_path: Path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("gsutil ls gs://bucket/*.svs")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("list svs files", _make_context())
        assert store.pending_count() == 1

        promoted = resolver.promote_from_execution("ls", exit_code=0)
        assert promoted is False
        assert store.pending_count() == 0

    def test_proven_semantic_served_on_repeat(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=11)
        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.95)]
        provider = _make_mock_provider()
        resolver = _make_resolver(store, embedder, provider)

        store.semantic_put("find python files", vec, "find . -name '*.py'", provenance="proven")

        result = resolver.resolve("find python files", _make_context())
        assert result.tier == 1
        assert result.command == "find . -name '*.py'"
        provider.generate.assert_not_called()

    def test_unproven_semantic_not_served(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=11)
        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("find . -name '*.py'")
        resolver = _make_resolver(store, embedder, provider)

        store.semantic_put(
            "find python files", vec, "find . -name '*.py'", provenance="llm"
        )

        result = resolver.resolve("find python files", _make_context())
        assert result.tier == 3
        provider.generate.assert_called_once()
