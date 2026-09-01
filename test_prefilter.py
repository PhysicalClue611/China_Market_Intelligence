#!/usr/bin/env python3
"""Unit tests for prefilter_articles — OpenRouter Gemma reasoning-off gate (issue #17).

Run: .venv/bin/python -m pytest test_prefilter.py -v
"""
import pytest

from datetime import datetime, timedelta

import run_intel


def _recent_date(days: int = 2) -> str:
    """ISO date a few days ago — prefilter's event_date hard filter is 30 days,
    so fixture dates must stay recent or the test rots as time passes."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _article(i: int, title: str = "t"):
    return {
        "title": title,
        "content": f"content {i}",
        "url": f"https://x.com/{i}",
        "published_date": _recent_date(),
    }


class TestPrefilterRequestToOpenRouter:
    def test_request_body_uses_gemma_reasoning_off_with_provider_cfg(self, monkeypatch):
        captured = {}
        meta_seen = []

        def fake_call_llm_json(url, *, headers, json_body, timeout, logger=None, label="LLM", max_attempts=2, meta_cb=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json_body"] = json_body
            if meta_cb is not None:
                meta_cb({"provider": "Crusoe", "usage": {"completion_tokens_details": {"reasoning_tokens": 0}}})
                meta_seen.append("called")
            return {"keep": [{"i": 0, "event_date": _recent_date()}], "skip": False, "skip_reason": "", "length_hint": 400}

        monkeypatch.setattr(run_intel, "call_llm_json", fake_call_llm_json)
        articles = [_article(0), _article(1)]
        filtered, hint, status = run_intel.prefilter_articles("李宁", articles)

        assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
        body = captured["json_body"]
        assert body["model"] == "google/gemma-4-31b-it"
        assert body["reasoning"] == {"enabled": False}
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0
        provider = body["provider"]
        assert provider["order"] == ["Crusoe", "Friendli", "OpenInference"]
        assert provider["ignore"] == ["Together"]
        assert provider["allow_fallbacks"] is False

        headers = captured["headers"]
        assert headers["HTTP-Referer"] == "https://github.com/PhysicalClue611/China_Market_Intelligence"
        assert headers["X-OpenRouter-Title"] == "MI"
        assert headers["Authorization"].startswith("Bearer ")

        assert meta_seen == ["called"]
        assert status == "ok"
        assert hint == 400
        assert [a["title"] for a in filtered] == ["t"]


class TestPrefilterFiltering:
    def test_valid_keep_filters_and_returns_hint(self, monkeypatch):
        monkeypatch.setattr(
            run_intel, "call_llm_json",
            lambda *a, **k: {"keep": [{"i": 1, "event_date": None}], "skip": False, "length_hint": 600},
        )
        articles = [_article(0), _article(1), _article(2)]
        filtered, hint, status = run_intel.prefilter_articles("X", articles)
        assert status == "ok"
        assert hint == 600
        assert [a["title"] for a in filtered] == ["t"]

    def test_skip_true_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            run_intel, "call_llm_json",
            lambda *a, **k: {"keep": [], "skip": True, "skip_reason": "pr only", "length_hint": 0},
        )
        articles = [_article(0)]
        filtered, hint, status = run_intel.prefilter_articles("X", articles)
        assert filtered == []
        assert hint == 0
        assert status == "ok"


class TestPrefilterFailure:
    def test_non_dict_result_passthrough(self, monkeypatch):
        # call_llm_json returns None when the model emits a top-level JSON array
        # (parse → non-dict → retry → None). prefilter must pass everything through.
        monkeypatch.setattr(run_intel, "call_llm_json", lambda *a, **k: None)
        articles = [_article(0), _article(1)]
        filtered, hint, status = run_intel.prefilter_articles("X", articles)
        assert filtered == articles
        assert hint == 400
        assert status == "llm_failed_passthrough"


class TestCallLlmJsonRejectsArray:
    def test_top_level_array_yields_none(self, monkeypatch):
        import http_utils

        def fake_post(url, *, headers, json_body, timeout, max_retries=2):
            return {"choices": [{"message": {"content": '[{"i": 0}]'}}]}, None

        monkeypatch.setattr(http_utils, "post_with_retry", fake_post)
        assert (
            http_utils.call_llm_json("http://x", headers={}, json_body={}, timeout=5, max_attempts=2)
            is None
        )


class TestCallLlmJsonMetaCb:
    def test_meta_cb_receives_full_response_on_success(self, monkeypatch):
        import http_utils

        def fake_post(url, *, headers, json_body, timeout, max_retries=2):
            data = {
                "provider": "Crusoe",
                "usage": {"completion_tokens_details": {"reasoning_tokens": 0}},
                "choices": [{"message": {"content": '{"keep": [], "skip": false}'}}],
            }
            return data, None

        monkeypatch.setattr(http_utils, "post_with_retry", fake_post)
        seen = []
        result = http_utils.call_llm_json(
            "http://x", headers={}, json_body={}, timeout=5,
            meta_cb=lambda data: seen.append(data),
        )
        assert result == {"keep": [], "skip": False}
        assert seen and seen[0]["provider"] == "Crusoe"
        assert seen[0]["usage"]["completion_tokens_details"]["reasoning_tokens"] == 0

    def test_meta_cb_not_called_on_failure(self, monkeypatch):
        import http_utils

        def fake_post(url, *, headers, json_body, timeout, max_retries=2):
            return None, "HTTP 500: boom"

        monkeypatch.setattr(http_utils, "post_with_retry", fake_post)
        seen = []
        assert http_utils.call_llm_json(
            "http://x", headers={}, json_body={}, timeout=5, meta_cb=seen.append
        ) is None
        assert seen == []

    def test_meta_cb_exception_does_not_lose_result(self, monkeypatch):
        # A logging callback must never turn a successful LLM parse into a
        # failure (which would skip the whole company via pass-through).
        import http_utils

        def fake_post(url, *, headers, json_body, timeout, max_retries=2):
            data = {
                "provider": "Crusoe",
                "choices": [{"message": {"content": '{"keep": [], "skip": false}'}}],
            }
            return data, None

        def boom(data):
            raise RuntimeError("logging backend down")

        monkeypatch.setattr(http_utils, "post_with_retry", fake_post)
        result = http_utils.call_llm_json(
            "http://x", headers={}, json_body={}, timeout=5, meta_cb=boom
        )
        assert result == {"keep": [], "skip": False}


class TestValidateIntelConfig:
    def test_missing_openrouter_key_raises(self, monkeypatch):
        monkeypatch.setattr(run_intel, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(run_intel, "TAVILY_API_KEY", "k")
        monkeypatch.setattr(run_intel, "DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("HERMES_DATA", "/tmp/mi-test")
        monkeypatch.setenv("OBSIDIAN_PATH", "/tmp/mi-test")
        with pytest.raises(run_intel.IntelConfigError) as ei:
            run_intel._validate_intel_config()
        assert "OPENROUTER_API_KEY" in str(ei.value)

    def test_all_keys_present_passes(self, monkeypatch):
        monkeypatch.setattr(run_intel, "OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(run_intel, "TAVILY_API_KEY", "k")
        monkeypatch.setattr(run_intel, "DEEPSEEK_API_KEY", "k")
        monkeypatch.setenv("HERMES_DATA", "/tmp/mi-test")
        monkeypatch.setenv("OBSIDIAN_PATH", "/tmp/mi-test")
        run_intel._validate_intel_config()  # must not raise
