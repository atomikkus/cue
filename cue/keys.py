"""API key resolution and keychain helpers.

Resolution order (same as registry):
  1. CUE_<PROVIDER>_API_KEY
  2. Provider canonical env var
  3. config.toml key field
  4. OS keychain (service ``cue``, username = provider name)
"""

from __future__ import annotations

import os
from typing import Literal

from cue.config import CONFIG_PATH, load

KeySource = Literal["env_cue", "env", "config", "keyring", "none"]

KEYRING_SERVICE = "cue"

PROVIDER_NAMES = ("openrouter", "anthropic", "openai", "mistral", "custom")

_ENV_FALLBACKS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Suggested default models per provider (setup wizard hints)
DEFAULT_MODELS: dict[str, str] = {
    "openrouter": "anthropic/claude-haiku-4-5",
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4o-mini",
    "mistral": "mistral-small-latest",
    "custom": "qwen2.5-coder:1.5b",
}

ESCALATE_MODELS: dict[str, str] = {
    "openrouter": "anthropic/claude-sonnet-4-6",
    "anthropic": "claude-sonnet-4-6",
    "openai": "gpt-4o",
    "mistral": "mistral-large-latest",
    "custom": "qwen2.5-coder:7b",
}


def mask_key(key: str) -> str:
    """Return a redacted preview of an API key."""
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}…{key[-4:]}"


def keyring_get(provider: str) -> str | None:
    try:
        import keyring  # type: ignore[import]

        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:
        return None


def keyring_set(provider: str, api_key: str) -> None:
    import keyring  # type: ignore[import]

    keyring.set_password(KEYRING_SERVICE, provider, api_key)


def keyring_delete(provider: str) -> bool:
    try:
        import keyring  # type: ignore[import]

        keyring.delete_password(KEYRING_SERVICE, provider)
        return True
    except Exception:
        return False


def resolve_key(provider_name: str, config_key: str = "") -> str:
    """Resolve API key using the documented priority order."""
    cue_var = f"CUE_{provider_name.upper()}_API_KEY"
    if val := os.environ.get(cue_var):
        return val

    if canonical := _ENV_FALLBACKS.get(provider_name):
        if val := os.environ.get(canonical):
            return val

    if config_key:
        return config_key

    if val := keyring_get(provider_name):
        return val

    return ""


def key_status(provider_name: str) -> tuple[KeySource, str]:
    """Return (source, masked_key) for a provider."""
    cue_var = f"CUE_{provider_name.upper()}_API_KEY"
    if val := os.environ.get(cue_var):
        return "env_cue", mask_key(val)

    if canonical := _ENV_FALLBACKS.get(provider_name):
        if val := os.environ.get(canonical):
            return "env", mask_key(val)

    cfg = load()
    config_key = cfg.get_provider_config(provider_name).key
    if config_key:
        return "config", mask_key(config_key)

    if val := keyring_get(provider_name):
        return "keyring", mask_key(val)

    return "none", "(not set)"


def provider_needs_key(provider_name: str) -> bool:
    """Local/custom providers may not require an API key."""
    return provider_name != "custom"
