"""Tests for history source auto-detection and ingestion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cue.history as history


class TestAutoHistoryOrder:
    def test_prefers_zsh_when_shell_is_zsh(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert history._auto_history_order() == ["zsh", "bash"]

    def test_prefers_bash_when_shell_is_bash(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/bash")
        assert history._auto_history_order() == ["bash", "zsh"]


class TestDefaultIndexSource:
    def test_bash_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert history.default_index_source() == "bash_history"

    def test_zsh_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert history.default_index_source() == "zsh_history"


class TestIngestHistoryAuto:
    def test_auto_ingests_both_sources(self, tmp_path: Path, monkeypatch):
        zsh_hist = tmp_path / ".zsh_history"
        bash_hist = tmp_path / ".bash_history"
        zsh_hist.write_text(": 1:0;git status\n", encoding="utf-8")
        bash_hist.write_text("docker ps\n", encoding="utf-8")
        monkeypatch.setattr(history, "_ZSH_HISTORY_PATH", zsh_hist)
        monkeypatch.setattr(history, "_BASH_HISTORY_PATH", bash_hist)

        store = MagicMock()
        store.history_known_commands.return_value = set()
        embed_fn = MagicMock(side_effect=lambda batch, _model: [[0.1, 0.2] for _ in batch])

        count = history.ingest_history(store, embed_fn, source="auto")
        assert count == 2
        assert store.history_put_batch.call_count == 2

    def test_explicit_zsh_only(self, tmp_path: Path, monkeypatch):
        zsh_hist = tmp_path / ".zsh_history"
        bash_hist = tmp_path / ".bash_history"
        zsh_hist.write_text(": 1:0;git status\n", encoding="utf-8")
        bash_hist.write_text("docker ps\n", encoding="utf-8")
        monkeypatch.setattr(history, "_ZSH_HISTORY_PATH", zsh_hist)
        monkeypatch.setattr(history, "_BASH_HISTORY_PATH", bash_hist)

        store = MagicMock()
        store.history_known_commands.return_value = set()
        embed_fn = MagicMock(return_value=[[0.1, 0.2]])

        count = history.ingest_history(store, embed_fn, source="zsh")
        assert count == 1
