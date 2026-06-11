"""Tests for shell detection and widget install helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from cue.shell_install import (
    CUE_BASH_HOOK,
    CUE_ZSH_HOOK,
    bashrc_path,
    detect_shell,
    install_shell_widget,
    profile_has_hooks,
    profile_hook_line,
    zshrc_path,
)


class TestDetectShell:
    def test_auto_zsh(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert detect_shell() == "zsh"

    def test_auto_bash(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/bash")
        assert detect_shell() == "bash"

    def test_explicit_override(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert detect_shell("bash") == "bash"

    def test_unsupported_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/bin/fish")
        assert detect_shell() == "other"


class TestProfilePaths:
    def test_zshrc_default(self, monkeypatch):
        monkeypatch.delenv("ZDOTDIR", raising=False)
        assert zshrc_path() == Path.home() / ".zshrc"

    def test_bashrc_default(self, monkeypatch):
        monkeypatch.delenv("BASH_ENV", raising=False)
        assert bashrc_path() == Path.home() / ".bashrc"


class TestProfileHooks:
    def test_hook_lines(self):
        assert profile_hook_line("zsh") == CUE_ZSH_HOOK
        assert profile_hook_line("bash") == CUE_BASH_HOOK

    def test_profile_has_hooks(self, tmp_path: Path, monkeypatch):
        profile = tmp_path / ".bashrc"
        profile.write_text(f"# cue\n{CUE_BASH_HOOK}\n", encoding="utf-8")
        monkeypatch.setattr("cue.shell_install.bashrc_path", lambda: profile)
        assert profile_has_hooks("bash") is True


class TestInstallShellWidget:
    def test_installs_bash_widget(self, tmp_path: Path):
        dest = install_shell_widget("bash", target_dir=tmp_path)
        assert dest == tmp_path / "cue.bash"
        assert dest.is_file()
        text = dest.read_text(encoding="utf-8")
        assert "_cue_generate" in text
        assert "_cue_read_line" in text
        assert "READLINE_LINE" in text
        assert "read -e -r" not in text

    def test_installs_zsh_widget(self, tmp_path: Path):
        dest = install_shell_widget("zsh", target_dir=tmp_path)
        assert dest == tmp_path / "cue.zsh"
        assert dest.is_file()

    def test_rejects_unsupported_shell(self, tmp_path: Path):
        with pytest.raises(ValueError, match="Unsupported shell"):
            install_shell_widget("fish", target_dir=tmp_path)
