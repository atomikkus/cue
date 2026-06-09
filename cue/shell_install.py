"""Install and verify the bundled zsh widget."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from cue.config import CONFIG_DIR


def install_shell_widget(target_dir: Path | None = None) -> Path:
    """Copy the packaged cue.zsh widget into the config directory."""
    dest_dir = (target_dir or CONFIG_DIR).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "cue.zsh"

    with resources.files("cue.shell").joinpath("cue.zsh").open("rb") as src:
        with dest.open("wb") as out:
            shutil.copyfileobj(src, out)

    return dest


def _zshrc_path() -> Path:
    zdotdir = os.environ.get("ZDOTDIR", "").strip()
    if zdotdir:
        return Path(zdotdir).expanduser() / ".zshrc"
    return Path.home() / ".zshrc"


def zshrc_has_hooks() -> bool:
    zshrc = _zshrc_path()
    if not zshrc.is_file():
        return False
    text = zshrc.read_text(encoding="utf-8", errors="replace")
    return 'source "${HOME}/.config/cue/cue.zsh"' in text


def run_doctor() -> int:
    """Print install health checks. Returns 0 if all critical checks pass."""
    ok = True
    venv_bin = CONFIG_DIR / "venv" / "bin"
    widget = CONFIG_DIR / "cue.zsh"
    socket = Path(os.environ.get("CUE_SOCKET", str(CONFIG_DIR / "daemon.sock"))).expanduser()

    print("cue doctor")
    print("─" * 40)

    def check(label: str, passed: bool, detail: str, *, critical: bool = True) -> None:
        nonlocal ok
        mark = "✓" if passed else ("✗" if critical else "!")
        print(f"  {mark} {label}: {detail}")
        if not passed and critical:
            ok = False

    check("Python package", shutil.which("cue") is not None, shutil.which("cue") or "not on PATH")
    check("Daemon binary", shutil.which("cue-daemon") is not None, shutil.which("cue-daemon") or "not on PATH")
    check("Shell widget", widget.is_file(), str(widget))
    check("~/.zshrc hooks", zshrc_has_hooks(), str(_zshrc_path()))

    if shutil.which("cue-daemon"):
        try:
            proc = subprocess.run(
                ["cue-daemon", "health"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            healthy = proc.returncode == 0
            detail = proc.stdout.strip() or proc.stderr.strip() or f"exit {proc.returncode}"
        except Exception as exc:
            healthy = False
            detail = str(exc)
        check("Daemon health", healthy, detail)
    else:
        check("Daemon health", False, "cue-daemon not found")

    check("Unix socket", socket.exists(), str(socket), critical=False)

    if widget.is_file():
        text = widget.read_text(encoding="utf-8", errors="replace")
        has_fix = "_cue_read_line" in text and "read -k 1 char" in text
        check("Widget input fix", has_fix, "read -k ZLE input present" if has_fix else "outdated widget — run: cue install-shell")

    bind_check = subprocess.run(
        ["zsh", "-lic", 'source "${HOME}/.config/cue/cue.zsh" 2>/dev/null; bindkey | grep _cue_generate'],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    binding = bind_check.stdout.strip() or "not bound"
    check("Ctrl+K binding", "^K" in binding and "_cue_generate" in binding, binding, critical=False)

    has_key = any(
        os.environ.get(name)
        for name in (
            "OPENROUTER_API_KEY",
            "CUE_OPENROUTER_API_KEY",
            "ANTHROPIC_API_KEY",
            "CUE_ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "CUE_OPENAI_API_KEY",
        )
    )
    check(
        "API key in shell env",
        has_key,
        "set in this shell (daemon needs key at startup — add to ~/.zshrc)",
        critical=False,
    )

    print("─" * 40)
    if ok:
        print("  All critical checks passed.")
        print("  Press Ctrl+K at a zsh prompt (rebind if Cursor steals ^K).")
        return 0

    print("  Some checks failed. Run: ./install.sh  or  cue install-shell")
    return 1


def main(argv: list[str] | None = None) -> None:
    args = (argv or sys.argv)[1:]
    if args and args[0] == "doctor":
        sys.exit(run_doctor())

    dest = install_shell_widget()
    print(f"Installed shell widget: {dest}")
    print("Reload your shell:  source ~/.zshrc")
