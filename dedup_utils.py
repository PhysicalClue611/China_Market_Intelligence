"""
Hermes intel dedup utilities — two independent layers.

Layer 2 — title similarity against 90-day article cache
    Catches: same story at a different URL (no external calls)

Layer 3 — MemPalace semantic search against past intel reports
    Catches: topic already covered in a previous weekly report

Layer 1 (URL exact dedup) lives in run_intel.py / seen_urls.json as before.
"""
import json
import logging
import re
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Layer 2: title similarity (article cache) ─────────────────────────────────

# Words to strip before Jaccard comparison.
# NOTE: year numbers (2021, 2024 …) are intentionally NOT included —
# "Annual Report 2021" and "Annual Report 2024" are different documents.
_STOPWORDS = {
    # English function words
    "the", "a", "an", "and", "or", "of", "in", "to", "for", "is", "are",
    "was", "were", "be", "been", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "at", "by", "from",
    "on", "with", "as", "its", "it", "this", "that", "their", "they", "we",
    "our",
    # Generic finance/company words that add no signal
    "report", "results", "company", "group", "co", "ltd", "inc",
    "corp", "holdings", "limited", "stock", "shares", "price", "news",
    "latest", "update", "q1", "q2", "q3", "q4", "fy",
    # Chinese generic words
    "集团", "股份", "有限公司", "控股", "公司", "最新", "消息",
}


def _words(text: str) -> set[str]:
    """Normalize text into a set of meaningful tokens."""
    text = text.lower().strip()
    tokens = set(re.split(r'[\s\-–—,.:;|()[\]{}"\'·•]+', text))
    tokens -= _STOPWORDS
    tokens -= {"", "-", "–", "—"}
    # Drop single chars and non-year pure numbers (years 20xx are meaningful signal)
    tokens = {t for t in tokens if len(t) > 1 and (not t.isdigit() or re.match(r'^20\d{2}$', t))}
    return tokens


def title_jaccard(title_a: str, title_b: str) -> float:
    """Jaccard similarity on word sets. Returns 0.0–1.0."""
    wa, wb = _words(title_a), _words(title_b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def find_cache_duplicate(
    new_url: str,
    new_title: str,
    cached_articles: list[dict],
    threshold: float = 0.65,
) -> tuple[bool, float, str]:
    """
    Compare new_title against all cached articles for the same company.
    Excludes the article itself (by URL).

    Returns:
        (is_duplicate, best_jaccard_score, matched_title)
    """
    best_score, best_title = 0.0, ""
    for art in cached_articles:
        if art.get("url") == new_url:
            continue
        cached_title = (art.get("title") or "").strip()
        if not cached_title:
            continue
        score = title_jaccard(new_title, cached_title)
        if score > best_score:
            best_score = score
            best_title = cached_title
    return best_score >= threshold, round(best_score, 3), best_title


# ── Layer 3: MemPalace semantic search (past reports) ─────────────────────────

# Default bridge URL when called from inside the container.
# For host-side scripts (test_dedup.py), pass bridge_url="http://localhost:8765".
_BRIDGE_DEFAULT = "http://localhost:8765"


def search_mempalace(
    query: str,
    bridge_url: str = _BRIDGE_DEFAULT,
    wing: str | None = "paperview",
    room: str | None = "general",
    source_contains: str | None = "china-companies",
    n_results: int = 3,
    timeout: int = 5,
) -> list[dict]:
    """
    Query the MemPalace HTTP bridge.
    Returns list of hit dicts with keys: similarity, source_file, text.
    Returns [] silently on any error so dedup degrades gracefully.
    """
    payload = json.dumps({
        "query": query,
        "wing": wing,
        "room": room,
        "source_contains": source_contains,
        "n_results": n_results,
    }, ensure_ascii=False).encode()

    req = urllib.request.Request(
        f"{bridge_url}/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get("results", [])
    except urllib.error.URLError as e:
        logger.warning("MemPalace bridge unreachable (%s): dedup skipped", e)
        return []
    except Exception as e:
        logger.warning("MemPalace search error: %s", e)
        return []


def find_report_duplicate(
    title: str,
    content_snippet: str,
    bridge_url: str = _BRIDGE_DEFAULT,
    threshold: float = 0.65,
    **kwargs,
) -> tuple[bool, float, str]:
    """
    Check if this article's topic was already covered in a past intel report.

    Returns:
        (is_duplicate, top_similarity, matched_source_file)
    """
    query = f"{title}. {content_snippet}"[:500]
    hits = search_mempalace(query, bridge_url=bridge_url, **kwargs)
    if not hits:
        return False, 0.0, ""
    top = hits[0]
    sim = top.get("similarity", 0.0)
    src = top.get("source_file", "")
    return sim >= threshold, round(sim, 3), src
