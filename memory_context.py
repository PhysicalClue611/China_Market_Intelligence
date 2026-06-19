"""
memory_context.py — Personal knowledge base context enrichment for run_intel.py

For each company, pulls two signal types before LLM generation:
  1. MemPalace semantic search — past intel snippets (multi-query: base + 战略/合作 + 财务/营收)
  2. Obsidian full-text search  — any notes in the vault mentioning this company

Returns a compact text block ready for prompt injection.
"""
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_BRIDGE_DEFAULT = "http://localhost:8765"


def _post(bridge_url: str, endpoint: str, payload: dict, timeout: int = 6) -> dict:
    """POST JSON to bridge. Returns {} on any error (fail-open)."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{bridge_url}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug("bridge %s error: %s", endpoint, e)
        return {}


def _mempalace_search(
    bridge_url: str,
    query: str,
    wing: str = "paperview",
    room: str = "general",
    source_contains: str = "china-companies",
    n_results: int = 3,
    max_distance: float = 0.85,
) -> list[dict]:
    result = _post(bridge_url, "/mempalace/search", {
        "query": query,
        "wing": wing,
        "room": room,
        "source_contains": source_contains,
        "n_results": n_results,
        "max_distance": max_distance,
    })
    return result.get("results", [])


def _obsidian_search(
    bridge_url: str,
    query: str,
    path: str = "Hermes/MI",
    max_results: int = 3,
) -> list[dict]:
    result = _post(bridge_url, "/obsidian/search", {
        "query": query,
        "path": path,
        "max_results": max_results,
    })
    return result.get("hits", [])


def get_company_context(
    company_zh: str,
    company_en: str,
    bridge_url: str = _BRIDGE_DEFAULT,
    max_chars: int = 1500,
) -> str:
    """
    Build a concise historical-context block for one company.

    Returns a non-empty string formatted for prompt injection, or ""
    if nothing useful was found (caller should skip injection).
    """
    sections: list[str] = []

    # 1. Past intel reports (MemPalace) — three focused queries, deduplicated
    mp_seen_sources: set[str] = set()
    mp_snippets: list[str] = []

    base_query = f"{company_zh} {company_en}"
    mp_queries = [
        base_query,
        f"{company_zh} 战略 合作 扩张",
        f"{company_zh} 财务 营收 利润",
    ]
    for q in mp_queries:
        for h in _mempalace_search(bridge_url, q, n_results=2):
            src = h.get("source_file", "?")
            if src in mp_seen_sources:
                continue
            mp_seen_sources.add(src)
            sim = h.get("similarity", 0)
            text = h.get("text", "")[:300].replace("\n", " ")
            mp_snippets.append(f"[{src} sim={sim:.2f}] {text}")

    if mp_snippets:
        sections.append("【历史情报摘要（MemPalace）】\n" + "\n".join(mp_snippets))

    # 2. Obsidian vault notes
    obs_hits = _obsidian_search(bridge_url, base_query)
    if obs_hits:
        snippets = []
        for h in obs_hits:
            path = h.get("path", "?")
            line = h.get("line_no", 0)
            snippet = h.get("snippet", "")
            snippets.append(f"[{path}:{line}] {snippet}")
        sections.append("【Obsidian笔记命中】\n" + "\n".join(snippets))

    if not sections:
        return ""

    header = f"=== {company_zh} / {company_en} 历史上下文 ===\n"
    body = "\n\n".join(sections)
    result = header + body
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...truncated]"
    return result
