"""cue CLI entry point.

Usage:
  cue                 -- generate a command interactively (terminal mode)
  cue stats           -- show hit rates and token usage
  cue health          -- check daemon status
  cue daemon start    -- start the background daemon
  cue daemon stop     -- stop the background daemon
  cue reindex         -- rebuild the history embedding index
  cue setup           -- interactive provider, key, and model setup
  cue config show     -- show config and key status (redacted)
  cue config set ...  -- update a config value
  cue key list        -- list API key sources per provider
  cue key set ...     -- store API key in OS keychain
  cue install-shell   -- install/update the zsh or bash widget from the package
  cue doctor          -- verify install, daemon, and keybindings
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from cue.paths import resolve_socket_path

_SOCKET_PATH = resolve_socket_path()


def _send_request(request: dict, timeout: float = 15.0) -> dict:
    """Send a JSON request to the daemon and return the parsed response."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(_SOCKET_PATH))
            s.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
        return json.loads(data.decode("utf-8").strip())
    except FileNotFoundError:
        return {"ok": False, "error": "Daemon socket not found. Run: cue-daemon start"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _cmd_stats() -> None:
    resp = _send_request({"op": "stats"})
    if not resp.get("ok"):
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)
    stats = resp.get("stats", {})
    total = stats.get("total", 0)
    tier_counts = stats.get("tier_counts", {})
    hit_rate = stats.get("local_hit_rate", 0.0)
    tokens_in = stats.get("total_tokens_in", 0)
    tokens_out = stats.get("total_tokens_out", 0)
    history = stats.get("history_entries", 0)

    print("cue stats")
    print("─" * 40)
    print(f"  Total queries:     {total}")
    print(f"  Local hit rate:    {hit_rate:.1%}  (Tier 0/1/2 — zero API cost)")
    for tier in sorted(tier_counts):
        label = {0: "Exact match", 1: "Semantic cache", 2: "History search", 3: "LLM generation"}.get(tier, f"Tier {tier}")
        print(f"    Tier {tier} ({label}):  {tier_counts[tier]}")
    print(f"  Total tokens in:   {tokens_in:,}")
    print(f"  Total tokens out:  {tokens_out:,}")
    print(f"  History entries:   {history:,}")


def _cmd_health() -> None:
    resp = _send_request({"op": "health"})
    if resp.get("ok"):
        print(f"cue daemon OK  uptime={resp.get('uptime_seconds')}s  history={resp.get('history_entries')} entries")
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


def _cmd_generate(query: str) -> None:
    """Send a generate request (useful for testing from the command line)."""
    import platform
    import subprocess
    cwd = os.getcwd()
    git_branch = ""
    try:
        git_branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        pass

    request = {
        "op": "generate",
        "query": query,
        "context": {
            "cwd": cwd,
            "git_branch": git_branch,
            "last_exit_code": 0,
            "shell": os.environ.get("SHELL", "zsh"),
            "os": platform.system().lower(),
        },
    }
    resp = _send_request(request, timeout=30.0)
    if resp.get("ok") and resp.get("command"):
        print(resp["command"])
        tier_labels = {0: "exact cache", 1: "semantic cache", 2: "history", 3: "LLM"}
        tier = resp.get("tier", -1)
        print(f"  [source: {tier_labels.get(tier, str(tier))}]", file=sys.stderr)
    else:
        print(f"Error: {resp.get('error', 'No command returned')}", file=sys.stderr)
        sys.exit(1)


def _cmd_reindex(*, force: bool = False) -> None:
    """Rebuild the history index via the running daemon."""
    resp = _send_request({"op": "reindex", "force": force}, timeout=120.0)
    if not resp.get("ok"):
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)
    count = resp.get("indexed", 0)
    label = "reindexed" if force else "newly indexed"
    print(f"History {label}: {count} command(s)")


def _cmd_reload() -> None:
    """Reload daemon config (SIGHUP-equivalent)."""
    resp = _send_request({"op": "reload"})
    if resp.get("ok"):
        print(resp.get("message", "Config reloaded."))
    else:
        print(f"Error: {resp.get('error')}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    args = (argv or sys.argv)[1:]

    if not args:
        print("Usage: cue <command>")
        print("Commands: setup, config, key, stats, health, generate, daemon, reindex, reload, install-shell, doctor")
        sys.exit(0)

    cmd = args[0]

    if cmd == "stats":
        _cmd_stats()
    elif cmd == "health":
        _cmd_health()
    elif cmd == "generate":
        query = " ".join(args[1:])
        if not query:
            print("Usage: cue generate <natural language query>", file=sys.stderr)
            sys.exit(1)
        _cmd_generate(query)
    elif cmd == "daemon":
        # Forward to daemon CLI
        from cue.daemon import main as daemon_main  # noqa: PLC0415
        daemon_main(args[1:])
    elif cmd == "reindex":
        force = "--force" in args[1:]
        _cmd_reindex(force=force)
    elif cmd == "reload":
        _cmd_reload()
    elif cmd == "setup":
        from cue.cli_setup import run_setup  # noqa: PLC0415
        sys.exit(run_setup())
    elif cmd == "config":
        from cue.cli_setup import main_config  # noqa: PLC0415
        sys.exit(main_config(args[1:]))
    elif cmd == "key":
        from cue.cli_setup import main_key  # noqa: PLC0415
        sys.exit(main_key(args[1:]))
    elif cmd == "install-shell":
        from cue.shell_install import detect_shell, install_shell_widget, profile_path  # noqa: PLC0415
        shell = detect_shell()
        if shell not in {"zsh", "bash"}:
            print(
                f"Unsupported shell: {shell}. Use zsh/bash or pass via SHELL env.",
                file=sys.stderr,
            )
            sys.exit(1)
        dest = install_shell_widget(shell)
        print(f"Installed shell widget: {dest}")
        print(f"Reload your shell:  source {profile_path(shell)}")
    elif cmd == "doctor":
        from cue.shell_install import run_doctor  # noqa: PLC0415
        sys.exit(run_doctor())
    else:
        # Treat as implicit "generate"
        query = " ".join(args)
        _cmd_generate(query)


if __name__ == "__main__":
    main()
