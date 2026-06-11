"""Promote LLM suggestions to durable cache only after the user runs them.

Tier 3 queues a pending (query, command) pair. The shell precmd hook sends each
executed command back via index_cmd; we match against pending rows and promote
proven pairs to exact + semantic cache.
"""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher

log = logging.getLogger(__name__)

PENDING_TTL_SECONDS = 86_400
REJECTION_WINDOW_SECONDS = 300
REJECTION_TTL_SECONDS = 2_592_000
MATCH_THRESHOLD = 0.85

_MATCH_PREFIX_RE = re.compile(r"^⚠\s*")


def normalize_command_for_match(command: str) -> str:
    """Strip cue safety prefix and collapse whitespace for comparison."""
    cmd = command.strip()
    cmd = _MATCH_PREFIX_RE.sub("", cmd)
    return re.sub(r"\s+", " ", cmd).strip()


def commands_match(executed: str, suggested: str, *, threshold: float = MATCH_THRESHOLD) -> bool:
    """Return True if executed command is the same as (or a light edit of) suggested."""
    a = normalize_command_for_match(executed)
    b = normalize_command_for_match(suggested)
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold
