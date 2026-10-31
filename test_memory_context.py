"""Offline regression tests for MemPalace context search defaults."""

import json
from unittest.mock import MagicMock, patch

import dedup_utils
import memory_context


def test_mempalace_search_default_room():
    with patch.object(memory_context, "_post", return_value={}) as post:
        memory_context._mempalace_search("http://bridge.invalid", "company")

    post.assert_called_once_with("http://bridge.invalid", "/mempalace/search", {
        "query": "company",
        "wing": "paperview",
        "room": None,
        "source_contains": "china-companies",
        "n_results": 3,
        "max_distance": 0.85,
    })


def test_mempalace_search_explicit_room():
    with patch.object(memory_context, "_post", return_value={}) as post:
        memory_context._mempalace_search(
            "http://bridge.invalid", "company", room="finance",
        )

    assert post.call_args.args[2]["room"] == "finance"


def test_dedup_search_mempalace_default_room():
    response = MagicMock()
    response.__enter__.return_value.read.return_value = b'{"results": []}'
    with patch("dedup_utils.urllib.request.urlopen", return_value=response) as urlopen:
        assert dedup_utils.search_mempalace("company") == []

    urlopen.assert_called_once()
    request = urlopen.call_args.args[0]
    assert json.loads(request.data) == {
        "query": "company",
        "wing": "paperview",
        "room": None,
        "source_contains": "china-companies",
        "n_results": 3,
    }
    assert urlopen.call_args.kwargs == {"timeout": 5}


def test_get_company_context_includes_mempalace_hit():
    hit_text = "Historical company intelligence"

    def post_stub(bridge_url, endpoint, payload):
        if endpoint == "/mempalace/search":
            return {"results": [{
                "source_file": "china-companies/report.md",
                "similarity": 0.9,
                "text": hit_text,
            }]}
        assert endpoint == "/obsidian/search"
        return {"hits": []}

    with patch.object(memory_context, "_post", side_effect=post_stub):
        context = memory_context.get_company_context("测试公司", "Test Company")

    assert context
    assert hit_text in context
