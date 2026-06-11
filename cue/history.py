"""Shell history ingestion and incremental indexing.

On first run: parse ~/.zsh_history and/or ~/.bash_history, dedupe,
embed in batches, write to history_index.

Incremental mode: called from the shell's precmd/PROMPT_COMMAND hook to index new commands.

History is private — never transmitted; stored locally only.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cue.embedder import embed_batch
    from cue.store import Store

log = logging.getLogger(__name__)

_ZSH_HISTORY_PATH = Path("~/.zsh_history").expanduser()
_BASH_HISTORY_PATH = Path("~/.bash_history").expanduser()

_ZSH_EXTENDED_RE = re.compile(r"^:\s*\d+:\d+;(.+)$")

_IGNORE_PREFIXES = frozenset([
    "cd ", "ls", "pwd", "exit", "clear", "history", "man ",
    "#",
])
_IGNORE_EXACT = frozenset(["ls", "pwd", "exit", "clear", "q", "quit", ""])


def _parse_zsh_history(path: Path) -> list[str]:
    """Parse ~/.zsh_history (with or without EXTENDED_HISTORY format)."""
    commands: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return commands

    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        while line.endswith("\\") and i + 1 < len(lines):
            i += 1
            line = line[:-1] + "\n" + lines[i]
        i += 1

        m = _ZSH_EXTENDED_RE.match(line)
        if m:
            cmd = m.group(1).strip()
        else:
            cmd = line.strip()

        if _should_index(cmd):
            commands.append(cmd)

    return commands


def _parse_bash_history(path: Path) -> list[str]:
    """Parse ~/.bash_history (simple line-per-command format)."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []
    return [line.strip() for line in lines if _should_index(line.strip())]


def _should_index(cmd: str) -> bool:
    """Return True if the command is worth embedding."""
    if not cmd or cmd in _IGNORE_EXACT:
        return False
    if any(cmd.startswith(prefix) for prefix in _IGNORE_PREFIXES):
        return False
    if len(cmd) < 4:
        return False
    return True


def _dedupe(commands: list[str]) -> list[str]:
    """Deduplicate while preserving last-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for cmd in reversed(commands):
        if cmd not in seen:
            seen.add(cmd)
            result.append(cmd)
    return list(reversed(result))


def _auto_history_order() -> list[str]:
    """Prefer active shell history, then fall back to the other."""
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    if shell == "bash":
        return ["bash", "zsh"]
    return ["zsh", "bash"]


def _load_commands(source: str) -> tuple[list[str], str]:
    if source == "bash":
        return _parse_bash_history(_BASH_HISTORY_PATH), "bash_history"
    return _parse_zsh_history(_ZSH_HISTORY_PATH), "zsh_history"


def ingest_history(
    store: "Store",
    embed_fn: "embed_batch",
    model_name: str = "all-MiniLM-L6-v2",
    source: str = "auto",
    *,
    force: bool = False,
) -> int:
    """Full history ingestion (called on daemon startup or --reindex).

    Returns the number of new commands indexed.
    """
    if source == "auto":
        total = 0
        for src in _auto_history_order():
            total += _ingest_single_source(store, embed_fn, model_name, src, force=force)
        return total
    return _ingest_single_source(store, embed_fn, model_name, source, force=force)


def _ingest_single_source(
    store: "Store",
    embed_fn: "embed_batch",
    model_name: str,
    source: str,
    *,
    force: bool,
) -> int:
    raw, src_label = _load_commands(source)
    all_commands = _dedupe(raw)

    if not all_commands:
        log.info("No history found for source=%s", source)
        return 0

    if not force:
        known = store.history_known_commands()
        new_commands = [c for c in all_commands if c not in known]
    else:
        new_commands = all_commands

    if not new_commands:
        log.info("All %d history commands already indexed for source=%s.", len(all_commands), source)
        return 0

    log.info("Embedding %d new history commands from %s...", len(new_commands), source)
    batch_size = 256
    total_indexed = 0

    for offset in range(0, len(new_commands), batch_size):
        batch = new_commands[offset : offset + batch_size]
        embeddings = embed_fn(batch, model_name)
        entries = list(zip(batch, embeddings, [src_label] * len(batch)))
        store.history_put_batch(entries)
        total_indexed += len(batch)
        log.debug("Indexed batch %d/%d", offset + len(batch), len(new_commands))

    log.info("History ingestion complete: %d commands indexed from %s.", total_indexed, source)
    return total_indexed


def index_single_command(
    command: str,
    store: "Store",
    embed_fn,
    model_name: str = "all-MiniLM-L6-v2",
    source: str | None = None,
) -> None:
    """Index a single command (called from shell hook via daemon)."""
    if not _should_index(command):
        return
    src_label = source or default_index_source()
    try:
        vec = embed_fn(command, model_name)
        store.history_put(command, vec, src_label)
    except Exception as exc:
        log.debug("Failed to index command '%s': %s", command[:50], exc)


def default_index_source() -> str:
    """Label for incremental indexing based on active shell."""
    shell = Path(os.environ.get("SHELL", "")).name.lower()
    return "bash_history" if shell == "bash" else "zsh_history"
