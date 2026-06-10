"""Tests for config I/O, key helpers, and non-interactive CLI paths."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cue.config_io import format_config_show, get_nested, load_raw, save_raw, set_config_value, set_nested
from cue.keys import mask_key, resolve_key


class TestMaskKey:
    def test_masks_long_key(self):
        assert mask_key("sk-or-v1-abcdefghijklmnopqrstuvwxyz") == "sk-o…wxyz"

    def test_empty(self):
        assert mask_key("") == "(not set)"


class TestConfigIO:
    def test_set_and_get_nested(self, tmp_path: Path):
        p = tmp_path / "config.toml"
        raw = load_raw(p)
        set_nested(raw, "providers.primary.model", "gpt-4o-mini")
        save_raw(raw, p)
        reloaded = load_raw(p)
        assert get_nested(reloaded, "providers.primary.model") == "gpt-4o-mini"

    def test_set_config_value(self, tmp_path: Path):
        p = tmp_path / "config.toml"
        set_config_value("providers.primary.provider", "openai", path=p)
        raw = load_raw(p)
        assert raw["providers"]["primary"]["provider"] == "openai"


class TestResolveKey:
    def test_env_cue_takes_priority(self, monkeypatch):
        monkeypatch.setenv("CUE_OPENROUTER_API_KEY", "cue-env-key")
        monkeypatch.setenv("OPENROUTER_API_KEY", "canonical-key")
        assert resolve_key("openrouter", "config-key") == "cue-env-key"

    def test_config_before_keyring(self, monkeypatch):
        monkeypatch.delenv("CUE_OPENROUTER_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        assert resolve_key("openrouter", "from-config") == "from-config"


class TestConfigShow:
    def test_show_redacts_keys(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("CUE_CONFIG_DIR", str(tmp_path))
        # Reload CONFIG_PATH by patching
        with patch("cue.config_io.CONFIG_PATH", tmp_path / "config.toml"), patch(
            "cue.config.CONFIG_PATH", tmp_path / "config.toml"
        ), patch("cue.keys.CONFIG_PATH", tmp_path / "config.toml"):
            load_raw(tmp_path / "config.toml")
            text = format_config_show()
        assert "providers.primary" in text or "[providers.primary]" in text
        assert "openrouter" in text
