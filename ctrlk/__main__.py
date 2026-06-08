"""ctrlk CLI entry point.

Usage:
  ctrlk                 -- generate a command interactively (terminal mode)
  ctrlk stats           -- show hit rates and token usage
  ctrlk health          -- check daemon status
  ctrlk daemon start    -- start the background daemon
  ctrlk daemon stop     -- stop the background daemon
  ctrlk reindex         -- rebuild the history embedding index
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

_SOCKET_PATH = Path(os.environ.get("CTRLK_SOCKET", "~/.config/ctrlk/daemon.sock")).expanduser()


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
        return {"ok": False, "error": "Daemon socket not found. Run: ctrlk-daemon start"}
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

    print("ctrlk stats")
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
        print(f"ctrlk daemon OK  uptime={resp.get('uptime_seconds')}s  history={resp.get('history_entries')} entries")
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


def _cmd_reindex() -> None:
    """Rebuild the history index (runs in the daemon process)."""
    print("Reindex is performed by the daemon on startup.")
    print("To force: stop the daemon, delete ~/.config/ctrlk/cache.db, restart.")


def main(argv: list[str] | None = None) -> None:
    args = (argv or sys.argv)[1:]

    if not args:
        print("Usage: ctrlk <command>")
        print("Commands: stats, health, generate <query>, daemon, reindex")
        sys.exit(0)

    cmd = args[0]

    if cmd == "stats":
        _cmd_stats()
    elif cmd == "health":
        _cmd_health()
    elif cmd == "generate":
        query = " ".join(args[1:])
        if not query:
            print("Usage: ctrlk generate <natural language query>", file=sys.stderr)
            sys.exit(1)
        _cmd_generate(query)
    elif cmd == "daemon":
        # Forward to daemon CLI
        from ctrlk.daemon import main as daemon_main  # noqa: PLC0415
        daemon_main(args[1:])
    elif cmd == "reindex":
        _cmd_reindex()
    else:
        # Treat as implicit "generate"
        query = " ".join(args)
        _cmd_generate(query)


if __name__ == "__main__":
    main()
