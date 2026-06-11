"""Unix domain socket daemon — the warm Python process that lives for the shell session.

Start:  cue-daemon start
Stop:   cue-daemon stop
Health: cue-daemon health

The daemon loads all heavy state (embedding model, SQLite, providers) once at startup.
The shell widget sends lightweight JSON requests; the daemon replies with JSON + newline.

Protocol: newline-framed JSON over a Unix domain socket.
  Request:  <json>\n
  Response: <json>\n
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import NoReturn

log = logging.getLogger(__name__)

_DEFAULT_SOCKET_PATH = Path(os.environ.get("CUE_SOCKET", "~/.config/cue/daemon.sock")).expanduser()
_DEFAULT_PID_PATH = Path(os.environ.get("CUE_PID", "~/.config/cue/daemon.pid")).expanduser()
_BACKLOG = 8


def _setup_logging(level: str = "WARNING") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _build_router():
    """Load all heavy state and return a configured Router instance."""
    from cue.config import ConfigManager  # noqa: PLC0415
    from cue import embedder as emb_mod  # noqa: PLC0415
    from cue.history import ingest_history  # noqa: PLC0415
    from cue.providers.registry import build_registry  # noqa: PLC0415
    from cue.resolver import Resolver  # noqa: PLC0415
    from cue.router import Router  # noqa: PLC0415
    from cue.store import Store  # noqa: PLC0415

    config_mgr = ConfigManager()
    cfg = config_mgr.config

    # Storage
    store = Store(
        cfg.cache.resolved_db_path,
        history_max_entries=cfg.cache.history_max_entries,
    )

    # Embedding model loads lazily on first Tier 1/2/3 query (fast daemon startup).

    # Provider registry
    registry = build_registry(cfg)

    # Resolver
    resolver = Resolver(
        store=store,
        embedder=emb_mod,
        providers=registry,
        primary_provider_name=cfg.primary.provider,
        primary_model=cfg.primary.model,
        primary_max_tokens=cfg.primary.max_tokens,
        escalate_provider_name=cfg.escalate.provider,
        escalate_model=cfg.escalate.model,
        escalate_max_tokens=cfg.escalate.max_tokens,
        similarity_threshold=cfg.cache.similarity_threshold,
        history_threshold=cfg.cache.history_threshold,
        alignment_threshold=cfg.cache.alignment_threshold,
        embedding_model=cfg.embeddings.model,
        danger_scan=cfg.safety.danger_scan,
        redact=cfg.safety.redact_secrets,
        telemetry_enabled=cfg.telemetry.enabled,
    )

    # Background history ingestion (non-blocking)
    def _bg_ingest():
        try:
            store.history_purge_non_commands()
            ingest_history(store, emb_mod.embed_batch, cfg.embeddings.model, source="auto")
        except Exception as exc:
            log.warning("Background history ingestion failed: %s", exc)

    t = threading.Thread(target=_bg_ingest, daemon=True, name="history-ingest")
    t.start()

    return Router(
        resolver=resolver,
        store=store,
        config_manager=config_mgr,
        embedder=emb_mod,
        embedding_model=cfg.embeddings.model,
        telemetry_enabled=cfg.telemetry.enabled,
    )


class DaemonServer:
    """Unix socket server that handles one JSON request per connection."""

    def __init__(self, socket_path: Path, router) -> None:
        self.socket_path = socket_path
        self.router = router
        self._sock: socket.socket | None = None
        self._running = False

    def start(self) -> None:
        # Remove stale socket file
        if self.socket_path.exists():
            self.socket_path.unlink()

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(_BACKLOG)
        # Restrict permissions to owner only
        os.chmod(str(self.socket_path), 0o600)

        self._running = True
        log.info("cue daemon listening on %s", self.socket_path)

        while self._running:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                daemon=True,
            )
            t.start()

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            with conn:
                data = b""
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break

                if not data.strip():
                    return

                try:
                    request = json.loads(data.decode("utf-8").strip())
                except json.JSONDecodeError as exc:
                    response = {"ok": False, "error": f"JSON decode error: {exc}"}
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
                    return

                response = self.router.handle(request)
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))

        except Exception as exc:
            log.exception("Error handling connection: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _write_pid(pid_path: Path) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()))


def _read_pid(pid_path: Path) -> int | None:
    try:
        return int(pid_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_running(pid_path: Path) -> int | None:
    pid = _read_pid(pid_path)
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return None


def _run_daemon(socket_path: Path, pid_path: Path, log_level: str) -> NoReturn:
    """Run the daemon process until SIGTERM/SIGINT."""
    _setup_logging(log_level)
    log.info("Starting cue daemon (pid=%d)", os.getpid())
    _write_pid(pid_path)

    router = _build_router()
    server = DaemonServer(socket_path, router)

    def _shutdown(_sig, _frame):
        log.info("Shutting down cue daemon.")
        server.stop()
        pid_path.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.start()
    sys.exit(0)


def _wait_for_socket(socket_path: Path, timeout_s: float = 60.0) -> bool:
    import time  # noqa: PLC0415

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if socket_path.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(1.0)
                    s.connect(str(socket_path))
                    s.sendall(json.dumps({"op": "health"}).encode() + b"\n")
                    data = s.recv(4096)
                resp = json.loads(data.decode().strip())
                if resp.get("ok"):
                    return True
            except OSError:
                pass
        time.sleep(0.25)
    return False


def _daemon_argv(
    socket_path: Path,
    pid_path: Path,
    log_level: str,
    *,
    foreground: bool,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        "cue.daemon",
        "start",
        "--socket",
        str(socket_path),
        "--pid",
        str(pid_path),
        "--log-level",
        log_level,
    ]
    if foreground:
        argv.append("--foreground")
    return argv


def _cmd_start(socket_path: Path, pid_path: Path, log_level: str, *, foreground: bool = False) -> None:
    """Start the daemon. Returns to the shell unless --foreground is set."""
    running = _is_running(pid_path)
    if running is not None:
        print(f"cue daemon already running (pid={running}).")
        return

    if foreground:
        _run_daemon(socket_path, pid_path, log_level)

    print("Starting cue daemon...")
    # subprocess spawn — safe on macOS (os.fork() crashes Python after ObjC init)
    proc = subprocess.Popen(
        _daemon_argv(socket_path, pid_path, log_level, foreground=True),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    if _wait_for_socket(socket_path):
        ready_pid = _read_pid(pid_path)
        print(f"cue daemon ready (pid={ready_pid}).")
        return

    if proc.poll() is not None:
        print(f"cue daemon failed to start (exit code={proc.returncode}).")
        print("Run with logs visible: cue-daemon start --foreground")
        sys.exit(1)

    print("cue daemon is still starting.")
    print("Check status with: cue-daemon health")


def _cmd_stop(pid_path: Path) -> None:
    pid = _read_pid(pid_path)
    if pid is None:
        print("cue daemon is not running (no PID file).")
        return
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to cue daemon (pid={pid}).")
    except ProcessLookupError:
        print(f"No process with pid={pid}; cleaning up PID file.")
        pid_path.unlink(missing_ok=True)


def _cmd_health(socket_path: Path) -> None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(str(socket_path))
            s.sendall(json.dumps({"op": "health"}).encode() + b"\n")
            data = s.recv(4096)
        resp = json.loads(data.decode().strip())
        if resp.get("ok"):
            print(f"cue daemon OK  uptime={resp.get('uptime_seconds')}s  history={resp.get('history_entries')} entries")
        else:
            print(f"Daemon error: {resp.get('error')}")
            sys.exit(1)
    except Exception as exc:
        print(f"cue daemon unreachable: {exc}")
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    import argparse  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="cue-daemon", description="cue background daemon")
    ap.add_argument("command", choices=["start", "stop", "health", "restart"],
                    nargs="?", default="start")
    ap.add_argument("--socket", default=str(_DEFAULT_SOCKET_PATH), help="Socket path")
    ap.add_argument("--pid", default=str(_DEFAULT_PID_PATH), help="PID file path")
    ap.add_argument("--log-level", default="WARNING", help="Logging level")
    ap.add_argument(
        "--foreground", "-f",
        action="store_true",
        help="Run in the foreground (for debugging; blocks the terminal)",
    )
    args = ap.parse_args(argv)

    socket_path = Path(args.socket).expanduser()
    pid_path = Path(args.pid).expanduser()

    if args.command == "start":
        _cmd_start(socket_path, pid_path, args.log_level, foreground=args.foreground)
    elif args.command == "stop":
        _cmd_stop(pid_path)
    elif args.command == "health":
        _cmd_health(socket_path)
    elif args.command == "restart":
        _cmd_stop(pid_path)
        import time  # noqa: PLC0415
        time.sleep(0.5)
        _cmd_start(socket_path, pid_path, args.log_level, foreground=args.foreground)


if __name__ == "__main__":
    main()
