"""Provider protocol and shared data types.

Every provider must satisfy the Provider protocol:

    generate(system, few_shot, user, *, model, max_tokens, stop, stream) -> GenResult

Static system + few_shot constitute the cacheable prefix; user is the dynamic suffix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class GenResult:
    """Returned by every provider's generate() call."""
    text: str                  # The generated command (stripped of whitespace)
    tokens_in: int             # Input tokens consumed
    tokens_out: int            # Output tokens generated
    cached_tokens: int         # Tokens served from provider-side cache (0 if unsupported)
    model: str                 # Model identifier as reported by the provider
    provider: str              # Provider name (e.g. "anthropic", "openrouter")
    error: str | None = None   # Non-None means generation failed; text may be empty


@dataclass
class FewShotExample:
    """A single NL→command example used in the few-shot prefix."""
    user: str
    assistant: str


# Curated few-shot examples injected as the static cacheable prefix.
# These are chosen for diversity and to establish the command-only output style.
DEFAULT_FEW_SHOT: list[FewShotExample] = [
    FewShotExample("list all python files recursively", "find . -name '*.py' -type f"),
    FewShotExample("show disk usage for each directory here", "du -sh */ | sort -h"),
    FewShotExample("kill the process on port 8080", "lsof -ti tcp:8080 | xargs kill -9"),
    FewShotExample("compress this directory to a tar.gz", "tar -czf archive.tar.gz ."),
    FewShotExample("show last 50 lines of a log file", "tail -n 50 logfile.log"),
    FewShotExample("git show what changed in last commit", "git show --stat HEAD"),
    FewShotExample("find files larger than 100MB", "find . -type f -size +100M"),
    FewShotExample("count lines in all python files", "find . -name '*.py' | xargs wc -l | tail -1"),
]

SYSTEM_PROMPT = """\
You are a shell command generator. Given a natural language description, output ONLY \
the exact shell command to run — no explanation, no markdown, no prose, no trailing newline.

Rules:
- Output a single command or pipeline. Nothing else.
- Prefer portable POSIX commands unless context implies macOS or Linux specifics.
- When given context (CWD, git branch, last exit code, recent history), use it.
- If a command would be destructive, output it anyway — the user will review before running.
"""


@runtime_checkable
class Provider(Protocol):
    """Protocol that every provider must satisfy."""
    name: str
    supports_prompt_caching: bool

    def generate(
        self,
        system: str,
        few_shot: list[dict],   # [{"role": "user"|"assistant", "content": str}, ...]
        user: str,              # dynamic suffix: context + query
        *,
        model: str,
        max_tokens: int = 100,
        stop: list[str] | None = None,
        stream: bool = False,
    ) -> GenResult:
        ...


def few_shot_to_messages(examples: list[FewShotExample]) -> list[dict]:
    """Convert FewShotExample list to the message dict format used by providers."""
    messages = []
    for ex in examples:
        messages.append({"role": "user", "content": ex.user})
        messages.append({"role": "assistant", "content": ex.assistant})
    return messages
