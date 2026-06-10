"""Request router — dispatches incoming JSON ops to the right handler.

Supported ops:
  generate   — NL query → shell command (uses full tier engine)
  explain    — explain the current buffer command
  fix_last   — fix the last failed command
  index_cmd  — index a single command from precmd hook (fire-and-forget)
  health     — ping / liveness check
  stats      — return telemetry stats
  reload     — SIGHUP-equivalent: reload config
  reindex    — rebuild history embedding index
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cue.config import Config
    from cue.context import ShellContext
    from cue.resolver import Resolver
    from cue.store import Store

log = logging.getLogger(__name__)


class Router:
    """Dispatches requests to the correct handler."""

    def __init__(
        self,
        resolver: "Resolver",
        store: "Store",
        config_manager,  # ConfigManager
        embedder,
        embedding_model: str = "all-MiniLM-L6-v2",
        telemetry_enabled: bool = False,
    ) -> None:
        self.resolver = resolver
        self.store = store
        self.config_manager = config_manager
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.telemetry_enabled = telemetry_enabled
        self._start_time = time.time()

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Process a single decoded JSON request, return a JSON-serializable response."""
        op = request.get("op", "generate")

        try:
            if op == "generate":
                return self._handle_generate(request)
            elif op == "explain":
                return self._handle_explain(request)
            elif op == "fix_last":
                return self._handle_fix_last(request)
            elif op == "index_cmd":
                return self._handle_index_cmd(request)
            elif op == "health":
                return self._handle_health()
            elif op == "stats":
                return self._handle_stats()
            elif op == "reload":
                return self._handle_reload()
            elif op == "reindex":
                return self._handle_reindex(request)
            else:
                return {"ok": False, "error": f"Unknown op: {op}"}
        except Exception as exc:
            log.exception("Router error handling op=%s", op)
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Op handlers
    # ------------------------------------------------------------------

    def _handle_generate(self, request: dict) -> dict:
        query = (request.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "Empty query"}

        from cue.context import from_client_payload  # noqa: PLC0415
        context = from_client_payload(request.get("context", {}))
        context.query = query

        result = self.resolver.resolve(query, context, op="generate")
        return self._result_to_response(result)

    def _handle_explain(self, request: dict) -> dict:
        """Explain the current buffer command in plain English."""
        from cue.context import from_client_payload  # noqa: PLC0415
        context = from_client_payload(request.get("context", {}))
        buffer = context.buffer or request.get("buffer", "")
        if not buffer:
            return {"ok": False, "error": "No command to explain"}

        context.buffer = buffer
        query = f"Explain what this command does: {buffer}"

        result = self.resolver.resolve(query, context, op="explain")
        return self._result_to_response(result)

    def _handle_fix_last(self, request: dict) -> dict:
        """Fix the last failed command."""
        from cue.context import from_client_payload  # noqa: PLC0415
        context = from_client_payload(request.get("context", {}))
        if not context.buffer:
            return {"ok": False, "error": "No last command to fix"}

        query = request.get("query", f"fix the failed command: {context.buffer}")
        context.query = query

        result = self.resolver.resolve(query, context, op="fix_last")
        return self._result_to_response(result)

    def _handle_index_cmd(self, request: dict) -> dict:
        """Incrementally index a single new command from precmd hook."""
        command = (request.get("command") or "").strip()
        if not command:
            return {"ok": True, "indexed": False}

        from cue.history import index_single_command  # noqa: PLC0415
        index_single_command(
            command,
            self.store,
            self.embedder.embed,
            self.embedding_model,
        )
        return {"ok": True, "indexed": True}

    def _handle_health(self) -> dict:
        uptime = int(time.time() - self._start_time)
        return {
            "ok": True,
            "uptime_seconds": uptime,
            "history_entries": self.store.history_count(),
        }

    def _handle_stats(self) -> dict:
        stats = self.store.telemetry_stats()
        return {"ok": True, "stats": stats}

    def _handle_reload(self) -> dict:
        try:
            self.config_manager.reload()
            return {"ok": True, "message": "Config reloaded."}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _handle_reindex(self, request: dict) -> dict:
        """Rebuild the shell history embedding index."""
        from cue.history import ingest_history  # noqa: PLC0415

        force = bool(request.get("force", False))
        source = request.get("source", "zsh")
        try:
            count = ingest_history(
                self.store,
                self.embedder.embed_batch,
                self.embedding_model,
                source=source,
                force=force,
            )
            return {"ok": True, "indexed": count, "force": force}
        except Exception as exc:
            log.exception("Reindex failed")
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _result_to_response(result) -> dict:
        resp: dict[str, Any] = {
            "ok": result.error is None or bool(result.command),
            "command": result.command,
            "tier": result.tier,
            "confidence": result.confidence,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
        }
        if result.error:
            resp["error"] = result.error
        if result.model:
            resp["model"] = result.model
        if result.provider_name:
            resp["provider"] = result.provider_name
        return resp
