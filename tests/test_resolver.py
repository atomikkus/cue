"""Tests for the resolver tier engine — all dependencies mocked, no LLM calls."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from ctrlk.config import Config
from ctrlk.context import ShellContext
from ctrlk.providers.base import GenResult
from ctrlk.resolver import Resolver, _normalize
from ctrlk.store import Store


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

def _make_vec(dim: int = 384, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return (v / np.linalg.norm(v)).astype(np.float32)


def _make_store(tmp_path: Path) -> Store:
    return Store(tmp_path / "test_cache.db")


def _make_mock_embedder(query_vec: np.ndarray | None = None):
    """Return a mock embedder module that returns controllable vectors."""
    emb = MagicMock()
    fixed_vec = query_vec if query_vec is not None else _make_vec()
    emb.embed.return_value = fixed_vec
    emb.embed_batch.return_value = np.stack([fixed_vec])
    emb.top_k_similar.side_effect = lambda q, m, k=1: [(0, float(np.dot(q, m[0])))] if len(m) > 0 else []
    return emb


def _make_mock_provider(text: str = "ls -la", error: str | None = None) -> MagicMock:
    provider = MagicMock()
    provider.name = "mock"
    provider.supports_prompt_caching = False
    provider.generate.return_value = GenResult(
        text=text,
        tokens_in=50,
        tokens_out=5,
        cached_tokens=0,
        model="mock-model",
        provider="mock",
        error=error,
    )
    return provider


def _make_context(**kwargs) -> ShellContext:
    ctx = ShellContext()
    ctx.cwd = kwargs.get("cwd", "/home/user/project")
    ctx.git_branch = kwargs.get("git_branch", "main")
    ctx.git_remote = kwargs.get("git_remote", "https://github.com/user/project")
    ctx.project_root = kwargs.get("project_root", "/home/user/project")
    ctx.os_name = "linux"
    ctx.shell = "zsh"
    return ctx


def _make_resolver(store: Store, embedder, primary_provider, escalate_provider=None) -> Resolver:
    if escalate_provider is None:
        escalate_provider = _make_mock_provider("echo 'escalated'")

    return Resolver(
        store=store,
        embedder=embedder,
        providers={
            "primary": primary_provider,
            "escalate": escalate_provider,
        },
        primary_provider_name="primary",
        primary_model="mock-small",
        primary_max_tokens=100,
        escalate_provider_name="escalate",
        escalate_model="mock-large",
        escalate_max_tokens=200,
        similarity_threshold=0.92,
        history_threshold=0.88,
        embedding_model="all-MiniLM-L6-v2",
        danger_scan=True,
        redact=True,
        telemetry_enabled=False,
    )


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_lowercase(self):
        assert _normalize("List Files") == "list files"

    def test_collapse_whitespace(self):
        assert _normalize("  list   files  ") == "list files"

    def test_strip(self):
        assert _normalize("  hello  ") == "hello"


# ---------------------------------------------------------------------------
# Tier 0 — exact match
# ---------------------------------------------------------------------------

class TestTier0:
    def test_hit_returns_tier_0(self, tmp_path):
        store = _make_store(tmp_path)
        store.exact_put("list all files", "ls -la")
        embedder = _make_mock_embedder()
        provider = _make_mock_provider()
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("List All Files", ctx)

        assert result.tier == 0
        assert result.command == "ls -la"
        assert result.confidence == 1.0
        # Provider should NOT have been called
        provider.generate.assert_not_called()

    def test_miss_falls_through(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        provider = _make_mock_provider("ls -la")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("list all the files here", ctx)

        # Should reach at least Tier 3 since no cache
        assert result.tier >= 1


# ---------------------------------------------------------------------------
# Tier 1 — semantic cache
# ---------------------------------------------------------------------------

class TestTier1:
    def test_high_similarity_hit(self, tmp_path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=42)
        store.semantic_put("list files", vec, "ls -la")

        # Embedder returns the same vector (cosine sim = 1.0)
        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.95)]  # above threshold

        provider = _make_mock_provider()
        resolver = _make_resolver(store, embedder, provider)
        resolver.similarity_threshold = 0.92

        ctx = _make_context()
        result = resolver.resolve("show all files", ctx)

        assert result.tier == 1
        assert result.command == "ls -la"
        provider.generate.assert_not_called()

    def test_low_similarity_falls_through(self, tmp_path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=42)
        store.semantic_put("list files", vec, "ls -la")

        embedder = _make_mock_embedder(query_vec=_make_vec(seed=99))
        # Return low similarity score
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.50)]

        provider = _make_mock_provider("ls -la")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("find all docker containers", ctx)

        # Should fall through to Tier 3 since similarity is too low
        assert result.tier == 3


# ---------------------------------------------------------------------------
# Tier 2 — history search
# ---------------------------------------------------------------------------

class TestTier2:
    def test_history_high_similarity_hit(self, tmp_path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=1)
        store.history_put("docker ps -a", vec, "zsh_history")

        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.91)]  # above history_threshold

        provider = _make_mock_provider()
        resolver = _make_resolver(store, embedder, provider)
        resolver.history_threshold = 0.88

        ctx = _make_context()
        result = resolver.resolve("list running docker containers", ctx)

        assert result.tier == 2
        assert result.command == "docker ps -a"
        provider.generate.assert_not_called()

    def test_history_below_threshold_passes_hint_to_tier3(self, tmp_path):
        store = _make_store(tmp_path)
        vec = _make_vec(seed=1)
        store.history_put("docker ps -a", vec, "zsh_history")

        embedder = _make_mock_embedder(query_vec=vec)
        embedder.top_k_similar.side_effect = lambda q, m, k=1: [(0, 0.70)]  # below threshold

        provider = _make_mock_provider("docker ps -a")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("show containers", ctx)

        # Tier 3 should be called
        assert result.tier == 3
        assert provider.generate.called


# ---------------------------------------------------------------------------
# Tier 3 — LLM generation
# ---------------------------------------------------------------------------

class TestTier3:
    def test_tier3_uses_primary_provider(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []  # force Tier 3

        provider = _make_mock_provider("git log --oneline -10")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("show last 10 git commits", ctx)

        assert result.tier == 3
        assert "git log" in result.command
        assert result.tokens_in == 50
        assert result.tokens_out == 5
        provider.generate.assert_called_once()

    def test_tier3_escalates_on_invalid_parse(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []

        # Primary returns unparseable command
        primary = _make_mock_provider("echo 'broken quote")
        # Patch validate so primary's output is marked invalid
        escalate = _make_mock_provider("echo 'fixed'")

        resolver = _make_resolver(store, embedder, primary, escalate)

        # Patch validate to make primary's output invalid
        import ctrlk.resolver as resolver_mod
        original_validate = resolver_mod.validate

        call_count = [0]
        def _patched_validate(cmd, *, danger_scan=True):
            call_count[0] += 1
            if "broken quote" in cmd:
                from ctrlk.validator import ValidationResult
                return ValidationResult(
                    command=cmd, safe_command=cmd,
                    is_valid=False, is_dangerous=False,
                    danger_reason="", binary_found=False,
                    parse_error="No closing quotation",
                )
            return original_validate(cmd, danger_scan=danger_scan)

        with patch("ctrlk.resolver.validate", side_effect=_patched_validate):
            ctx = _make_context()
            result = resolver.resolve("some query", ctx)

        # Should have escalated
        escalate.generate.assert_called_once()
        assert result.command == "echo 'fixed'"

    def test_tier3_writes_to_cache(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []

        provider = _make_mock_provider("find . -name '*.py'")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        resolver.resolve("find python files", ctx)

        # Should be in exact cache now
        norm = _normalize("find python files")
        cached = store.exact_get(norm)
        assert cached == "find . -name '*.py'"

    def test_tier3_provider_error_returns_error_result(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []

        primary = _make_mock_provider(error="API timeout")
        escalate = _make_mock_provider(error="Escalation also failed")
        resolver = _make_resolver(store, embedder, primary, escalate)

        ctx = _make_context()
        result = resolver.resolve("some query", ctx)

        assert result.error is not None


# ---------------------------------------------------------------------------
# Context-sensitivity guard
# ---------------------------------------------------------------------------

class TestContextSensitivity:
    def test_deictic_query_detected(self):
        from ctrlk.context import is_context_sensitive
        # "service" is a concrete noun so that one is NOT context-sensitive — correct behavior
        # Queries with deictic words but NO concrete nouns are flagged
        assert is_context_sensitive("delete the last migration")
        assert is_context_sensitive("restart this thing")
        assert is_context_sensitive("run that again")

    def test_concrete_noun_not_sensitive(self):
        from ctrlk.context import is_context_sensitive
        # Has deictic but also concrete noun — not context-sensitive
        assert not is_context_sensitive("list all files")
        assert not is_context_sensitive("show git branch")

    def test_non_deictic_not_sensitive(self):
        from ctrlk.context import is_context_sensitive
        assert not is_context_sensitive("find all python files recursively")
        assert not is_context_sensitive("compress a directory to tar gz")

    def test_context_bucket_hash_consistent(self):
        ctx = _make_context()
        h1 = ctx.context_bucket_hash()
        h2 = ctx.context_bucket_hash()
        assert h1 == h2
        assert len(h1) == 16  # 16 hex chars

    def test_different_projects_different_buckets(self):
        ctx1 = _make_context(project_root="/home/user/project-a")
        ctx2 = _make_context(project_root="/home/user/project-b")
        assert ctx1.context_bucket_hash() != ctx2.context_bucket_hash()


# ---------------------------------------------------------------------------
# Danger scan integration in resolver
# ---------------------------------------------------------------------------

class TestDangerInResolver:
    def test_dangerous_command_gets_warning_prefix(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []

        provider = _make_mock_provider("rm -rf /")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("wipe the filesystem", ctx)

        assert result.command.startswith("⚠")
        assert "rm -rf /" in result.command

    def test_safe_command_no_prefix(self, tmp_path):
        store = _make_store(tmp_path)
        embedder = _make_mock_embedder()
        embedder.top_k_similar.return_value = []

        provider = _make_mock_provider("ls -la")
        resolver = _make_resolver(store, embedder, provider)

        ctx = _make_context()
        result = resolver.resolve("list files", ctx)

        assert not result.command.startswith("⚠")
