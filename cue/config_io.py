"""Read/write config.toml as a nested dict (for cue config / cue setup)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import tomli_w

from cue.config import CONFIG_PATH, DEFAULT_CONFIG_TOML, load

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]


def load_raw(path: Path | None = None) -> dict[str, Any]:
    p = path or CONFIG_PATH
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    with open(p, "rb") as fh:
        return tomllib.load(fh)


def save_raw(raw: dict[str, Any], path: Path | None = None) -> Path:
    p = path or CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tomli_w.dumps(raw), encoding="utf-8")
    return p


def _parse_path(dotted: str) -> list[str]:
    return [p.strip() for p in dotted.split(".") if p.strip()]


def get_nested(raw: dict[str, Any], dotted: str) -> Any:
    node: Any = raw
    for part in _parse_path(dotted):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(f"Unknown config path: {dotted}")
        node = node[part]
    return node


def set_nested(raw: dict[str, Any], dotted: str, value: Any) -> None:
    parts = _parse_path(dotted)
    if not parts:
        raise ValueError("Empty config path")
    node = raw
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def set_config_value(dotted: str, value: Any, path: Path | None = None) -> Path:
    """Update a single config key and write config.toml."""
    raw = load_raw(path)
    # Coerce common numeric fields
    if dotted.endswith("max_tokens"):
        value = int(value)
    set_nested(raw, dotted, value)
    return save_raw(raw, path)


def format_config_show() -> str:
    """Human-readable config summary with redacted secrets."""
    from cue.keys import key_status

    cfg = load()
    lines = [
        f"Config: {CONFIG_PATH}",
        "",
        "[providers.primary]",
        f"  provider   = {cfg.primary.provider}",
        f"  model      = {cfg.primary.model}",
        f"  max_tokens = {cfg.primary.max_tokens}",
        "",
        "[providers.escalate]",
        f"  provider   = {cfg.escalate.provider}",
        f"  model      = {cfg.escalate.model}",
        f"  max_tokens = {cfg.escalate.max_tokens}",
        "",
        "API keys (effective):",
    ]
    for name in ("openrouter", "anthropic", "openai", "mistral", "custom"):
        source, masked = key_status(name)
        source_label = {
            "env_cue": "env (CUE_*)",
            "env": "env",
            "config": "config.toml",
            "keyring": "keychain",
            "none": "missing",
        }[source]
        lines.append(f"  {name:12} {masked:16} [{source_label}]")

    custom = cfg.get_provider_config("custom")
    if custom.base_url:
        lines.extend(["", "[providers.custom]", f"  base_url = {custom.base_url}", f"  name     = {custom.name}"])

    return "\n".join(lines)
