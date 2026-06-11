"""Install and verify zsh/bash shell widgets."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from cue.config import CONFIG_DIR
from cue.keys import key_status

SUPPORTED_SHELLS = frozenset({"zsh", "bash"})
CUE_PATH_LINE = 'export PATH="${HOME}/.config/cue/venv/bin:$PATH"'
CUE_ZSH_HOOK = 'source "${HOME}/.config/cue/cue.zsh"'
CUE_BASH_HOOK = 'source "${HOME}/.config/cue/cue.bash"'
CUE_DAEMON_LAUNCH = "(cue-daemon start --no-wait &>/dev/null &)"


def detect_shell(explicit: str | None = None) -> str:
    """Return zsh, bash, or other."""
    if explicit and explicit != "auto":
        return explicit.lower()
    shell_path = os.environ.get("SHELL", "")
    name = Path(shell_path).name.lower() if shell_path else ""
    if name in SUPPORTED_SHELLS:
        return name
    return "other"


def zshrc_path() -> Path:
    zdotdir = os.environ.get("ZDOTDIR", "").strip()
    if zdotdir:
        return Path(zdotdir).expanduser() / ".zshrc"
    return Path.home() / ".zshrc"


def bashrc_path() -> Path:
    bash_env = os.environ.get("BASH_ENV", "").strip()
    if bash_env and Path(bash_env).expanduser().is_file():
        return Path(bash_env).expanduser()
    return Path.home() / ".bashrc"


def profile_path(shell: str) -> Path:
    if shell == "zsh":
        return zshrc_path()
    if shell == "bash":
        return bashrc_path()
    raise ValueError(f"Unsupported shell: {shell}")


def profile_hook_line(shell: str) -> str:
    if shell == "zsh":
        return CUE_ZSH_HOOK
    if shell == "bash":
        return CUE_BASH_HOOK
    raise ValueError(f"Unsupported shell: {shell}")


def widget_filename(shell: str) -> str:
    return f"cue.{shell}"


def install_shell_widget(shell: str | None = None, target_dir: Path | None = None) -> Path:
    """Copy the packaged shell widget into the config directory."""
    shell_name = shell or detect_shell()
    if shell_name not in SUPPORTED_SHELLS:
        raise ValueError(f"Unsupported shell for widget install: {shell_name}")

    dest_dir = (target_dir or CONFIG_DIR).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / widget_filename(shell_name)

    with resources.files("cue.shell").joinpath(widget_filename(shell_name)).open("rb") as src:
        with dest.open("wb") as out:
            shutil.copyfileobj(src, out)

    return dest


def profile_has_hooks(shell: str) -> bool:
    path = profile_path(shell)
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    widget = CONFIG_DIR / widget_filename(shell)
    return str(widget) in text or profile_hook_line(shell) in text or f"cue.{shell}" in text


def _any_key_configured() -> bool:
    for provider in ("openrouter", "anthropic", "openai", "mistral", "custom"):
        if key_status(provider)[0] != "none":
            return True
    return False


def run_doctor() -> int:
    """Print install health checks. Returns 0 if all critical checks pass."""
    ok = True
    shell = detect_shell()
    system = platform.system().lower()
    widget = CONFIG_DIR / widget_filename(shell) if shell in SUPPORTED_SHELLS else None
    socket = Path(os.environ.get("CUE_SOCKET", str(CONFIG_DIR / "daemon.sock"))).expanduser()

    print("cue doctor")
    print("─" * 40)
    print(f"  OS: {system}   shell: {shell}")

    def check(label: str, passed: bool, detail: str, *, critical: bool = True) -> None:
        nonlocal ok
        mark = "✓" if passed else ("✗" if critical else "!")
        print(f"  {mark} {label}: {detail}")
        if not passed and critical:
            ok = False

    check("Python package", shutil.which("cue") is not None, shutil.which("cue") or "not on PATH")
    check("Daemon binary", shutil.which("cue-daemon") is not None, shutil.which("cue-daemon") or "not on PATH")

    if shell in SUPPORTED_SHELLS:
        assert widget is not None
        check("Shell widget", widget.is_file(), str(widget))
        check(f"{profile_path(shell).name} hooks", profile_has_hooks(shell), str(profile_path(shell)))
    else:
        check(
            "Inline shell integration",
            False,
            f"unsupported shell '{shell}' — use zsh/bash or `cue generate`",
            critical=False,
        )

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

    if shell == "zsh" and widget and widget.is_file():
        text = widget.read_text(encoding="utf-8", errors="replace")
        has_fix = "_cue_read_line" in text and "read -k 1 char" in text
        check(
            "Widget input fix",
            has_fix,
            "read -k ZLE input present" if has_fix else "outdated widget — run: cue install-shell",
            critical=False,
        )
        if shutil.which("zsh"):
            bind_check = subprocess.run(
                ["zsh", "-lic", 'source "${HOME}/.config/cue/cue.zsh" 2>/dev/null; bindkey | grep _cue_generate'],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            binding = bind_check.stdout.strip() or "not bound"
            check("Ctrl+K binding", "^K" in binding and "_cue_generate" in binding, binding, critical=False)
        else:
            check("zsh binary", False, "not found — install zsh for inline Ctrl+K", critical=False)

    if shell == "bash" and widget and widget.is_file():
        text = widget.read_text(encoding="utf-8", errors="replace")
        has_readline = "_cue_generate" in text and "READLINE_LINE" in text
        check(
            "Bash Readline widget",
            has_readline,
            "READLINE_LINE integration present" if has_readline else "outdated widget — run: cue install-shell",
            critical=False,
        )
        if shutil.which("bash"):
            bind_check = subprocess.run(
                ["bash", "-lic", 'source "${HOME}/.config/cue/cue.bash" 2>/dev/null; bind -p | grep _cue_generate'],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            binding = bind_check.stdout.strip() or "not bound"
            check("Ctrl+K binding", "_cue_generate" in binding, binding, critical=False)
        else:
            check("bash binary", False, "not found", critical=False)

    check(
        "API key configured",
        _any_key_configured(),
        "set via `cue setup` or `cue key set <provider>`",
        critical=False,
    )

    print("─" * 40)
    if ok:
        print("  All critical checks passed.")
        if shell in SUPPORTED_SHELLS:
            tip = "rebind if your terminal steals ^K: export CUE_KEY_GENERATE='^X^K'"
            if system == "darwin":
                print(f"  Press Ctrl+K at a {shell} prompt ({tip}).")
            else:
                print(f"  Press Ctrl+K at a {shell} prompt ({tip}).")
        else:
            print("  Use `cue generate \"your intent\"` from this shell.")
        return 0

    print("  Some checks failed. Run: ./install.sh  or  cue install-shell")
    return 1


def main(argv: list[str] | None = None) -> None:
    args = (argv or sys.argv)[1:]
    if args and args[0] == "doctor":
        sys.exit(run_doctor())

    shell = detect_shell(args[0] if args else None)
    if shell not in SUPPORTED_SHELLS:
        print(f"Unsupported shell: {shell}. Use zsh or bash.", file=sys.stderr)
        sys.exit(1)

    dest = install_shell_widget(shell)
    print(f"Installed shell widget: {dest}")
    profile = profile_path(shell)
    print(f"Reload your shell:  source {profile}")


if __name__ == "__main__":
    main()
