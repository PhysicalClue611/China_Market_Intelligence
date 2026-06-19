import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
CACHE_PATH = DATA_DIR / "article_cache.json"
TTL_DAYS = 90


def _load() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        data = json.loads(CACHE_PATH.read_text())
        cutoff = datetime.now().timestamp() - TTL_DAYS * 86400
        valid = {url: a for url, a in data.items() if a.get("ts", 0) >= cutoff}
        if len(valid) != len(data):
            logger.info(f"Article cache: pruned {len(data) - len(valid)} expired entries")
            CACHE_PATH.write_text(json.dumps(valid))
        return valid
    except Exception as e:
        logger.warning(f"Article cache load failed: {e}")
        return {}


def _save(data: dict):
    CACHE_PATH.write_text(json.dumps(data))


def save_articles(articles: list[dict], company: str):
    """持久化一批文章。articles 每项含 url, title, content。"""
    cache = _load()
    ts = datetime.now().timestamp()
    added = 0
    for a in articles:
        url = a.get("url", "")
        if not url:
            continue
        if url not in cache:
            cache[url] = {
                "title": a.get("title", ""),
                "content": a.get("content", ""),
                "company": company,
                "ts": ts,
            }
            added += 1
    if added:
        _save(cache)
        logger.info(f"Article cache: +{added} new articles for [{company}]")


def get_articles_by_company(company: str) -> list[dict]:
    """返回某公司在缓存中的所有文章，按时间倒序。"""
    cache = _load()
    results = [
        {"url": url, **meta}
        for url, meta in cache.items()
        if meta.get("company") == company
    ]
    results.sort(key=lambda x: x["ts"], reverse=True)
    return results


def get_all_companies() -> list[str]:
    cache = _load()
    return list({a["company"] for a in cache.values()})
