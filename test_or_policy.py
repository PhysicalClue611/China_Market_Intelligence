"""OpenRouter provider policy (owner rule 2026-11-03): every OpenRouter request
carries data_collection=deny. Enforced once in http_utils.post_with_retry, which
every MI OpenRouter call goes through (directly or via call_llm_json)."""
from unittest.mock import MagicMock

import http_utils

OR_URL = "https://openrouter.ai/api/v1/chat/completions"


def _capture(monkeypatch):
    sent = []

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append((url, json))
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"choices": [{"message": {"content": '{"ok": true}'}}]}
        return resp

    monkeypatch.setattr(http_utils.httpx, "post", fake_post)
    return sent


def test_openrouter_request_without_provider_gets_deny(monkeypatch):
    sent = _capture(monkeypatch)
    body = {"model": "anthropic/claude-haiku-5.5", "messages": []}
    http_utils.post_with_retry(OR_URL, headers={}, json_body=body, timeout=5)
    assert sent[0][1]["provider"] == {"data_collection": "deny"}
    assert "provider" not in body, "caller's dict must not be mutated"


def test_openrouter_request_keeps_provider_keys_and_forces_deny(monkeypatch):
    sent = _capture(monkeypatch)
    body = {"model": "m", "provider": {"order": ["X"], "data_collection": "allow"}}
    http_utils.post_with_retry(OR_URL, headers={}, json_body=body, timeout=5)
    assert sent[0][1]["provider"] == {"order": ["X"], "data_collection": "deny"}


def test_call_llm_json_goes_through_policy(monkeypatch):
    sent = _capture(monkeypatch)
    http_utils.call_llm_json(OR_URL, headers={}, json_body={"model": "m", "messages": []}, timeout=5)
    assert sent[0][1]["provider"]["data_collection"] == "deny"


def test_non_openrouter_request_untouched(monkeypatch):
    sent = _capture(monkeypatch)
    body = {"query": "q"}
    http_utils.post_with_retry("https://api.tavily.com/search", headers={}, json_body=body, timeout=5)
    assert sent[0][1] == {"query": "q"}
