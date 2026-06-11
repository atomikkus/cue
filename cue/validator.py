"""Command validation and safety layer.

INVARIANT: This module NEVER blocks a command from reaching the buffer.
Dangerous commands receive a visible ⚠ prefix so the user sees them before
pressing Enter. The decision to execute always belongs to the user.

BUFFER-ALWAYS: There is no code path in cue that calls zle accept-line or
any equivalent. Commands are placed in the ZLE buffer and nothing more.
"""

from __future__ import annotations

import re
import shlex
import shutil
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Danger patterns — regexes over the raw command string
# ---------------------------------------------------------------------------

_DANGER_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Wipe root / entire filesystem
    ("wipes filesystem", re.compile(r"\brm\b.*-[a-zA-Z]*r[a-zA-Z]*\s+/\s*$|\brm\b.*-[a-zA-Z]*r[a-zA-Z]*\s+/\s")),
    # dd writing to block device
    ("writes to block device", re.compile(r"\bdd\b.*\bof=/dev/")),
    # mkfs formatting a device
    ("formats block device", re.compile(r"\bmkfs\b")),
    # Fork bomb
    ("fork bomb", re.compile(r":\s*\(\s*\)\s*\{.*:\|:.*\}")),
    # curl/wget pipe to shell (code execution from remote)
    ("remote code execution risk", re.compile(r"\b(curl|wget)\b.*\|\s*(bash|sh|zsh|fish|python|ruby|perl|node)\b")),
    # Overwrite /etc/passwd or /etc/shadow
    ("overwrites sensitive system file", re.compile(r">\s*/etc/(passwd|shadow|sudoers)")),
    # Recursively delete everything under a common dangerous path
    ("recursive delete of important path", re.compile(r"\brm\b.*-[a-zA-Z]*r[a-zA-Z]*\s+~\s*$|\brm\b.*-[a-zA-Z]*r[a-zA-Z]*\s+~/")),
    # chmod 777 on filesystem root (not relative paths like ./foo)
    ("dangerous chmod on root", re.compile(r"\bchmod\b.*777\s+/(?:\s|$)")),
    # kill all processes
    ("kills all processes", re.compile(r"\bkillall\b\s+-9\s+init|\bkill\b\s+-9\s+-1")),
    # Wipe MBR/boot sector
    ("wipes boot sector", re.compile(r"\bdd\b.*\bof=/dev/(sda|hda|vda|nvme)\b[^p]")),
]

# Characters that mark a command as potentially dangerous even without pattern match
_DANGER_KEYWORDS = frozenset([":(){ :|:& };:", "> /dev/sda"])


_META_BINARIES = frozenset({"cue", "cue-daemon"})

_SHELL_SIGNAL_RE = re.compile(
    r"(\./|\.\./|/|"
    r"-\w|"
    r"\||>|;|"
    r"\*|`|"
    r"\$\(|\$\{)"
)


def is_likely_shell_command(command: str) -> bool:
    """Heuristic: distinguish real shell commands from natural-language history lines.

    Tier 2 must not surface chatty lines like "find all files with pdf?" even when
    embeddings match the user's intent.
    """
    command = command.strip()
    if not command or command.endswith("?"):
        return False
    if command.startswith("#"):
        return False

    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    if not tokens:
        return False

    leading = tokens[0].split("/")[-1]
    if leading in _META_BINARIES:
        return False
    if len(tokens) == 1:
        return shutil.which(leading) is not None or tokens[0].startswith("/")

    if _SHELL_SIGNAL_RE.search(command):
        return True

    # e.g. find . -name ...
    if len(tokens) >= 2 and tokens[1] == ".":
        return True

    if len(tokens) == 2 and shutil.which(leading) is not None:
        return True

    return False


@dataclass
class ValidationResult:
    """Result of validating a single command string."""
    command: str             # Original command (without ⚠ prefix)
    safe_command: str        # Command to place in buffer (may have ⚠ prefix)
    is_valid: bool           # False only if unparseable — command still goes to buffer
    is_dangerous: bool       # True if any danger pattern matched
    danger_reason: str       # Human-readable reason if dangerous
    binary_found: bool       # Whether the leading binary is on $PATH
    parse_error: str | None  # shlex error message, if any


def validate(command: str, *, danger_scan: bool = True) -> ValidationResult:
    """Validate a generated command.

    Always returns a result with safe_command set — the caller places
    safe_command into the ZLE buffer unconditionally.

    ⚠ prefix is added for dangerous commands; the user sees it before Enter.
    """
    command = command.strip()

    # --- Parse check ---
    parse_error: str | None = None
    tokens: list[str] = []
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        parse_error = str(exc)

    is_valid = parse_error is None

    # --- Binary existence check ---
    binary_found = False
    if tokens:
        leading = tokens[0]
        # Handle env/sudo/time wrappers
        _skip = {"env", "sudo", "time", "nice", "nohup", "strace", "ltrace"}
        idx = 0
        while idx < len(tokens) and tokens[idx] in _skip:
            idx += 1
        if idx < len(tokens):
            binary = tokens[idx]
            # Strip path prefix for absolute binaries
            binary_name = binary.split("/")[-1] if "/" in binary else binary
            binary_found = shutil.which(binary_name) is not None or binary.startswith("/")

    # --- Danger scan ---
    is_dangerous = False
    danger_reason = ""

    if danger_scan:
        for reason, pattern in _DANGER_PATTERNS:
            if pattern.search(command):
                is_dangerous = True
                danger_reason = reason
                break
        if not is_dangerous:
            for kw in _DANGER_KEYWORDS:
                if kw in command:
                    is_dangerous = True
                    danger_reason = "matches known dangerous pattern"
                    break

    # --- Build buffer command ---
    # INVARIANT: command always goes to the buffer. Dangerous = ⚠ prefix only.
    if is_dangerous:
        safe_command = f"⚠ {command}"
    else:
        safe_command = command

    return ValidationResult(
        command=command,
        safe_command=safe_command,
        is_valid=is_valid,
        is_dangerous=is_dangerous,
        danger_reason=danger_reason,
        binary_found=binary_found,
        parse_error=parse_error,
    )
