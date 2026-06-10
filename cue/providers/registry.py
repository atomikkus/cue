"""Provider registry — instantiates providers from config.

Key resolution order for every provider:
  1. CUE_<PROVIDER>_API_KEY environment variable
  2. Provider's canonical env var (ANTHROPIC_API_KEY, OPENAI_API_KEY, …)
  3. The 'key' field in the config file
  4. OS keychain (optional, via keyring library)

Keys are never logged, never written to disk, never sent anywhere except
the provider's own HTTPS endpoint.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cue.keys import resolve_key

from .anthropic import AnthropicProvider
from .base import Provider
from .openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from cue.config import Config

log = logging.getLogger(__name__)


def build_registry(config: "Config") -> dict[str, Provider]:
    """Instantiate all configured providers from config.

    Returns a dict mapping provider name → Provider instance.
    """
    providers: dict[str, Provider] = {}

    # Anthropic
    anthro_cfg = config.get_provider_config("anthropic")
    key = resolve_key("anthropic", anthro_cfg.key)
    providers["anthropic"] = AnthropicProvider(api_key=key)

    # OpenAI
    oai_cfg = config.get_provider_config("openai")
    key = resolve_key("openai", oai_cfg.key)
    providers["openai"] = OpenAICompatProvider(
        base_url="https://api.openai.com/v1",
        api_key=key,
        name="openai",
    )

    # Mistral
    mistral_cfg = config.get_provider_config("mistral")
    key = resolve_key("mistral", mistral_cfg.key)
    providers["mistral"] = OpenAICompatProvider(
        base_url="https://api.mistral.ai/v1",
        api_key=key,
        name="mistral",
    )

    # OpenRouter
    or_cfg = config.get_provider_config("openrouter")
    key = resolve_key("openrouter", or_cfg.key)
    extra_headers: dict[str, str] = {"X-Title": "cue"}
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
        key = resolve_key("custom", custom_cfg.key)
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
