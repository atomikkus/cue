"""Interactive setup and config/key CLI commands."""

from __future__ import annotations

import getpass
import subprocess
import sys

from cue.config_io import format_config_show, set_config_value
from cue.keys import (
    DEFAULT_MODELS,
    ESCALATE_MODELS,
    PROVIDER_NAMES,
    key_status,
    keyring_delete,
    keyring_set,
    provider_needs_key,
)

_PROVIDER_MENU = """
Providers:
  1) openrouter  — one key, many models (recommended)
  2) anthropic   — Claude direct
  3) openai      — GPT direct
  4) mistral     — Mistral direct
  5) custom      — local Ollama / vLLM (no API key)
"""


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return value or default


def _prompt_yes_no(label: str, *, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        ans = input(f"{label} ({hint}): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose_provider() -> str:
    print(_PROVIDER_MENU)
    choice = _prompt("Choose provider (1-5)", "1")
    mapping = {"1": "openrouter", "2": "anthropic", "3": "openai", "4": "mistral", "5": "custom"}
    provider = mapping.get(choice, choice)
    if provider not in PROVIDER_NAMES:
        print(f"Unknown provider: {provider}", file=sys.stderr)
        sys.exit(1)
    return provider


def _restart_daemon_hint() -> None:
    print("\nRestart the daemon so keys and models take effect:")
    print("  cue-daemon restart")


def _try_restart_daemon() -> None:
    if not _prompt_yes_no("Restart cue daemon now?", default=True):
        _restart_daemon_hint()
        return
    try:
        subprocess.run(["cue-daemon", "restart"], check=False, timeout=90)
        print("Daemon restarted.")
    except FileNotFoundError:
        print("cue-daemon not on PATH — run: cue-daemon restart")
    except subprocess.TimeoutExpired:
        print("Daemon restart timed out — check: cue-daemon health")


def run_setup() -> int:
    """Interactive wizard for provider, API key, and models."""
    print("cue setup")
    print("─" * 40)

    provider = _choose_provider()
    default_model = DEFAULT_MODELS[provider]
    default_escalate = ESCALATE_MODELS.get(provider, default_model)

    if provider_needs_key(provider):
        source, masked = key_status(provider)
        if source != "none":
            print(f"Existing key for {provider}: {masked} [{source}]")
            if not _prompt_yes_no("Replace API key?", default=False):
                pass
            else:
                api_key = getpass.getpass(f"API key for {provider}: ").strip()
                if api_key:
                    keyring_set(provider, api_key)
                    print(f"✓ Key saved to keychain (service: cue, account: {provider})")
        else:
            api_key = getpass.getpass(f"API key for {provider} (hidden): ").strip()
            if not api_key:
                print("No key entered — you can add one later with: cue key set", provider)
            else:
                keyring_set(provider, api_key)
                print(f"✓ Key saved to keychain")
    else:
        base_url = _prompt("Local API base URL", "http://localhost:11434/v1")
        set_config_value("providers.custom.base_url", base_url)
        set_config_value("providers.custom.name", "local-ollama")

    model = _prompt("Primary model", default_model)
    set_config_value("providers.primary.provider", provider)
    set_config_value("providers.primary.model", model)

    if _prompt_yes_no("Configure escalation model?", default=True):
        esc_provider = _prompt("Escalate provider", provider)
        esc_model = _prompt("Escalate model", ESCALATE_MODELS.get(esc_provider, default_escalate))
        set_config_value("providers.escalate.provider", esc_provider)
        set_config_value("providers.escalate.model", esc_model)

    print("\n✓ Configuration saved to ~/.config/cue/config.toml")
    print()
    print(format_config_show())
    _try_restart_daemon()
    return 0


def run_config_show() -> int:
    print(format_config_show())
    return 0


def run_config_set(args: list[str]) -> int:
    if len(args) < 2:
        print("Usage: cue config set <dotted.path> <value>", file=sys.stderr)
        print("Example: cue config set providers.primary.model anthropic/claude-haiku-4-5", file=sys.stderr)
        return 1
    path, value = args[0], args[1]
    dest = set_config_value(path, value)
    print(f"Updated {path} = {value}")
    print(f"Written to {dest}")
    _restart_daemon_hint()
    return 0


def run_key_list() -> int:
    print("cue keys")
    print("─" * 40)
    for name in PROVIDER_NAMES:
        source, masked = key_status(name)
        source_label = {
            "env_cue": "environment (CUE_*)",
            "env": "environment",
            "config": "config.toml (consider moving to keychain)",
            "keyring": "keychain",
            "none": "not set",
        }[source]
        print(f"  {name:12} {masked:16}  {source_label}")
    return 0


def run_key_set(args: list[str]) -> int:
    if not args:
        print("Usage: cue key set <provider>", file=sys.stderr)
        print(f"Providers: {', '.join(PROVIDER_NAMES)}", file=sys.stderr)
        return 1
    provider = args[0].lower()
    if provider not in PROVIDER_NAMES:
        print(f"Unknown provider: {provider}", file=sys.stderr)
        return 1
    api_key = getpass.getpass(f"API key for {provider} (hidden): ").strip()
    if not api_key:
        print("Aborted — empty key.", file=sys.stderr)
        return 1
    keyring_set(provider, api_key)
    print(f"✓ Key saved to keychain for {provider}")
    _restart_daemon_hint()
    return 0


def run_key_delete(args: list[str]) -> int:
    if not args:
        print("Usage: cue key delete <provider>", file=sys.stderr)
        return 1
    provider = args[0].lower()
    if provider not in PROVIDER_NAMES:
        print(f"Unknown provider: {provider}", file=sys.stderr)
        return 1
    if keyring_delete(provider):
        print(f"✓ Removed keychain entry for {provider}")
    else:
        print(f"No keychain entry for {provider} (or keyring unavailable)")
    return 0


def main_config(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: cue config show | cue config set <path> <value>")
        return 0 if not argv else 1
    sub = argv[0]
    if sub == "show":
        return run_config_show()
    if sub == "set":
        return run_config_set(argv[1:])
    print(f"Unknown config command: {sub}", file=sys.stderr)
    return 1


def main_key(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: cue key list | cue key set <provider> | cue key delete <provider>")
        return 0 if not argv else 1
    sub = argv[0]
    if sub == "list":
        return run_key_list()
    if sub == "set":
        return run_key_set(argv[1:])
    if sub in ("delete", "rm", "unset"):
        return run_key_delete(argv[1:])
    print(f"Unknown key command: {sub}", file=sys.stderr)
    return 1
