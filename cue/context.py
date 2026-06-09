"""Context capture and secret redaction.

Captures: CWD, git branch, last exit code, shell, OS.
Redacts: API keys, tokens, passwords, .env-style values before any LLM call.

Context is compressed to a single-line string injected into the Tier-3 prompt.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret redaction patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Generic API key / token / secret patterns
    ("api_key", re.compile(r'\b([A-Za-z0-9_-]{20,})\b', re.ASCII)),
    # sk-... OpenAI style
    ("openai_key", re.compile(r'\bsk-[A-Za-z0-9]{20,}\b')),
    # Bearer tokens
    ("bearer_token", re.compile(r'\bBearer\s+\S{10,}\b', re.IGNORECASE)),
    # AWS-style keys
    ("aws_key", re.compile(r'\bAKIA[A-Z0-9]{16}\b')),
    # Generic hex secrets (32+ hex chars)
    ("hex_secret", re.compile(r'\b[0-9a-fA-F]{32,}\b')),
    # .env VALUE patterns  KEY=VALUE where value looks secret-ish
    ("env_value", re.compile(r'(?:PASSWORD|SECRET|TOKEN|KEY|CREDENTIAL)=[^\s]{6,}', re.IGNORECASE)),
]

# Short, common words that happen to be hex — don't redact these
_HEX_ALLOWLIST = frozenset(["deadbeef", "cafebabe", "00000000", "ffffffff"])


def redact_secrets(text: str) -> str:
    """Scrub obvious secrets from a string before sending to an LLM provider."""
    # Remove .env-style KEY=value pairs
    text = _SECRET_PATTERNS[5][1].sub(lambda m: m.group(0).split("=")[0] + "=[REDACTED]", text)

    # Redact Bearer tokens
    text = _SECRET_PATTERNS[2][1].sub("Bearer [REDACTED]", text)

    # Redact sk-... keys
    text = _SECRET_PATTERNS[1][1].sub("[REDACTED_KEY]", text)

    # Redact AWS keys
    text = _SECRET_PATTERNS[3][1].sub("[REDACTED_AWS_KEY]", text)

    # Redact long hex strings (possible hashes/secrets) — but not short ones
    def _maybe_redact_hex(m: re.Match) -> str:
        val = m.group(0)
        if val.lower() in _HEX_ALLOWLIST or len(val) < 32:
            return val
        return "[REDACTED_HEX]"

    text = _SECRET_PATTERNS[4][1].sub(_maybe_redact_hex, text)

    return text


# ---------------------------------------------------------------------------
# Context capture
# ---------------------------------------------------------------------------

@dataclass
class ShellContext:
    """Captured shell context sent with every query."""
    cwd: str = ""
    git_branch: str = ""
    git_remote: str = ""
    last_exit_code: int = 0
    shell: str = ""
    os_name: str = ""
    # These come from the client (widget), not the daemon
    buffer: str = ""          # Current ZLE buffer (for explain/fix ops)
    query: str = ""           # The user's natural language query

    # Derived
    project_root: str = ""    # Git repo root, or CWD if not in a repo

    def context_bucket_hash(self) -> str:
        """Hash of (project_root, git_remote) — used for context-sensitivity guard."""
        key = f"{self.project_root}|{self.git_remote}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def to_prompt_string(self, *, redact: bool = True) -> str:
        """Compact one-line context for the dynamic LLM suffix."""
        parts: list[str] = []
        if self.cwd:
            cwd = self.cwd
            if redact:
                cwd = redact_secrets(cwd)
            parts.append(f"cwd:{cwd}")
        if self.git_branch:
            parts.append(f"branch:{self.git_branch}")
        if self.last_exit_code != 0:
            parts.append(f"exit:{self.last_exit_code}")
        if self.os_name:
            parts.append(f"os:{self.os_name}")
        if self.shell:
            parts.append(f"shell:{self.shell}")
        return " | ".join(parts)


def capture_local() -> ShellContext:
    """Capture context from the current process environment.

    In daemon mode the context comes from the client request, not here.
    This is for CLI / testing use.
    """
    ctx = ShellContext()
    ctx.cwd = os.getcwd()
    ctx.shell = os.environ.get("SHELL", "")
    ctx.os_name = platform.system().lower()
    ctx.last_exit_code = 0  # Can't know after-the-fact in daemon context

    # Git context
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        ctx.git_branch = branch
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        ctx.git_remote = remote
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        ctx.project_root = root
    except (subprocess.CalledProcessError, FileNotFoundError):
        ctx.project_root = ctx.cwd

    return ctx


def from_client_payload(payload: dict) -> ShellContext:
    """Deserialize context from the JSON payload sent by the shell widget."""
    ctx = ShellContext()
    ctx.cwd = payload.get("cwd", "")
    ctx.git_branch = payload.get("git_branch", "")
    ctx.git_remote = payload.get("git_remote", "")
    ctx.last_exit_code = int(payload.get("last_exit_code", 0))
    ctx.shell = payload.get("shell", "zsh")
    ctx.os_name = payload.get("os", platform.system().lower())
    ctx.buffer = payload.get("buffer", "")
    ctx.query = payload.get("query", "")
    ctx.project_root = payload.get("project_root", ctx.cwd)
    return ctx


# ---------------------------------------------------------------------------
# Deictic / context-sensitive query detection
# ---------------------------------------------------------------------------

_DEICTIC_WORDS = re.compile(
    r'\b(this|that|here|the last|the current|my|our|the same|it|its|them|these|those)\b',
    re.IGNORECASE,
)

_CONCRETE_NOUNS = re.compile(
    r'\b(file|files|directory|dir|folder|command|git|docker|process|port|server|database|'
    r'repo|branch|commit|log|log file|service|container|image|table|function|script|package)\b',
    re.IGNORECASE,
)


def is_context_sensitive(query: str) -> bool:
    """Return True if query likely depends on the current working directory/project.

    Used to decide whether Tier-1/2 hits need a matching context bucket.
    """
    has_deictic = bool(_DEICTIC_WORDS.search(query))
    has_concrete_noun = bool(_CONCRETE_NOUNS.search(query))
    # Context-sensitive if deictic without a concrete anchor noun
    return has_deictic and not has_concrete_noun
