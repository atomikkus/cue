"""Tests for WSL-safe path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from cue import paths


class TestResolveConfigDir:
    def test_explicit_config_dir(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("CUE_CONFIG_DIR", str(tmp_path / "cue"))
        assert paths.resolve_config_dir() == tmp_path / "cue"

    def test_default_uses_home(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("CUE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(paths, "is_wsl", lambda: False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "user"))
        assert paths.resolve_config_dir() == tmp_path / "user" / ".config" / "cue"

    def test_wsl_redirects_off_windows_mount(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("CUE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(paths, "is_wsl", lambda: True)
        monkeypatch.setattr(paths, "wsl_linux_home", lambda: tmp_path / "satya")
        monkeypatch.setattr(
            Path,
            "home",
            staticmethod(lambda: Path("/mnt/c/Users/satya")),
        )
        assert paths.resolve_config_dir() == tmp_path / "satya" / ".config" / "cue"


class TestResolveSocketPath:
    def test_defaults_to_config_dir_socket(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("CUE_SOCKET", raising=False)
        monkeypatch.setenv("CUE_CONFIG_DIR", str(tmp_path / "cue"))
        assert paths.resolve_socket_path() == tmp_path / "cue" / "daemon.sock"

    def test_wsl_redirects_socket_off_mnt(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("CUE_SOCKET", "/mnt/c/Users/satya/.config/cue/daemon.sock")
        monkeypatch.setattr(paths, "is_wsl", lambda: True)
        monkeypatch.setattr(paths, "wsl_linux_home", lambda: tmp_path / "satya")
        monkeypatch.delenv("CUE_CONFIG_DIR", raising=False)
        monkeypatch.setattr(
            Path,
            "home",
            staticmethod(lambda: Path("/mnt/c/Users/satya")),
        )
        assert paths.resolve_socket_path() == tmp_path / "satya" / ".config" / "cue" / "daemon.sock"
