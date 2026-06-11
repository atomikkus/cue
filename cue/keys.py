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


def keyring_available() -> bool:
    """Return True when a real OS keyring backend is available (not WSL/headless fail)."""
    try:
        import keyring  # type: ignore[import]
        from keyring.backends.fail import FailKeyring  # type: ignore[import]

        return not isinstance(keyring.get_keyring(), FailKeyring)
    except Exception:
        return False


def keyring_get(provider: str) -> str | None:
    if not keyring_available():
        return None
    try:
        import keyring  # type: ignore[import]

        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:
        return None


def keyring_set(provider: str, api_key: str) -> None:
    import keyring  # type: ignore[import]

    keyring.set_password(KEYRING_SERVICE, provider, api_key)


def keyring_delete(provider: str) -> bool:
    if not keyring_available():
        return False
    try:
        import keyring  # type: ignore[import]

        keyring.delete_password(KEYRING_SERVICE, provider)
        return True
    except Exception:
        return False


def set_config_key(provider: str, api_key: str) -> None:
    from cue.config_io import set_config_value  # noqa: PLC0415

    set_config_value(f"providers.{provider}.key", api_key)


def clear_config_key(provider: str) -> bool:
    from cue.config_io import load_raw, save_raw, set_nested  # noqa: PLC0415

    raw = load_raw()
    providers = raw.get("providers")
    if not isinstance(providers, dict):
        return False
    section = providers.get(provider)
    if not isinstance(section, dict) or not section.get("key"):
        return False
    set_nested(raw, f"providers.{provider}.key", "")
    save_raw(raw)
    return True


def save_api_key(provider: str, api_key: str) -> KeySource:
    """Store an API key in the OS keychain, or config.toml when keyring is unavailable."""
    if keyring_available():
        try:
            keyring_set(provider, api_key)
            return "keyring"
        except Exception:
            pass
    set_config_key(provider, api_key)
    return "config"


def delete_api_key(provider: str) -> bool:
    """Remove a stored API key from keychain and/or config.toml."""
    removed = keyring_delete(provider)
    return clear_config_key(provider) or removed


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
