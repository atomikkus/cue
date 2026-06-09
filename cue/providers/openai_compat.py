"""OpenAI-compatible provider adapter.

Covers: OpenAI, Mistral, OpenRouter, Ollama, LocalAI, vLLM, and any other
service that exposes the OpenAI /chat/completions API shape.

One adapter parameterised by base_url, api_key, and extra_headers covers all of them.
"""

from __future__ import annotations

import json
import os
from typing import Iterator

import httpx

from .base import GenResult


class OpenAICompatProvider:
    """Generic OpenAI /chat/completions adapter."""

    supports_prompt_caching = True  # Best-effort; silently ignored if unsupported

    def __init__(
        self,
        base_url: str,
        api_key: str,
        name: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.extra_headers = extra_headers or {}

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
        messages = [{"role": "system", "content": system}]
        messages.extend(few_shot)
        messages.append({"role": "user", "content": user})

        payload: dict = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if stop:
            payload["stop"] = stop
        if stream:
            payload["stream"] = True

        headers = {
            "content-type": "application/json",
            **self.extra_headers,
        }
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/chat/completions"

        if stream:
            return self._stream(url, payload, headers, model)

        return self._complete(url, payload, headers, model)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _complete(self, url: str, payload: dict, headers: dict, model: str) -> GenResult:
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.post(url, json=payload, headers=headers)
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
        # Some providers (OpenRouter) report cached tokens under prompt_tokens_details
        cached = (
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            or usage.get("cached_tokens", 0)
        )
        return GenResult(
            text=text.strip(),
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cached_tokens=cached,
            model=data.get("model", model),
            provider=self.name,
        )

    def _stream(self, url: str, payload: dict, headers: dict, model: str) -> Iterator[str]:
        try:
            with httpx.Client(timeout=60.0) as client:
                with client.stream("POST", url, json=payload, headers=headers) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        chunk = line[6:]
                        if chunk == "[DONE]":
                            break
                        try:
                            event = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        choices = event.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content")
                            if content:
                                yield content
        except Exception:
            return

    @staticmethod
    def _extract_text(data: dict) -> str:
        choices = data.get("choices", [])
        if not choices:
            return ""
        return choices[0].get("message", {}).get("content", "") or ""
