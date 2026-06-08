"""Tests for the provider layer — no network calls, uses fixtures and mocks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ctrlk.providers.base import GenResult, SYSTEM_PROMPT, DEFAULT_FEW_SHOT, few_shot_to_messages
from ctrlk.providers.anthropic import AnthropicProvider
from ctrlk.providers.openai_compat import OpenAICompatProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


# ---------------------------------------------------------------------------
# Provider protocol compliance
# ---------------------------------------------------------------------------

class TestProviderProtocol:
    def test_anthropic_has_name(self):
        p = AnthropicProvider(api_key="sk-test")
        assert p.name == "anthropic"

    def test_anthropic_supports_caching(self):
        p = AnthropicProvider(api_key="sk-test")
        assert p.supports_prompt_caching is True

    def test_openai_compat_has_name(self):
        p = OpenAICompatProvider("https://api.openai.com/v1", "sk-test", "openai")
        assert p.name == "openai"

    def test_openai_compat_supports_caching(self):
        p = OpenAICompatProvider("https://api.openai.com/v1", "sk-test", "openai")
        assert p.supports_prompt_caching is True


# ---------------------------------------------------------------------------
# Anthropic provider — fixture-based (no network)
# ---------------------------------------------------------------------------

class TestAnthropicProvider:
    def _make_provider(self):
        return AnthropicProvider(api_key="sk-ant-test-key")

    def test_generate_parses_fixture(self):
        fixture = _load_fixture("anthropic_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_provider()
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp

            result = provider.generate(
                SYSTEM_PROMPT,
                few_shot,
                "list all python files",
                model="claude-haiku-4-5-20250514",
                max_tokens=100,
            )

        assert isinstance(result, GenResult)
        assert result.text == "find . -name '*.py' -type f"
        assert result.tokens_in == 142
        assert result.tokens_out == 12
        assert result.cached_tokens == 112
        assert result.provider == "anthropic"
        assert result.error is None

    def test_no_api_key_returns_error(self):
        provider = AnthropicProvider(api_key="")
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)

        with patch.dict("os.environ", {}, clear=True):
            # Ensure env vars are not set
            import os
            os.environ.pop("ANTHROPIC_API_KEY", None)
            os.environ.pop("CTRLK_ANTHROPIC_API_KEY", None)
            result = provider.generate(SYSTEM_PROMPT, few_shot, "test", model="test")

        assert isinstance(result, GenResult)
        assert result.error is not None
        assert "No API key" in result.error

    def test_payload_includes_system_cache_control(self):
        """Verify that the system prompt is marked with cache_control."""
        fixture = _load_fixture("anthropic_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_provider()
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)
        captured_payload = {}

        def _capture_post(url, json=None, **kwargs):
            captured_payload.update(json or {})
            return mock_resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _capture_post

            provider.generate(SYSTEM_PROMPT, few_shot, "test query", model="claude-haiku-4-5-20250514")

        system_blocks = captured_payload.get("system", [])
        assert isinstance(system_blocks, list)
        assert len(system_blocks) == 1
        assert system_blocks[0].get("cache_control") == {"type": "ephemeral"}

    def test_http_error_returns_error_result(self):
        import httpx

        provider = self._make_provider()
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        err = httpx.HTTPStatusError("401", request=MagicMock(), response=mock_resp)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = err

            result = provider.generate(SYSTEM_PROMPT, few_shot, "test", model="m")

        assert result.error is not None
        assert "401" in result.error


# ---------------------------------------------------------------------------
# OpenAI-compatible provider — fixture-based
# ---------------------------------------------------------------------------

class TestOpenAICompatProvider:
    def _make_openai(self):
        return OpenAICompatProvider("https://api.openai.com/v1", "sk-test", "openai")

    def _make_openrouter(self):
        return OpenAICompatProvider(
            "https://openrouter.ai/api/v1",
            "sk-or-test",
            "openrouter",
            extra_headers={"HTTP-Referer": "https://example.com", "X-Title": "ctrlk"},
        )

    def test_openai_parses_fixture(self):
        fixture = _load_fixture("openai_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_openai()
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp

            result = provider.generate(
                SYSTEM_PROMPT, few_shot, "list all python files",
                model="gpt-4o-mini",
            )

        assert isinstance(result, GenResult)
        assert result.text == "find . -name '*.py' -type f"
        assert result.tokens_in == 142
        assert result.tokens_out == 12
        assert result.cached_tokens == 112
        assert result.provider == "openai"
        assert result.error is None

    def test_openrouter_parses_fixture(self):
        fixture = _load_fixture("openrouter_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_openrouter()
        few_shot = few_shot_to_messages(DEFAULT_FEW_SHOT)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp

            result = provider.generate(
                SYSTEM_PROMPT, few_shot, "test",
                model="anthropic/claude-haiku-4-5",
            )

        assert result.text == "find . -name '*.py' -type f"
        assert result.cached_tokens == 100
        assert result.provider == "openrouter"

    def test_request_includes_system_message(self):
        """Verify the system prompt is the first message in the payload."""
        fixture = _load_fixture("openai_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_openai()
        captured = {}

        def _capture(url, json=None, **kwargs):
            captured["payload"] = json or {}
            return mock_resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _capture

            provider.generate(SYSTEM_PROMPT, [], "test query", model="gpt-4o-mini")

        msgs = captured["payload"]["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == SYSTEM_PROMPT
        # Last message is the user query
        assert msgs[-1]["role"] == "user"
        assert msgs[-1]["content"] == "test query"

    def test_extra_headers_sent(self):
        """OpenRouter requires HTTP-Referer and X-Title headers."""
        fixture = _load_fixture("openrouter_response.json")
        mock_resp = MagicMock()
        mock_resp.json.return_value = fixture
        mock_resp.raise_for_status.return_value = None

        provider = self._make_openrouter()
        captured_headers = {}

        def _capture(url, json=None, headers=None, **kwargs):
            captured_headers.update(headers or {})
            return mock_resp

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = _capture

            provider.generate(SYSTEM_PROMPT, [], "test", model="m")

        assert captured_headers.get("HTTP-Referer") == "https://example.com"
        assert captured_headers.get("X-Title") == "ctrlk"

    def test_empty_choices_returns_empty_text(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"choices": [], "usage": {}}
        mock_resp.raise_for_status.return_value = None

        provider = self._make_openai()

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = lambda s: mock_client
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_resp

            result = provider.generate(SYSTEM_PROMPT, [], "test", model="m")

        assert result.text == ""


# ---------------------------------------------------------------------------
# Few-shot helpers
# ---------------------------------------------------------------------------

class TestFewShot:
    def test_few_shot_to_messages_alternates_roles(self):
        messages = few_shot_to_messages(DEFAULT_FEW_SHOT)
        roles = [m["role"] for m in messages]
        # Should alternate user/assistant
        for i in range(0, len(roles) - 1, 2):
            assert roles[i] == "user"
            assert roles[i + 1] == "assistant"

    def test_default_few_shot_not_empty(self):
        assert len(DEFAULT_FEW_SHOT) > 0

    def test_few_shot_messages_count(self):
        messages = few_shot_to_messages(DEFAULT_FEW_SHOT)
        assert len(messages) == len(DEFAULT_FEW_SHOT) * 2
