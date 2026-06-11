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
    _orthogonal_vec,
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
        query_vec = _make_vec(seed=19)
        unrelated_vec = _orthogonal_vec(query_vec)
        embedder = _make_mock_embedder(query_vec=query_vec)
        embedder.top_k_similar.return_value = []
        embedder.embed.side_effect = lambda text, model: (
            unrelated_vec if text == "ls" else query_vec
        )
        provider = _make_mock_provider("gsutil ls gs://bucket/*.svs")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("list svs files", _make_context())
        assert store.pending_count() == 1

        promoted = resolver.promote_from_execution("ls", exit_code=0)
        assert promoted is False
        assert store.pending_count() == 0
        assert store.rejection_count() == 1
        assert store.exact_get(_normalize("list svs files")) is None

    def test_heavy_edit_promotes_executed_command_when_aligned(self, tmp_path: Path):
        store = _make_store(tmp_path)
        query_vec = _make_vec(seed=23)
        embedder = _make_mock_embedder(query_vec=query_vec)
        embedder.top_k_similar.return_value = []
        embedder.embed.side_effect = lambda text, model: query_vec
        provider = _make_mock_provider("gsutil ls gs://bucket/*.svs")
        resolver = _make_resolver(store, embedder, provider)

        resolver.resolve("list svs files in this folder", _make_context())
        promoted = resolver.promote_from_execution("find . -name '*.svs' -type f", exit_code=0)

        assert promoted is True
        assert store.pending_count() == 0
        assert store.rejection_count() == 1
        assert (
            store.exact_get(_normalize("list svs files in this folder"))
            == "find . -name '*.svs' -type f"
        )

    def test_semantic_cache_hit_can_be_corrected(self, tmp_path: Path):
        """Reproduces: a wrong command served from semantic cache gets corrected."""
        store = _make_store(tmp_path)
        query_vec = _make_vec(seed=51)
        bad = "gsutil ls gs://wsi_bucket53/**/*.json"
        good = "gsutil ls gs://wsi_bucket53/"
        store.semantic_put(
            "list all files in gcs wsi_bucket53", query_vec, bad, provenance="proven"
        )

        embedder = _make_mock_embedder(query_vec=query_vec)
        embedder.embed.side_effect = lambda text, model: query_vec
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.96)]
        provider = _make_mock_provider(good)
        resolver = _make_resolver(store, embedder, provider)

        # 1) Wrong command served from semantic cache, pending queued.
        first = resolver.resolve("list all files in gcs wsi_bucket53", _make_context())
        assert first.tier == 1
        assert first.command == bad
        assert store.pending_count() == 1

        # 2) User runs the corrected command successfully.
        promoted = resolver.promote_from_execution(good, exit_code=0)
        assert promoted is True

        # 3) Bad entry is gone; correction is served instead.
        second = resolver.resolve("list all files in gcs wsi_bucket53", _make_context())
        assert second.command != bad
        assert second.command == good

    def test_ctrl_k_session_promotes_corrected_command(self, tmp_path: Path):
        """Explicit Ctrl+K session link — the user's png correction scenario."""
        store = _make_store(tmp_path)
        query_vec = _make_vec(seed=61)
        embedder = _make_mock_embedder(query_vec=query_vec)
        embedder.embed.side_effect = lambda text, model: query_vec
        resolver = _make_resolver(store, embedder, _make_mock_provider())

        query = "list all .png files in gcs wsi_bucket53"
        wrong = "gsutil ls gs://wsi_bucket53/**/*.png"
        good = "gsutil ls gs://wsi_bucket53/ | grep '\\.png$'"

        promoted = resolver.promote_from_execution(
            good,
            exit_code=0,
            session_query=query,
            session_suggestion=wrong,
        )
        assert promoted is True
        assert store.exact_get(_normalize(query)) == good
        assert store.rejection_count() == 1

        result = resolver.resolve(query, _make_context())
        assert result.tier == 0
        assert result.command == good

    def test_session_rejects_unrelated_command(self, tmp_path: Path):
        store = _make_store(tmp_path)
        query_vec = _make_vec(seed=71)
        unrelated_vec = _orthogonal_vec(query_vec)
        embedder = _make_mock_embedder(query_vec=query_vec)
        embedder.embed.side_effect = lambda text, model: (
            unrelated_vec if text == "cd GitHub" else query_vec
        )
        resolver = _make_resolver(store, embedder, _make_mock_provider())

        query = "number of json files in gcs wsi_bucket53"
        wrong = "gsutil ls gs://wsi_bucket53/**/*.json"
        promoted = resolver.promote_from_execution(
            "cd GitHub",
            exit_code=0,
            session_query=query,
            session_suggestion=wrong,
        )
        assert promoted is False
        assert store.exact_get(_normalize(query)) is None
        assert store.rejection_count() == 1

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

    def test_rejected_pair_blocks_exact_cache(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=31)
        query = "list svs files"
        bad = "gsutil ls gs://bucket/*.svs"
        store.exact_put(_normalize(query), bad)
        store.rejection_put(_normalize(query), query, vec, bad)

        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.return_value = []
        provider = _make_mock_provider("find . -name '*.svs' -type f")
        resolver = _make_resolver(store, embedder, provider)

        result = resolver.resolve(query, _make_context())
        assert result.tier == 3
        assert result.command == "find . -name '*.svs' -type f"
        assert store.exact_get(_normalize(query)) is None

    def test_rejected_pair_blocks_semantic_cache(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=37)
        query = "list svs files"
        bad = "gsutil ls gs://bucket/*.svs"
        store.semantic_put(query, vec, bad, provenance="proven")
        store.rejection_put(_normalize(query), query, vec, bad)

        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.96)]
        provider = _make_mock_provider("find . -name '*.svs' -type f")
        resolver = _make_resolver(store, embedder, provider)

        result = resolver.resolve("show svs files", _make_context())
        assert result.tier == 3
        assert result.command == "find . -name '*.svs' -type f"

    def test_failed_command_demotes_existing_cache(self, tmp_path: Path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=41)
        command = "false"
        store.exact_put(_normalize("run failing command"), command)
        store.semantic_put("run failing command", vec, command, provenance="proven")

        embedder = _make_mock_embedder(query_vec=vec)
        provider = _make_mock_provider()
        resolver = _make_resolver(store, embedder, provider)

        promoted = resolver.promote_from_execution(command, exit_code=1)

        assert promoted is False
        assert store.exact_get(_normalize("run failing command")) is None
        assert len(store.semantic_get_all()) == 0
