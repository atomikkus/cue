"""Configuration loader with TOML schema validation and SIGHUP reload.

All daemon settings live in ~/.config/cue/config.toml.
The shell widget reads the keybindings section at startup.
The daemon reads the rest at launch and reloads on SIGHUP.
"""

from __future__ import annotations

import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path(os.environ.get("CUE_CONFIG_DIR", "~/.config/cue")).expanduser()
CONFIG_PATH = CONFIG_DIR / "config.toml"

# ---------------------------------------------------------------------------
# Default config as TOML text (written on first run)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_TOML = """\
# cue configuration
# See https://github.com/yourusername/cue for documentation

[keys]
generate = "^K"
explain  = "^E"
fix      = "^F"

[providers.primary]
provider   = "openrouter"
model      = "anthropic/claude-haiku-4-5"
max_tokens = 100

[providers.escalate]
provider   = "anthropic"
model      = "claude-sonnet-4-6"
max_tokens = 200

[providers.openrouter]
# key resolved from CUE_OPENROUTER_API_KEY or OPENROUTER_API_KEY
referer = ""

[providers.custom]
name     = "local-ollama"
base_url = "http://localhost:11434/v1"
model    = "qwen2.5-coder:1.5b"
key      = ""

[providers.anthropic]
# key resolved from CUE_ANTHROPIC_API_KEY or ANTHROPIC_API_KEY

[providers.openai]
# key resolved from CUE_OPENAI_API_KEY or OPENAI_API_KEY

[providers.mistral]
# key resolved from CUE_MISTRAL_API_KEY or MISTRAL_API_KEY

[cache]
similarity_threshold = 0.92
history_threshold    = 0.88
db_path = "~/.config/cue/cache.db"

[context]
include_git   = true
include_pwd   = true
include_exit  = true
history_lines = 1

[embeddings]
backend = "local"
model   = "all-MiniLM-L6-v2"

[safety]
danger_scan    = true
redact_secrets = true

[telemetry]
enabled = false
"""


# ---------------------------------------------------------------------------
# Dataclasses for typed config access
# ---------------------------------------------------------------------------

@dataclass
class KeysConfig:
    generate: str = "^K"
    explain: str = "^E"
    fix: str = "^F"


@dataclass
class ProviderSlotConfig:
    """Primary or escalate provider slot."""
    provider: str = "openrouter"
    model: str = "anthropic/claude-haiku-4-5"
    max_tokens: int = 100


@dataclass
class ProviderSpecificConfig:
    """Per-provider specific settings (keys, referer, etc.)."""
    key: str = ""
    referer: str = ""
    base_url: str = ""
    name: str = ""


@dataclass
class CacheConfig:
    similarity_threshold: float = 0.92
    history_threshold: float = 0.88
    db_path: str = "~/.config/cue/cache.db"

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser()


@dataclass
class ContextConfig:
    include_git: bool = True
    include_pwd: bool = True
    include_exit: bool = True
    history_lines: int = 1


@dataclass
class EmbeddingsConfig:
    backend: str = "local"
    model: str = "all-MiniLM-L6-v2"


@dataclass
class SafetyConfig:
    danger_scan: bool = True
    redact_secrets: bool = True


@dataclass
class TelemetryConfig:
    enabled: bool = False


@dataclass
class Config:
    keys: KeysConfig = field(default_factory=KeysConfig)
    primary: ProviderSlotConfig = field(default_factory=ProviderSlotConfig)
    escalate: ProviderSlotConfig = field(default_factory=lambda: ProviderSlotConfig(
        provider="anthropic", model="claude-sonnet-4-6", max_tokens=200
    ))
    providers: dict[str, ProviderSpecificConfig] = field(default_factory=dict)
    cache: CacheConfig = field(default_factory=CacheConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    def get_provider_config(self, provider_name: str) -> ProviderSpecificConfig:
        """Return the specific config for a named provider."""
        return self.providers.get(provider_name, ProviderSpecificConfig())


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _parse_raw(raw: dict[str, Any]) -> Config:
    """Convert raw TOML dict into a typed Config."""
    cfg = Config()

    if keys := raw.get("keys"):
        cfg.keys = KeysConfig(
            generate=keys.get("generate", "^K"),
            explain=keys.get("explain", "^E"),
            fix=keys.get("fix", "^F"),
        )

    providers_raw = raw.get("providers", {})
    if primary := providers_raw.get("primary"):
        cfg.primary = ProviderSlotConfig(
            provider=primary.get("provider", "openrouter"),
            model=primary.get("model", "anthropic/claude-haiku-4-5"),
            max_tokens=int(primary.get("max_tokens", 100)),
        )
    if escalate := providers_raw.get("escalate"):
        cfg.escalate = ProviderSlotConfig(
            provider=escalate.get("provider", "anthropic"),
            model=escalate.get("model", "claude-sonnet-4-6"),
            max_tokens=int(escalate.get("max_tokens", 200)),
        )

    # Per-provider specifics
    for pname in ("anthropic", "openai", "mistral", "openrouter", "custom"):
        if praw := providers_raw.get(pname):
            cfg.providers[pname] = ProviderSpecificConfig(
                key=praw.get("key", ""),
                referer=praw.get("referer", ""),
                base_url=praw.get("base_url", ""),
                name=praw.get("name", ""),
            )
        else:
            cfg.providers[pname] = ProviderSpecificConfig()

    if cache := raw.get("cache"):
        cfg.cache = CacheConfig(
            similarity_threshold=float(cache.get("similarity_threshold", 0.92)),
            history_threshold=float(cache.get("history_threshold", 0.88)),
            db_path=cache.get("db_path", "~/.config/cue/cache.db"),
        )

    if context := raw.get("context"):
        cfg.context = ContextConfig(
            include_git=bool(context.get("include_git", True)),
            include_pwd=bool(context.get("include_pwd", True)),
            include_exit=bool(context.get("include_exit", True)),
            history_lines=int(context.get("history_lines", 1)),
        )

    if embeddings := raw.get("embeddings"):
        cfg.embeddings = EmbeddingsConfig(
            backend=embeddings.get("backend", "local"),
            model=embeddings.get("model", "all-MiniLM-L6-v2"),
        )

    if safety := raw.get("safety"):
        cfg.safety = SafetyConfig(
            danger_scan=bool(safety.get("danger_scan", True)),
            redact_secrets=bool(safety.get("redact_secrets", True)),
        )

    if telemetry := raw.get("telemetry"):
        cfg.telemetry = TelemetryConfig(
            enabled=bool(telemetry.get("enabled", False)),
        )

    return cfg


def load(path: Path | None = None) -> Config:
    """Load config from disk, creating defaults if missing."""
    p = path or CONFIG_PATH
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")

    with open(p, "rb") as fh:
        raw = tomllib.load(fh)

    return _parse_raw(raw)


# ---------------------------------------------------------------------------
# Live reload (SIGHUP)
# ---------------------------------------------------------------------------

class ConfigManager:
    """Thread-safe config holder that reloads on SIGHUP."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._config: Config = load(path)
        self._install_sighup()

    def _install_sighup(self) -> None:
        try:
            signal.signal(signal.SIGHUP, self._handle_sighup)
        except (OSError, ValueError):
            # Not supported on Windows; ignore in non-main threads
            pass

    def _handle_sighup(self, _signum: int, _frame: Any) -> None:
        try:
            self._config = load(self._path)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Config reload failed: %s", exc)

    @property
    def config(self) -> Config:
        return self._config

    def reload(self) -> None:
        self._config = load(self._path)
