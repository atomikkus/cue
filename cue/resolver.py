"""Tiered resolution engine — the heart of cue.

Tier 0: Exact match (hash lookup, ~3ms)
Tier 1: Semantic cache (embedding cosine similarity ≥ threshold, ~30ms)
Tier 2: History semantic search (user's own commands, ~40ms)
Tier 3: LLM generation (small model first, escalate on failure, ~400-800ms)

Context-sensitivity guard: deictic queries (this/that/here/the last) require
a matching context_bucket on Tier-1/2 hits to prevent cross-project leakage.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import numpy as np

from cue.cache_promotion import (
    PENDING_TTL_SECONDS,
    REJECTION_TTL_SECONDS,
    REJECTION_WINDOW_SECONDS,
    commands_match,
    normalize_command_for_match,
)
from cue.context import ShellContext, is_context_sensitive, redact_secrets
from cue.providers.base import (
    DEFAULT_FEW_SHOT,
    SYSTEM_PROMPT,
    GenResult,
    Provider,
    few_shot_to_messages,
)
from cue.store import Store, blob_to_vec
from cue.validator import ValidationResult, is_likely_shell_command, validate

log = logging.getLogger(__name__)


@dataclass
class ResolveResult:
    """Returned to the router for every query."""
    command: str              # Final command for the ZLE buffer (may have ⚠ prefix)
    raw_command: str          # Pre-safety command (for logging/cache)
    tier: int                 # 0-3: which tier resolved the query
    confidence: float         # 0.0–1.0; 1.0 for exact, cosine sim for 1/2, 0.9 for T3
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    model: str = ""
    provider_name: str = ""
    error: str | None = None
    validation: ValidationResult | None = None


def _normalize(query: str) -> str:
    """Lowercase + collapse whitespace."""
    return re.sub(r"\s+", " ", query.strip().lower())


class Resolver:
    """Walks the tier ladder for each query."""

    def __init__(
        self,
        store: Store,
        embedder,           # module or object with embed(text, model) and embed_batch
        providers: dict[str, Provider],
        primary_provider_name: str,
        primary_model: str,
        primary_max_tokens: int,
        escalate_provider_name: str,
        escalate_model: str,
        escalate_max_tokens: int,
        similarity_threshold: float = 0.92,
        history_threshold: float = 0.88,
        alignment_threshold: float = 0.78,
        embedding_model: str = "all-MiniLM-L6-v2",
        danger_scan: bool = True,
        redact: bool = True,
        telemetry_enabled: bool = False,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.providers = providers
        self.primary_provider_name = primary_provider_name
        self.primary_model = primary_model
        self.primary_max_tokens = primary_max_tokens
        self.escalate_provider_name = escalate_provider_name
        self.escalate_model = escalate_model
        self.escalate_max_tokens = escalate_max_tokens
        self.similarity_threshold = similarity_threshold
        self.history_threshold = history_threshold
        self.alignment_threshold = alignment_threshold
        self.embedding_model = embedding_model
        self.danger_scan = danger_scan
        self.redact = redact
        self.telemetry_enabled = telemetry_enabled

        # Pre-computed few-shot message list (static; computed once)
        self._few_shot_messages = few_shot_to_messages(DEFAULT_FEW_SHOT)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def resolve(self, query: str, context: ShellContext, op: str = "generate") -> ResolveResult:
        """Walk tiers and return the first confident result."""
        t0 = time.monotonic()
        query = query.strip()
        norm = _normalize(query)
        use_cache = op == "generate"
        ctx_sensitive = is_context_sensitive(query) if use_cache else False
        ctx_hash = context.context_bucket_hash() if ctx_sensitive else None

        best_history_cmd: str | None = None
        query_vec: np.ndarray | None = None

        if use_cache:
            # --- Tier 0: exact match ---
            result = self._tier0(norm, ctx_hash)
            if result:
                self._log_telemetry(op, 0, 0, 0, t0)
                return result

            # --- Embed query (shared for Tier 1 and 2) ---
            try:
                query_vec = self.embedder.embed(query, self.embedding_model)
            except Exception as exc:
                log.warning("Embedding failed, falling through to Tier 3: %s", exc)
                query_vec = None

            if query_vec is not None:
                # --- Tier 1: semantic cache ---
                result, _best_cache_score = self._tier1(query, query_vec, ctx_hash, ctx_sensitive)
                if result:
                    # Queue the served command so a later correction can reject it.
                    self._queue_pending(
                        norm, query, query_vec, result.raw_command, ctx_hash, "", ""
                    )
                    self._log_telemetry(op, 1, 0, 0, t0)
                    return result

                # --- Tier 2: history search ---
                result, best_history_cmd = self._tier2(query_vec, ctx_hash, ctx_sensitive)
                if result:
                    self._log_telemetry(op, 2, 0, 0, t0)
                    return result
        elif op == "generate":
            pass  # unreachable — use_cache covers generate
        else:
            # explain / fix_last: still embed for Tier-3 history hint only
            try:
                query_vec = self.embedder.embed(query, self.embedding_model)
                if query_vec is not None:
                    _, best_history_cmd = self._tier2(query_vec, None, False)
            except Exception as exc:
                log.debug("Embedding for history hint failed: %s", exc)

        # --- Tier 3: LLM generation ---
        result = self._tier3(query, norm, context, query_vec, best_history_cmd, op)
        self._log_telemetry(op, 3, result.tokens_in, result.tokens_out, t0)
        return result

    # ------------------------------------------------------------------
    # Tier implementations
    # ------------------------------------------------------------------

    def _tier0(self, norm: str, ctx_hash: str | None) -> ResolveResult | None:
        cached = self.store.exact_get(norm, ctx_hash)
        if cached is None:
            return None
        if self.store.rejection_has_exact(
            norm,
            cached,
            ctx_hash,
            max_age_seconds=REJECTION_TTL_SECONDS,
        ):
            self.store.exact_delete(norm, cached, ctx_hash)
            return None
        v = validate(cached, danger_scan=self.danger_scan)
        return ResolveResult(
            command=v.safe_command,
            raw_command=cached,
            tier=0,
            confidence=1.0,
            validation=v,
        )

    def _tier1(
        self,
        query: str,
        query_vec: np.ndarray,
        ctx_hash: str | None,
        ctx_sensitive: bool,
    ) -> tuple[ResolveResult | None, float]:
        filter_hash = ctx_hash if ctx_sensitive else None
        rows, mat = self.store.semantic_get_matrix(query_vec.shape[0], filter_hash)
        if mat.shape[0] == 0:
            return None, 0.0

        k = min(5, mat.shape[0])
        hits = self.embedder.top_k_similar(query_vec, mat, k=k)
        if not hits:
            return None, 0.0

        best_score = hits[0][1]
        for best_idx, score in hits:
            if score < self.similarity_threshold:
                break

            row = rows[best_idx]
            if ctx_sensitive and ctx_hash and row.get("context_hash") and row["context_hash"] != ctx_hash:
                continue

            cmd = row["command"]
            if not is_likely_shell_command(cmd):
                continue

            if self._is_rejected_pair(query_vec, cmd, ctx_hash):
                log.debug("Tier 1 skip rejected pair: query=%r cmd=%r", query[:60], cmd[:60])
                continue

            align = self._query_command_alignment(query_vec, cmd)
            if align < self.alignment_threshold:
                log.debug(
                    "Tier 1 skip low alignment: query=%r cmd=%r align=%.3f",
                    query[:60],
                    cmd[:60],
                    align,
                )
                continue

            self.store.semantic_update_hit(row["id"])
            v = validate(cmd, danger_scan=self.danger_scan)
            return ResolveResult(
                command=v.safe_command,
                raw_command=cmd,
                tier=1,
                confidence=score,
                validation=v,
            ), best_score

        return None, best_score

    def _tier2(
        self,
        query_vec: np.ndarray,
        ctx_hash: str | None,
        ctx_sensitive: bool,
    ) -> tuple[ResolveResult | None, str | None]:
        del ctx_hash, ctx_sensitive  # history is personal, not project-scoped
        rows, mat = self.store.history_get_matrix(query_vec.shape[0])
        if mat.shape[0] == 0:
            return None, None

        hits = self.embedder.top_k_similar(query_vec, mat, k=5)
        if not hits:
            return None, None

        best_cmd = rows[hits[0][0]]["command"]

        for idx, score in hits:
            if score < self.history_threshold:
                break
            cmd = rows[idx]["command"]
            if not is_likely_shell_command(cmd):
                log.debug("Tier 2 skip non-command history: %r (score=%.3f)", cmd[:60], score)
                continue
            v = validate(cmd, danger_scan=self.danger_scan)
            if not v.is_valid:
                continue
            return ResolveResult(
                command=v.safe_command,
                raw_command=cmd,
                tier=2,
                confidence=score,
                validation=v,
            ), cmd

        return None, best_cmd

    def _tier3(
        self,
        query: str,
        norm: str,
        context: ShellContext,
        query_vec: np.ndarray | None,
        history_hint: str | None,
        op: str,
    ) -> ResolveResult:
        """LLM generation: try primary, escalate on validation failure."""
        dynamic_user = self._build_user_message(query, context, history_hint, op)

        primary = self.providers.get(self.primary_provider_name)
        if primary is None:
            return ResolveResult(
                command="", raw_command="", tier=3, confidence=0.0,
                error=f"Primary provider '{self.primary_provider_name}' not found.",
            )

        gen_result = primary.generate(
            SYSTEM_PROMPT,
            self._few_shot_messages,
            dynamic_user,
            model=self.primary_model,
            max_tokens=self.primary_max_tokens,
            stop=["\n", "```"],
        )

        if not isinstance(gen_result, GenResult):
            return ResolveResult(
                command="", raw_command="", tier=3, confidence=0.0,
                error="Unexpected streaming response from provider.",
            )

        if gen_result.error:
            log.warning("Primary provider error: %s", gen_result.error)
            return self._escalate(query, norm, context, dynamic_user, query_vec, gen_result.error, op)

        if op == "explain":
            return self._result_from_text(gen_result, op, skip_validate=True)

        validation = validate(gen_result.text, danger_scan=self.danger_scan)
        if not validation.is_valid:
            log.info("Primary validation failed (%s), escalating.", validation.parse_error)
            return self._escalate(
                query, norm, context, dynamic_user, query_vec, validation.parse_error, op
            )

        if op == "generate":
            ctx_hash = context.context_bucket_hash() if is_context_sensitive(query) else None
            self._queue_pending(
                norm, query, query_vec, gen_result.text, ctx_hash,
                gen_result.provider, gen_result.model,
            )

        return self._result_from_text(gen_result, op, validation=validation)

    def _escalate(
        self,
        query: str,
        norm: str,
        context: ShellContext,
        dynamic_user: str,
        query_vec: np.ndarray | None,
        prev_error: str | None,
        op: str,
    ) -> ResolveResult:
        """Retry with the escalation provider/model."""
        escalate = self.providers.get(self.escalate_provider_name)
        if escalate is None:
            return ResolveResult(
                command="", raw_command="", tier=3, confidence=0.0,
                error=f"Escalate provider '{self.escalate_provider_name}' not found.",
            )

        escalate_user = dynamic_user
        if prev_error:
            escalate_user = (
                f"{dynamic_user}\n\n"
                f"[Previous attempt failed: {prev_error}. Generate a corrected command only.]"
            )

        gen_result = escalate.generate(
            SYSTEM_PROMPT,
            self._few_shot_messages,
            escalate_user,
            model=self.escalate_model,
            max_tokens=self.escalate_max_tokens,
            stop=["\n", "```"],
        )

        if not isinstance(gen_result, GenResult):
            return ResolveResult(
                command="", raw_command="", tier=3, confidence=0.0,
                error="Unexpected streaming response from escalation provider.",
            )

        if gen_result.error:
            return ResolveResult(
                command="", raw_command="", tier=3, confidence=0.0,
                error=f"Escalation also failed: {gen_result.error}",
                tokens_in=gen_result.tokens_in,
                tokens_out=gen_result.tokens_out,
            )

        if op == "explain":
            return self._result_from_text(gen_result, op, skip_validate=True, confidence=0.85)

        validation = validate(gen_result.text, danger_scan=self.danger_scan)

        if op == "generate":
            ctx_hash = context.context_bucket_hash() if is_context_sensitive(query) else None
            self._queue_pending(
                norm, query, query_vec, gen_result.text, ctx_hash,
                gen_result.provider, gen_result.model,
            )

        return self._result_from_text(
            gen_result, op, validation=validation, confidence=0.85
        )

    def _result_from_text(
        self,
        gen_result: GenResult,
        op: str,
        *,
        validation: ValidationResult | None = None,
        skip_validate: bool = False,
        confidence: float = 0.9,
    ) -> ResolveResult:
        if skip_validate:
            text = gen_result.text.strip()
            return ResolveResult(
                command=text,
                raw_command=text,
                tier=3,
                confidence=confidence,
                tokens_in=gen_result.tokens_in,
                tokens_out=gen_result.tokens_out,
                cached_tokens=gen_result.cached_tokens,
                model=gen_result.model,
                provider_name=gen_result.provider,
            )

        if validation is None:
            validation = validate(gen_result.text, danger_scan=self.danger_scan)

        return ResolveResult(
            command=validation.safe_command,
            raw_command=gen_result.text,
            tier=3,
            confidence=confidence,
            tokens_in=gen_result.tokens_in,
            tokens_out=gen_result.tokens_out,
            cached_tokens=gen_result.cached_tokens,
            model=gen_result.model,
            provider_name=gen_result.provider,
            validation=validation,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _redact(self, text: str) -> str:
        return redact_secrets(text) if self.redact else text

    def _query_command_alignment(self, query_vec: np.ndarray, command: str) -> float:
        """Cosine similarity between the query embedding and the command text."""
        try:
            cmd_vec = self.embedder.embed(command, self.embedding_model)
        except Exception as exc:
            log.debug("Command embedding failed for alignment check: %s", exc)
            return 0.0
        return float(np.dot(query_vec.flatten(), cmd_vec.flatten()))

    def _is_rejected_pair(
        self,
        query_vec: np.ndarray,
        command: str,
        ctx_hash: str | None,
    ) -> bool:
        """Return True when this query is close to a pair the user rejected."""
        try:
            self.store.rejection_prune_older_than(REJECTION_TTL_SECONDS)
            rows = self.store.rejection_list_recent(REJECTION_TTL_SECONDS, ctx_hash)
        except Exception as exc:
            log.debug("Rejected-cache lookup failed: %s", exc)
            return False

        for row in rows:
            if not commands_match(command, row["rejected_command"]):
                continue
            rejected_vec = blob_to_vec(
                row["embedding"], expected_dim=row.get("embedding_dim")
            )
            if rejected_vec is None:
                continue
            score = float(np.dot(query_vec.flatten(), rejected_vec.flatten()))
            if score >= self.similarity_threshold:
                return True
        return False

    def _build_user_message(
        self, query: str, context: ShellContext, history_hint: str | None, op: str
    ) -> str:
        parts: list[str] = []

        ctx_str = context.to_prompt_string(redact=self.redact)
        if ctx_str:
            parts.append(f"Context: {ctx_str}")

        if history_hint:
            parts.append(f"Relevant history: {self._redact(history_hint)}")

        safe_query = self._redact(query)
        safe_buffer = self._redact(context.buffer) if context.buffer else ""

        if op == "explain":
            parts.append(f"Explain this command concisely: {safe_buffer}")
        elif op == "fix_last":
            parts.append(
                f"The last command failed (exit {context.last_exit_code}): {safe_buffer}\n"
                f"Intent: {safe_query}"
            )
        else:
            parts.append(f"Intent: {safe_query}")

        return "\n".join(parts)

    def promote_from_execution(
        self,
        executed_command: str,
        exit_code: int,
        *,
        session_query: str | None = None,
        session_suggestion: str | None = None,
        session_ts: float | None = None,
    ) -> bool:
        """Promote a pending LLM suggestion after the user runs it; prune on reject."""
        executed = executed_command.strip()
        if not executed or not is_likely_shell_command(executed):
            return False
        executed = normalize_command_for_match(executed)

        try:
            if session_query and session_suggestion:
                promoted = self._promote_from_session(
                    session_query, session_suggestion, executed, exit_code, session_ts
                )
                if promoted is not None:
                    return promoted

            if exit_code != 0:
                demoted = self._demote_command(executed)
                if demoted:
                    log.debug("Demoted %d cache row(s) after failed command", demoted)

            self.store.pending_prune_older_than(PENDING_TTL_SECONDS)
            self.store.rejection_prune_older_than(REJECTION_TTL_SECONDS)
            pending = self.store.pending_list_recent(PENDING_TTL_SECONDS)
            if not pending:
                return False

            now = time.time()
            for row in pending:
                if not commands_match(executed, row["suggested_command"]):
                    continue

                self.store.pending_delete(row["id"])
                if exit_code != 0:
                    self._reject_pending(row)
                    log.debug("Pending cache rejected: command failed (exit %d)", exit_code)
                    return False

                query_vec = self._pending_query_vec(row)
                if query_vec is None:
                    log.warning("Pending promotion skipped: bad embedding for %r", row["query"][:60])
                    return False

                # Light edit: if the executed command differs from the suggestion,
                # drop the stale suggested command so it is not served again.
                suggested = normalize_command_for_match(row["suggested_command"])
                if suggested != executed:
                    self._demote_command(suggested)

                self._promote_proven(
                    row["query_norm"],
                    row["query"],
                    query_vec,
                    executed,
                    row.get("context_hash"),
                    row.get("provider") or "",
                    row.get("model") or "",
                )
                log.info("Promoted proven cache for query %r", row["query"][:60])
                return True

            # Only the most-recent suggestion is the one the user actually saw;
            # leave older pending rows to age out so we don't over-reject.
            recent = [r for r in pending if now - r["created_at"] <= REJECTION_WINDOW_SECONDS]
            if recent:
                newest = recent[0]
                if exit_code == 0:
                    query_vec = self._pending_query_vec(newest)
                    if query_vec is not None:
                        align = self._query_command_alignment(query_vec, executed)
                        if align >= self.alignment_threshold:
                            self._reject_pending(newest)
                            self.store.pending_delete(newest["id"])
                            self._promote_proven(
                                newest["query_norm"],
                                newest["query"],
                                query_vec,
                                executed,
                                newest.get("context_hash"),
                                newest.get("provider") or "",
                                newest.get("model") or "",
                            )
                            log.info(
                                "Promoted edited command for query %r",
                                newest["query"][:60],
                            )
                            return True

                self._reject_pending(newest)
                self.store.pending_delete(newest["id"])
                log.debug("Rejected suggestion for query %r", newest["query"][:60])
        except Exception as exc:
            log.warning("Pending cache promotion failed: %s", exc)
        return False

    def _pending_query_vec(self, row: dict) -> np.ndarray | None:
        query_vec = blob_to_vec(row["embedding"], expected_dim=row.get("embedding_dim"))
        if query_vec is None:
            query_vec = blob_to_vec(row["embedding"])
        return query_vec

    def _promote_from_session(
        self,
        session_query: str,
        session_suggestion: str,
        executed: str,
        exit_code: int,
        session_ts: float | None = None,
    ) -> bool | None:
        """Explicit Ctrl+K session link from the shell widget."""
        if session_ts and time.time() - session_ts > REJECTION_WINDOW_SECONDS:
            return None
        norm = _normalize(session_query)
        suggested = normalize_command_for_match(session_suggestion)
        self.store.pending_delete_for_query(norm)

        if exit_code != 0:
            self._reject_session_pair(session_query, session_suggestion)
            self._demote_command(suggested)
            return False

        try:
            query_vec = self.embedder.embed(session_query, self.embedding_model)
        except Exception as exc:
            log.debug("Session promotion skipped: embedding failed: %s", exc)
            return None

        align = self._query_command_alignment(query_vec, executed)
        related = align >= self.alignment_threshold or commands_match(executed, suggested)
        if not related:
            log.debug(
                "Session rejected unrelated command for %r: %r (align=%.3f)",
                session_query[:60],
                executed[:60],
                align,
            )
            self._reject_session_pair(session_query, session_suggestion)
            self._demote_command(suggested)
            return False

        if suggested != executed:
            self._demote_command(suggested)
            self.store.rejection_put(norm, session_query, query_vec, suggested, None)

        self._promote_proven(norm, session_query, query_vec, executed, None, "", "")
        log.info("Promoted Ctrl+K session for query %r", session_query[:60])
        return True

    def _reject_session_pair(self, session_query: str, session_suggestion: str) -> None:
        norm = _normalize(session_query)
        try:
            query_vec = self.embedder.embed(session_query, self.embedding_model)
        except Exception:
            return
        self.store.rejection_put(
            norm,
            session_query,
            query_vec,
            normalize_command_for_match(session_suggestion),
            None,
        )

    def _reject_pending(self, row: dict) -> None:
        query_vec = self._pending_query_vec(row)
        if query_vec is None:
            return
        self.store.rejection_put(
            row["query_norm"],
            row["query"],
            query_vec,
            normalize_command_for_match(row["suggested_command"]),
            row.get("context_hash"),
        )
        self.store.exact_delete(
            row["query_norm"],
            normalize_command_for_match(row["suggested_command"]),
            row.get("context_hash"),
        )
        self.store.semantic_delete_command(normalize_command_for_match(row["suggested_command"]))

    def _demote_command(self, command: str) -> int:
        return self.store.exact_delete_command(command) + self.store.semantic_delete_command(command)

    def _promote_proven(
        self,
        norm: str,
        query: str,
        query_vec: np.ndarray,
        command: str,
        ctx_hash: str | None,
        provider: str,
        model: str,
    ) -> None:
        """Write user-proven mappings to exact + semantic cache."""
        self.store.exact_put(norm, command, ctx_hash)
        align = self._query_command_alignment(query_vec, command)
        if align >= self.alignment_threshold:
            self.store.semantic_put(
                query,
                query_vec,
                command,
                ctx_hash,
                provider,
                model,
                provenance="proven",
            )
        else:
            log.debug(
                "Promoted exact only: low alignment (%.3f) for %r",
                align,
                query[:60],
            )

    def _queue_pending(
        self,
        norm: str,
        query: str,
        query_vec: np.ndarray | None,
        command: str,
        ctx_hash: str | None,
        provider: str,
        model: str,
    ) -> None:
        """Queue an LLM suggestion for promotion after the user executes it."""
        vec = query_vec
        if vec is None:
            try:
                vec = self.embedder.embed(query, self.embedding_model)
            except Exception as exc:
                log.debug("Pending queue skipped: embedding failed: %s", exc)
                return
        try:
            self.store.pending_put(norm, query, vec, command, ctx_hash, provider, model)
        except Exception as exc:
            log.warning("Pending cache queue failed: %s", exc)

    def _log_telemetry(
        self, op: str, tier: int, tokens_in: int, tokens_out: int, t0: float
    ) -> None:
        if not self.telemetry_enabled:
            return
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            self.store.telemetry_log(op, tier, tokens_in, tokens_out, latency_ms)
        except Exception as exc:
            log.debug("Telemetry log failed: %s", exc)
