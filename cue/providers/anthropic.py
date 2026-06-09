"""Anthropic provider — native /v1/messages API with prompt caching.

The static system prompt and few-shot prefix are marked with cache_control
so the provider bills them once and serves from cache on subsequent calls.
This is the key token-saving mechanism on the provider side.
"""

from __future__ import annotations

import os
from typing import Iterator

import httpx

from .base import GenResult


_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
_CACHE_CONTROL = {"type": "ephemeral"}


class AnthropicProvider:
    """Native Anthropic /v1/messages adapter with prompt-caching support."""

    name = "anthropic"
    supports_prompt_caching = True

    def __init__(self, api_key: str = "") -> None:
        # Key resolution order: explicit arg → CUE_ANTHROPIC_API_KEY → ANTHROPIC_API_KEY
        self.api_key = (
            api_key
            or os.environ.get("CUE_ANTHROPIC_API_KEY", "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )

    # ------------------------------------------------------------------
    # Public interface (satisfies Provider protocol)
    # ------------------------------------------------------------------

    def generate(
        self,
        system: str,
        few_shot: list[dict],
        user: str,
        *,
        model: str,
        max_tokens: int = 100,
        stop: list[str] | None = None,
        stream: bool = False,
    ) -> GenResult | Iterator[str]:
        if not self.api_key:
            return GenResult(
                text="", tokens_in=0, tokens_out=0, cached_tokens=0,
                model=model, provider=self.name,
                error="No API key configured for Anthropic provider.",
            )

        # Build system block with cache_control to amortize it across calls
        system_block = [
            {
                "type": "text",
                "text": system,
                "cache_control": _CACHE_CONTROL,
            }
        ]

        # Build messages: few-shot (with cache on last pair) + user query
        messages = []
        few_shot_count = len(few_shot)
        for i, msg in enumerate(few_shot):
            entry: dict = {"role": msg["role"], "content": msg["content"]}
            # Mark the last few-shot message so the prefix is cached
            if i == few_shot_count - 1:
                entry = {
                    "role": msg["role"],
                    "content": [
                        {
                            "type": "text",
                            "text": msg["content"],
                            "cache_control": _CACHE_CONTROL,
                        }
                    ],
                }
            messages.append(entry)

        # Dynamic user message (not cached — changes every call)
        messages.append({"role": "user", "content": user})

        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system_block,
            "messages": messages,
        }
        if stop:
            payload["stop_sequences"] = stop

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }

        if stream:
            return self._stream(payload, headers, model)

        return self._complete(payload, headers, model)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _complete(self, payload: dict, headers: dict, model: str) -> GenResult:
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(_ANTHROPIC_API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            return GenResult(
                text="", tokens_in=0, tokens_out=0, cached_tokens=0,
                model=model, provider=self.name,
                error=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except Exception as exc:
            return GenResult(
                text="", tokens_in=0, tokens_out=0, cached_tokens=0,
                model=model, provider=self.name,
                error=str(exc),
            )

        text = self._extract_text(data)
        usage = data.get("usage", {})
        return GenResult(
            text=text.strip(),
            tokens_in=usage.get("input_tokens", 0),
            tokens_out=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cache_read_input_tokens", 0),
            model=data.get("model", model),
            provider=self.name,
        )

    def _stream(self, payload: dict, headers: dict, model: str) -> Iterator[str]:
        payload["stream"] = True
        try:
            with httpx.Client(timeout=60.0) as client:
                with client.stream("POST", _ANTHROPIC_API_URL, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if line.startswith("data: "):
                            chunk = line[6:]
                            if chunk == "[DONE]":
                                break
                            import json
                            try:
                                event = json.loads(chunk)
                            except json.JSONDecodeError:
                                continue
                            if event.get("type") == "content_block_delta":
                                delta = event.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    yield delta.get("text", "")
        except Exception:
            return

    @staticmethod
    def _extract_text(data: dict) -> str:
        content = data.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block.get("text", "")
        return ""
