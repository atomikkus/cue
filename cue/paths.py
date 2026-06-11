"""Resolve cue config, socket, and pid paths (WSL-safe).

On WSL, HOME is often under /mnt/c (Windows profile). Unix domain sockets cannot
bind there (errno 95). Redirect to the Linux home config dir automatically.
"""

from __future__ import annotations

import os
from pathlib import Path


def is_wsl() -> bool:
    try:
        with open("/proc/version", encoding="utf-8") as fh:
            text = fh.read().lower()
        return "microsoft" in text or "wsl" in text
    except OSError:
        return False


def is_windows_mount(path: Path | str) -> bool:
    return str(path).startswith("/mnt/")


def wsl_linux_home() -> Path | None:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    if not user:
        return None
    home = Path(f"/home/{user}")
    if home.is_dir() and os.access(home, os.W_OK):
        return home
    return None


def resolve_config_dir() -> Path:
    """Return the cue config directory, redirecting off /mnt/* on WSL."""
    raw = os.environ.get("CUE_CONFIG_DIR", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
    else:
        candidate = Path.home() / ".config" / "cue"

    if is_wsl() and is_windows_mount(candidate):
        linux_home = wsl_linux_home()
        if linux_home is not None:
            return linux_home / ".config" / "cue"
    return candidate


def resolve_socket_path() -> Path:
    raw = os.environ.get("CUE_SOCKET", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if is_wsl() and is_windows_mount(path.parent):
            return resolve_config_dir() / "daemon.sock"
        return path
    return resolve_config_dir() / "daemon.sock"


def resolve_pid_path() -> Path:
    raw = os.environ.get("CUE_PID", "").strip()
    if raw:
        path = Path(raw).expanduser()
        if is_wsl() and is_windows_mount(path.parent):
            return resolve_config_dir() / "daemon.pid"
        return path
    return resolve_config_dir() / "daemon.pid"
