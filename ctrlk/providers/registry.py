"""Provider registry — instantiates providers from config.

Key resolution order for every provider:
  1. CTRLK_<PROVIDER>_API_KEY environment variable
  2. Provider's canonical env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, …)
  3. The 'key' field in the config file
  4. OS keychain (optional, via keyring library)

Keys are never logged, never written to disk, never sent anywhere except
the provider's own HTTPS endpoint.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from .anthropic import AnthropicProvider
from .base import Provider
from .openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from ctrlk.config import Config, ProviderSpecificConfig

log = logging.getLogger(__name__)

# Canonical env var fallbacks per provider
_ENV_FALLBACKS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _resolve_key(provider_name: str, config_key: str) -> str:
    """Resolve API key using the documented priority order."""
    # 1. CTRLK-prefixed env var
    ctrlk_var = f"CTRLK_{provider_name.upper()}_API_KEY"
    if val := os.environ.get(ctrlk_var):
        return val

    # 2. Canonical provider env var
    if canonical := _ENV_FALLBACKS.get(provider_name):
        if val := os.environ.get(canonical):
            return val

    # 3. Config file value
    if config_key:
        return config_key

    # 4. OS keychain (best-effort)
    try:
        import keyring  # type: ignore[import]
        val = keyring.get_password("ctrlk", provider_name)
        if val:
            return val
    except Exception:
        pass

    return ""


def build_registry(config: "Config") -> dict[str, Provider]:
    """Instantiate all configured providers from config.

    Returns a dict mapping provider name → Provider instance.
    """
    providers: dict[str, Provider] = {}

    # Anthropic
    anthro_cfg = config.get_provider_config("anthropic")
    key = _resolve_key("anthropic", anthro_cfg.key)
    providers["anthropic"] = AnthropicProvider(api_key=key)

    # OpenAI
    oai_cfg = config.get_provider_config("openai")
    key = _resolve_key("openai", oai_cfg.key)
    providers["openai"] = OpenAICompatProvider(
        base_url="https://api.openai.com/v1",
        api_key=key,
        name="openai",
    )

    # Mistral
    mistral_cfg = config.get_provider_config("mistral")
    key = _resolve_key("mistral", mistral_cfg.key)
    providers["mistral"] = OpenAICompatProvider(
        base_url="https://api.mistral.ai/v1",
        api_key=key,
        name="mistral",
    )

    # OpenRouter
    or_cfg = config.get_provider_config("openrouter")
    key = _resolve_key("openrouter", or_cfg.key)
    extra_headers: dict[str, str] = {"X-Title": "ctrlk"}
    if or_cfg.referer:
        extra_headers["HTTP-Referer"] = or_cfg.referer
    providers["openrouter"] = OpenAICompatProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        name="openrouter",
        extra_headers=extra_headers,
    )

    # Custom (Ollama, vLLM, LocalAI, etc.)
    custom_cfg = config.get_provider_config("custom")
    if custom_cfg.base_url:
        key = _resolve_key("custom", custom_cfg.key)
        providers["custom"] = OpenAICompatProvider(
            base_url=custom_cfg.base_url,
            api_key=key,
            name=custom_cfg.name or "custom",
        )

    return providers


def get_provider(registry: dict[str, Provider], name: str) -> Provider | None:
    """Retrieve a provider by name; log a warning if not found."""
    provider = registry.get(name)
    if provider is None:
        log.warning("Provider '%s' not found in registry. Available: %s", name, list(registry))
    return provider
