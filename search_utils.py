"""Three-level search fallback: Tavily → SerpApi → Serper.dev
Plus: Chinese-language news supplemental search via Serper News → SerpApi News.
"""
import logging
import os

import httpx

from http_utils import get_with_retry, post_with_retry

logger = logging.getLogger(__name__)

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")

# OpenRouter provider whitelist for gpt-oss-20b calls (used by email_check.py)
PROVIDER_ORDER = ["Inceptron", "AkashML", "Nebius", "NovitaAI", "Parasail"]


def _tavily(query: str, days: int = 8, max_results: int = 8) -> list[dict]:
    if not TAVILY_API_KEY:
        raise RuntimeError("TAVILY_API_KEY not set")
    data, err = post_with_retry(
        "https://api.tavily.com/search",
        headers={},
        json_body={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "topic": "general",
            "max_results": max_results,
            "days": days,
            "include_answer": False,
        },
        timeout=20,
    )
    if err:
        raise RuntimeError(f"Tavily: {err}")
    return [
        {
            "title": r["title"],
            "content": r.get("content", ""),
            "url": r["url"],
            "published_date": r.get("published_date", ""),  # ISO string or ""
        }
        for r in data.get("results", [])
    ]


def _serpapi(query: str, num: int = 8) -> list[dict]:
    if not SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY not set")
    data, err = get_with_retry(
        "https://serpapi.com/search",
        params={"q": query, "api_key": SERPAPI_API_KEY, "engine": "google", "num": num},
        timeout=20,
    )
    if err:
        raise RuntimeError(f"SerpApi: {err}")
    return [
        {
            "title": r.get("title", ""),
            "content": r.get("snippet", ""),
            "url": r.get("link", ""),
            "published_date": "",
        }
        for r in data.get("organic_results", [])
    ]


def _serper(query: str, num: int = 8) -> list[dict]:
    if not SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY not set")
    data, err = post_with_retry(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json_body={"q": query, "num": num},
        timeout=15,
    )
    if err:
        raise RuntimeError(f"Serper: {err}")
    return [
        {
            "title": r.get("title", ""),
            "content": r.get("snippet", ""),
            "url": r.get("link", ""),
            "published_date": "",
        }
        for r in data.get("organic", [])
    ]


def _serper_news(query: str, num: int = 8) -> list[dict]:
    """Serper.dev /news endpoint — Chinese news sources, includes publication date."""
    if not SERPER_API_KEY:
        raise RuntimeError("SERPER_API_KEY not set")
    data, err = post_with_retry(
        "https://google.serper.dev/news",
        headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
        json_body={"q": query, "num": num, "hl": "zh-cn", "gl": "cn"},
        timeout=15,
    )
    if err:
        raise RuntimeError(f"Serper News: {err}")
    return [
        {
            "title": r.get("title", ""),
            "content": r.get("snippet", ""),
            "url": r.get("link", ""),
            "published_date": r.get("date", ""),
        }
        for r in data.get("news", [])
    ]


def _serpapi_news(query: str, num: int = 8) -> list[dict]:
    """SerpApi Google News tab (tbm=nws) — past-week filter, Chinese locale."""
    if not SERPAPI_API_KEY:
        raise RuntimeError("SERPAPI_API_KEY not set")
    data, err = get_with_retry(
        "https://serpapi.com/search",
        params={
            "q": query,
            "api_key": SERPAPI_API_KEY,
            "engine": "google",
            "tbm": "nws",
            "hl": "zh-CN",
            "gl": "cn",
            "tbs": "qdr:w",  # past week
            "num": num,
        },
        timeout=20,
    )
    if err:
        raise RuntimeError(f"SerpApi News: {err}")
    return [
        {
            "title": r.get("title", ""),
            "content": r.get("snippet", ""),
            "url": r.get("link", ""),
            "published_date": r.get("date", ""),
        }
        for r in data.get("news_results", [])
    ]


def search(query: str, days: int = 8, max_results: int = 8) -> list[dict]:
    """Try Tavily → SerpApi → Serper.dev in order; return first success.
    Raises RuntimeError if all providers fail (caller decides how to handle).
    """
    for name, fn, kwargs in [
        ("Tavily", _tavily, {"days": days, "max_results": max_results}),
        ("SerpApi", _serpapi, {"num": max_results}),
        ("Serper", _serper, {"num": max_results}),
    ]:
        try:
            results = fn(query, **kwargs)
            logger.info(f"search via {name}: {len(results)} results for '{query[:60]}'")
            return results
        except Exception as e:
            logger.warning(f"{name} failed: {e}, trying next")
    logger.error(f"All search providers failed for query: '{query[:60]}'")
    raise RuntimeError(f"All search providers failed for query: '{query[:60]}'")


def search_cn(query: str, max_results: int = 8) -> list[dict]:
    """Chinese-language news search: Serper News → SerpApi News fallback.
    Returns [] on total failure (non-fatal supplement, main search already ran).
    Targets Chinese news sources (新浪财经, 东方财富, 腾讯财经, 界面, 财新 etc.)
    with past-week freshness filter.
    """
    for name, fn, kwargs in [
        ("Serper News", _serper_news, {"num": max_results}),
        ("SerpApi News", _serpapi_news, {"num": max_results}),
    ]:
        try:
            results = fn(query, **kwargs)
            logger.info(f"search_cn via {name}: {len(results)} results for '{query[:60]}'")
            return results
        except Exception as e:
            logger.warning(f"{name} failed: {e}, trying next")
    logger.warning(f"All CN news providers failed for query: '{query[:60]}'")
    return []
