#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

import re

import httpx

from http_utils import post_with_retry, extract_llm_text, call_llm_json
from email_sender import send_report
from slack_sender import post_report as post_slack_report
from config_store import get_companies_full, get_recipients
from hermes_footer import get_footer
from article_cache import save_articles, get_articles_by_company
from dedup_utils import find_cache_duplicate
from memory_context import get_company_context
from search_utils import search as search_news, search_cn as search_cn_news

logger = logging.getLogger(__name__)


class IntelConfigError(RuntimeError):
    """必需的情报配置缺失，属于部署错误，不应被当作单次抓取失败静默吞掉（exit 0）。"""


def _validate_intel_config():
    """启动时校验必需环境变量非空，缺失则直接抛出（fail fast），日志只报变量名不报值。"""
    required = {
        "TAVILY_API_KEY": TAVILY_API_KEY,
        "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
        "HERMES_DATA": os.getenv("HERMES_DATA", ""),
        "OBSIDIAN_PATH": os.getenv("OBSIDIAN_PATH", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise IntelConfigError(
            f"Missing required intel config: {', '.join(missing)} — check .env"
        )


DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
OBSIDIAN_DIR = Path(os.getenv("OBSIDIAN_PATH", "/opt/obsidian"))
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
JINA_BASE_URL = "https://r.jina.ai"

SEEN_URLS_PATH = DATA_DIR / "seen_urls.json"
FETCH_LOG_PATH = DATA_DIR / "fetch_log.json"
SEEN_URL_TTL_DAYS = 90

LLM_MODEL_FLASH = "deepseek-v4-flash"   # prefilter gate
LLM_MODEL_PRO   = "deepseek-v4-pro"     # final synthesis

SYSTEM_PROMPT = (
    "你是一名国际顶尖IT咨询公司的合伙人，专注于拓展战略咨询与IT咨询业务，"
    "包括接触高层领导、发掘咨询机会、引领咨询项目签约及落地，以及后续实施交付和收款工作。"
    "你的分析始终服务于识别潜在咨询机会、评估客户付费意愿与能力、以及规避商务风险。"
)

SGT = timezone(timedelta(hours=8))
CURRENT_YEAR = datetime.now().year

SEARCH_DAYS = 8        # Tavily: index freshness window (days)
PUB_DATE_MAX_AGE = 9   # drop articles published > N days ago if date parseable
EVENT_MAX_AGE_DAYS = 30  # drop prefilter-kept articles whose extracted underlying
                         # event date (not the article's own publish/reprint date)
                         # is older than this — see issue #12
JINA_ENRICH_MAX = 5    # max articles to enrich per company (no-date English results)
JINA_CACHE_CHARS = 4000  # full text stored in article cache
JINA_LLM_CHARS = 2000    # full text passed to LLM prompt (vs 500 for plain snippets)


# ── Date parsing ──────────────────────────────────────────────────────────────

def _parse_pub_date(date_str: str) -> datetime | None:
    """Parse publication date strings into datetime. Returns None if unparseable.

    Handles:
    - ISO:            "2026-05-20" / "2026-05-20T00:00:00Z"
    - Chinese abs:    "2025年3月25日"
    - Chinese rel:    "3天前" / "2周前" / "1个月前"
    - English rel:    "3 days ago" / "2 weeks ago" / "1 month ago"
    """
    if not date_str:
        return None
    s = date_str.strip()
    now = datetime.now()

    # Chinese relative
    m = re.match(r'^(\d+)\s*天前$', s)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.match(r'^(\d+)\s*周前$', s)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    m = re.match(r'^(\d+)\s*个月前$', s)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)

    # Chinese absolute: "2025年3月25日"
    m = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # English relative: "3 days ago", "2 weeks ago", "1 month ago"
    m = re.match(r'^(\d+)\s+day', s, re.I)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.match(r'^(\d+)\s+week', s, re.I)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    m = re.match(r'^(\d+)\s+month', s, re.I)
    if m:
        return now - timedelta(days=int(m.group(1)) * 30)

    # ISO: "2026-05-20" or "2026-05-20T..."
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except (ValueError, IndexError):
        pass

    return None


# ── Staleness filter ──────────────────────────────────────────────────────────

def _is_stale(title: str) -> bool:
    """True if title's max 20xx year is 2+ years behind current year (e.g. 2021 annual report)."""
    years = [int(y) for y in re.findall(r'\b(20\d{2})\b', title)]
    return bool(years) and max(years) < CURRENT_YEAR - 1


# ── Last report section loader ─────────────────────────────────────────────────

def load_last_report_section(company_zh: str) -> str:
    """Extract company_zh's section from the most recent previous weekly report.
    Returns "" if no previous report or company not found.
    """
    report_dir = OBSIDIAN_DIR / "Hermes" / "MI"
    today = datetime.now().strftime("%Y-%m-%d")

    reports = sorted(report_dir.glob("*-china-companies.md"), reverse=True)
    target = None
    for r in reports:
        # stem is e.g. "2026-05-17-china-companies"
        date_part = r.stem.replace("-china-companies", "")
        if date_part != today:
            target = r
            break

    if not target:
        return ""

    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        return ""

    lines = text.split("\n")
    in_section = False
    section_lines: list[str] = []

    for line in lines:
        if line.strip() == f"## {company_zh}":
            in_section = True
            continue
        if in_section:
            # Each company section ends at the next "---" separator or "## " header
            if line.startswith("## ") or line.strip() == "---":
                break
            section_lines.append(line)

    section = "\n".join(section_lines).strip()
    if len(section) > 1200:
        section = section[:1200] + "\n[...截断]"
    return section


# ── Dedup state ───────────────────────────────────────────────────────────────

def _load_seen_urls() -> dict:
    if not SEEN_URLS_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_URLS_PATH.read_text())
        cutoff = datetime.now().timestamp() - SEEN_URL_TTL_DAYS * 86400
        valid = {url: meta for url, meta in data.items() if meta.get("ts", 0) >= cutoff}
        if len(valid) != len(data):
            SEEN_URLS_PATH.write_text(json.dumps(valid))
        return valid
    except Exception:
        return {}


def _save_seen_urls(seen: dict):
    SEEN_URLS_PATH.write_text(json.dumps(seen))


def _load_fetch_log() -> dict:
    if not FETCH_LOG_PATH.exists():
        return {}
    try:
        return json.loads(FETCH_LOG_PATH.read_text())
    except Exception:
        return {}


def _save_fetch_log(log: dict):
    FETCH_LOG_PATH.write_text(json.dumps(log))


def _save_processed_id(msg_id):
    if isinstance(msg_id, list):
        for mid in msg_id:
            _save_processed_id(mid)
        return
    path = DATA_DIR / "processed_email_ids.json"
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        data = {}
    data[msg_id] = datetime.now().timestamp()
    path.write_text(json.dumps(data))


# ── Jina full-text extraction ─────────────────────────────────────────────────

def _jina_extract(url: str) -> str:
    """Fetch full article text via Jina Reader API. Returns "" on failure (non-fatal).

    Used to enrich English articles that lack published_date (Tavily results),
    replacing short NLP snippets with actual article content.
    """
    if not JINA_API_KEY or not url:
        return ""
    try:
        resp = httpx.get(
            f"{JINA_BASE_URL}/{url}",
            headers={
                "Authorization": f"Bearer {JINA_API_KEY}",
                "Accept": "application/json",
                "X-Return-Format": "markdown",
            },
            timeout=15,
            follow_redirects=True,
        )
        resp.raise_for_status()
        content = resp.json().get("data", {}).get("content", "")
        return content[:JINA_CACHE_CHARS] if content else ""
    except Exception as e:
        logger.debug(f"Jina extract failed [{url[:60]}]: {e}")
        return ""


# ── Search + fetch ────────────────────────────────────────────────────────────

def fetch_company_raw(company: dict) -> tuple[list[dict], dict] | tuple[None, dict]:
    """Bilingual + CN-news parallel search, then relevance/freshness filters.

    Returns (articles_or_None, funnel) where funnel tracks survivor counts at
    each filter stage for diagnosing why weekly output has been shrinking.
    """
    zh = company["zh"]
    en = company["en"]
    funnel = {"company": zh}

    # Primary bilingual search (Tavily days=8 → SerpApi → Serper fallback)
    if en:
        query = f"{en} {zh} latest news strategy financials"
    else:
        query = f"{zh} 最新动态 战略 合作 财报 业绩"
    try:
        results = search_news(query, days=SEARCH_DAYS, max_results=8)
    except Exception as e:
        logger.warning(f"Search failed [{query}]: {e}")
        funnel["error"] = "search_failed"
        return None, funnel

    # Supplemental Chinese news search (Serper News → SerpApi News, non-fatal)
    cn_query = f"{zh} {en} 最新 新闻 战略 财报" if en else f"{zh} 最新 新闻 战略"
    cn_results = search_cn_news(cn_query, max_results=6)
    if cn_results:
        seen_set = {r["url"] for r in results}
        added = 0
        for r in cn_results:
            if r["url"] and r["url"] not in seen_set:
                results.append(r)
                seen_set.add(r["url"])
                added += 1
        if added:
            logger.info(f"[{zh}] CN news supplement: +{added} articles")

    funnel["raw"] = len(results)

    # Method B: published_date filter — drop if parseable date is > PUB_DATE_MAX_AGE days ago
    cutoff_dt = datetime.now() - timedelta(days=PUB_DATE_MAX_AGE)
    fresh_by_date = []
    date_dropped = 0
    for r in results:
        pub_dt = _parse_pub_date(r.get("published_date", ""))
        if pub_dt and pub_dt < cutoff_dt:
            logger.info(f"[{zh}] Date filter: dropped '{r['title'][:55]}' (pub={r['published_date'][:10]})")
            date_dropped += 1
            continue
        fresh_by_date.append(r)
    if date_dropped:
        logger.info(f"[{zh}] Date filter: {len(results)} → {len(fresh_by_date)} ({date_dropped} dropped)")
    results = fresh_by_date
    funnel["after_date"] = len(results)

    # Relevance filter: Chinese prefix or English first word must appear
    zh_kw = zh[:2]
    en_kw = en.split()[0].lower() if en else ""
    relevant = [
        r for r in results
        if zh_kw in r["title"] or zh_kw in r["content"]
        or (en_kw and (en_kw in r["title"].lower() or en_kw in r["content"].lower()))
    ]
    if len(relevant) < len(results):
        logger.info(f"[{zh}] Relevance filter: {len(results)} → {len(relevant)} results")
    funnel["after_relevance"] = len(relevant)

    # Year staleness filter: titles containing only years ≥2 behind current year
    fresh = [r for r in relevant if not _is_stale(r["title"])]
    if len(fresh) < len(relevant):
        stale_titles = [r["title"] for r in relevant if _is_stale(r["title"])]
        logger.info(f"[{zh}] Staleness filter: removed {len(relevant) - len(fresh)} old docs: {stale_titles}")
        relevant = fresh
    funnel["after_staleness"] = len(relevant)

    # Jina enrichment: replace short Tavily snippets with full article text.
    # Only targets articles without published_date (English Tavily results);
    # Serper News articles have dates and their snippets already locate the story.
    if JINA_API_KEY:
        to_enrich = [r for r in relevant if not r.get("published_date")][:JINA_ENRICH_MAX]
        if to_enrich:
            enriched = 0
            for r in to_enrich:
                full_text = _jina_extract(r["url"])
                if full_text and len(full_text) > len(r.get("content", "")):
                    r["content"] = full_text
                    r["jina_enriched"] = True
                    enriched += 1
            if enriched:
                logger.info(f"[{zh}] Jina enriched: {enriched}/{len(to_enrich)} articles with full text")

    # Persist to 90-day cache AFTER enrichment so full text is stored
    if relevant:
        save_articles(relevant, zh)

    # Truncate for LLM prompt:
    # - Jina-enriched articles: up to JINA_LLM_CHARS (real content, worth using)
    # - Plain snippets: 500 chars (same as before)
    for r in relevant:
        limit = JINA_LLM_CHARS if r.get("jina_enriched") else 500
        r["content"] = r["content"][:limit]

    return relevant, funnel


# ── Prefilter (V4 Flash) ──────────────────────────────────────────────────────

def prefilter_articles(
    company_zh: str,
    articles: list[dict],
    last_report_section: str = "",
) -> tuple[list[dict], int, str]:
    """V4 Flash gate: filter low-value articles and cross-week duplicates.

    Also checks against last_report_section to remove articles whose content
    is already fully covered in the previous week's report.

    Returns (filtered_articles, length_hint, status). length_hint is 0 if all
    skipped. status is "ok" when the LLM gate actually ran, or
    "llm_failed_passthrough" when the call/parse failed and all input articles
    were passed through unfiltered — callers should distinguish the two in the
    funnel log rather than reading a pass-through as "model kept everything"
    (issue #10).
    """
    if not articles:
        return [], 0, "ok"

    sgt_now = datetime.now(SGT).strftime("%Y-%m-%d %H:%M SGT")
    article_list = json.dumps(
        [{"i": i, "title": a["title"], "summary": a["content"][:300]}
         for i, a in enumerate(articles)],
        ensure_ascii=False,
    )

    last_report_block = ""
    if last_report_section:
        last_report_block = (
            f"\n\n【上周报告内容（参考）】\n"
            f"以下是上周关于{company_zh}的报告摘要。请将与此高度重叠、无新进展的文章从 keep 列表移除：\n"
            f"{last_report_section[:800]}"
        )

    prompt = f"""你是一个情报质量控制器。以下是关于「{company_zh}」的文章列表：

{article_list}

当前时间：{sgt_now}{last_report_block}

请完成以下判断（按优先级）：
1. 时效性：过滤掉明显超过7天的内容。判断依据：标题/摘要中的明确日期，或文章自带的 published_date 字段。**无日期的文章不得默认保留，必须根据内容本身判断时效性**——若内容仅涉及历史事件、旧财报、旧公告，则视为超期丢弃；仅当内容本身无法判断新旧时才保留。
2. 相关性：过滤掉与{company_zh}关联度低的文章（仅提及公司名但内容不相关）
3. 跨周去重：若某篇文章报道的事件与上周报告高度重叠（同一事件无新进展），从 keep 移除
4. 信息量：若所有剩余文章均为PR稿、无实质内容（无战略变动、财务数据、合作/风险事件），返回 skip=true
5. 长度建议：根据剩余文章质量和数量，建议报告段字数（200、400或600）
6. 事件日期抽取：对每篇保留的文章，从标题/摘要中提取其报道的**核心事实性事件日期**（如财报期间、公告发布日、事件实际发生日期），格式 YYYY-MM-DD，无法判断则填 null。**必须区分"文章发布/转载时间"与"事件发生时间"**——例如英文媒体近期转载报道一年前的财报数据，event_date 应填财报对应的实际披露日期，不是转载文章本身的发布日期。

直接输出JSON，禁止任何思考过程文字，禁止markdown代码块：{{"keep": [{{"i": 0, "event_date": "2026-07-09"}}, {{"i": 2, "event_date": null}}], "skip": false, "skip_reason": "", "length_hint": 400}}"""

    result = call_llm_json(
        DEEPSEEK_BASE_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json_body={
            "model": LLM_MODEL_FLASH,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        },
        timeout=45,
        logger=logger,
        label=f"{company_zh} Prefilter",
    )
    if result is None:
        return articles, 400, "llm_failed_passthrough"

    try:
        if result.get("skip"):
            logger.info(f"[{company_zh}] Prefilter skip: {result.get('skip_reason', '')}")
            return [], 0, "ok"
        keep_raw = result.get("keep", [{"i": i, "event_date": None} for i in range(len(articles))])
        kept = []
        for entry in keep_raw:
            if isinstance(entry, dict):
                idx, event_date = entry.get("i"), entry.get("event_date")
            else:
                idx, event_date = entry, None  # tolerate old int-list shape if LLM reverts to it
            if isinstance(idx, int) and 0 <= idx < len(articles):
                kept.append((idx, event_date))

        # Deterministic backstop for issue #12: don't trust the LLM's own keep/skip
        # judgment to have already applied its extracted event_date — enforce it in
        # code. Articles whose event_date is unparseable/absent are kept unchanged
        # (matches rule 1's "无法判断则保留").
        event_cutoff = datetime.now() - timedelta(days=EVENT_MAX_AGE_DAYS)
        filtered = []
        event_dropped = 0
        for idx, event_date in kept:
            event_dt = _parse_pub_date(event_date) if event_date else None
            if event_dt and event_dt < event_cutoff:
                logger.info(f"[{company_zh}] Event-date filter: dropped '{articles[idx]['title'][:55]}' (event={event_date})")
                event_dropped += 1
                continue
            filtered.append(articles[idx])
        if event_dropped:
            logger.info(f"[{company_zh}] Event-date filter: {len(kept)} → {len(filtered)} ({event_dropped} dropped, >{EVENT_MAX_AGE_DAYS}d)")

        length_hint = int(result.get("length_hint", 400))
        logger.info(f"[{company_zh}] Prefilter: {len(articles)} → {len(filtered)} articles, hint={length_hint}w")
        return filtered, length_hint, "ok"
    except Exception as e:
        logger.warning(f"[{company_zh}] Prefilter failed (pass-through): {e}")
        return articles, 400, "llm_failed_passthrough"


# ── LLM synthesis (V4 Pro) ────────────────────────────────────────────────────

def synthesize_with_llm(
    company: str,
    new_context: str,
    historical_context: str = "",
    kb_context: str = "",
    last_week_section: str = "",
    length_hint: int = 400,
) -> tuple:
    last_week_block = ""
    if last_week_section:
        last_week_block = (
            f"\n\n【上周报告（{company}）】— 以下内容已报道，本周勿重复，仅在有具体新进展时更新：\n\n"
            f"{last_week_section}"
        )

    history_section = ""
    if historical_context:
        history_section = f"\n\n【历史留存情报（仅供背景参考）】\n\n{historical_context}"

    kb_section = ""
    if kb_context:
        kb_section = f"\n\n【个人知识库上下文（MemPalace / Obsidian / KG）】\n\n{kb_context}"

    prompt = f"""以下是关于 {company} 本周新获取的情报（过去{SEARCH_DAYS}天内）：

{new_context}{last_week_block}{history_section}{kb_section}

---

分析立场：国际顶尖IT咨询公司合伙人，目标是识别可行动的咨询机会、评估客户付费意愿与能力、规避商务风险。

**核心规则**：只写相对于上周报告真正新增的内容。已在上周完整报道、本周无进展的事件，完全省略，不要复述。如果本周确实无实质新动态，仅输出一句"本周{company}无实质新动态。"，不补充旧信息，不填充模板。

用中文，书面报告语气，第三人称，禁止列举符号（•、-、*）和数字序号，全篇约{length_hint}字。

按以下逻辑输出，没有内容的节完全省略（包括节标题）：

### 本周新动态
（仅有真正新增事件时输出）用独立段落表述各项动态：发生了什么 + 为何此时发生的机制判断 + 实质影响是什么。不描述表面现象，要给出因果和结构性分析。

### 财务数据更新
（仅含本期新出现的具体数字，不重复上周已报告的数据）数据具体，说明同比/环比变化。

### 对IT咨询业务的判断
（仅在有具体事件驱动时输出，不泛化）必须回答：哪个具体事件触发了哪类IT需求？需求紧迫程度与时间窗口？该公司当前付费意愿和预算能力的变化方向？主要风险？"""

    data, err = post_with_retry(
        DEEPSEEK_BASE_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json_body={
            "model": LLM_MODEL_PRO,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8000,
            "reasoning_effort": "high",
        },
        timeout=300,
    )
    if err:
        return None, {}
    usage = data.get("usage", {})
    content = extract_llm_text(data["choices"][0]["message"])
    return content, usage


# ── Telegram alert ───────────────────────────────────────────────────────────

def _send_telegram_alert(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = (
        os.getenv("TELEGRAM_HOME_CHANNEL")
        or os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")[0].strip()
    )
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram alert failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_intel(recipients: list[str] | None = None, force: bool = False):
    logger.info(f"Starting intelligence pull (force={force})...")
    _validate_intel_config()  # 缺失关键配置直接抛出，不落入下面逻辑被静默吞掉（issue #11）

    date_str = datetime.now().strftime("%Y-%m-%d")
    uid = f"HRM-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    companies = get_companies_full()
    if not companies:
        logger.error("No companies in config, skipping")
        return

    seen_urls = _load_seen_urls()
    fetch_log = _load_fetch_log()
    sections = []
    total_input = total_output = 0

    for company in companies:
        zh = company["zh"]
        if not force and fetch_log.get(zh) == date_str:
            logger.info(f"[{zh}] Already fetched today, skipping")
            continue

        # Load previous week's report section for cross-week dedup
        last_week_section = load_last_report_section(zh) if not force else ""
        if last_week_section:
            logger.info(f"[{zh}] Last report section loaded ({len(last_week_section)} chars)")

        # Snapshot historical cache BEFORE fetch to avoid same-batch mutual L2 drops
        historical_cache = get_articles_by_company(zh) if not force else []
        logger.info(f"[{zh}] Fetching news...")
        raw_results, funnel = fetch_company_raw(company)
        if raw_results is None:
            logger.warning(f"[{zh}] Search provider failed, skipping (will retry next run)")
            logger.info(f"[{zh}] FUNNEL {json.dumps(funnel, ensure_ascii=False)}")
            continue

        if not force:
            new_results = [r for r in raw_results if r["url"] not in seen_urls]
        else:
            new_results = raw_results
        funnel["after_l1"] = len(new_results)

        # L2: title Jaccard similarity — catches same story at a different URL
        if not force and new_results:
            l2_kept, l2_skipped = [], 0
            for r in new_results:
                is_dup, score, match = find_cache_duplicate(r["url"], r["title"], historical_cache)
                if is_dup:
                    logger.info(f"[{zh}] L2 dedup: '{r['title'][:55]}' "
                                f"(j={score:.2f} ← '{match[:40]}')")
                    l2_skipped += 1
                else:
                    l2_kept.append(r)
            if l2_skipped:
                logger.info(f"[{zh}] L2 title dedup: {len(new_results)} → {len(l2_kept)} "
                            f"({l2_skipped} removed)")
            new_results = l2_kept
        funnel["after_l2"] = len(new_results)

        if not new_results:
            logger.info(f"[{zh}] No new URLs after dedup, skipping section")
            logger.info(f"[{zh}] FUNNEL {json.dumps(funnel, ensure_ascii=False)}")
            fetch_log[zh] = date_str
            continue

        # Prefilter: V4 Flash quality + cross-week dedup gate
        new_results, length_hint, prefilter_status = prefilter_articles(zh, new_results, last_week_section)
        funnel["after_prefilter"] = len(new_results)
        funnel["prefilter_status"] = prefilter_status
        if not new_results:
            logger.info(f"[{zh}] Prefilter: no actionable intel, skipping section")
            logger.info(f"[{zh}] FUNNEL {json.dumps(funnel, ensure_ascii=False)}")
            fetch_log[zh] = date_str
            continue

        logger.info(f"[{zh}] {len(new_results)} articles after prefilter, synthesizing with V4 Pro...")
        new_context = "\n\n---\n\n".join(
            f"标题：{r['title']}\n内容：{r['content']}\n来源：{r['url']}"
            for r in new_results
        )
        # Historical cache context (background only, not primary source)
        new_urls = {r["url"] for r in new_results}
        historical = [a for a in get_articles_by_company(zh) if a["url"] not in new_urls]
        historical_context = "\n\n---\n\n".join(
            f"标题：{a['title']}\n内容：{a['content'][:300]}\n来源：{a['url']}"
            for a in historical[:8]
        )
        # Personal knowledge base: MemPalace + Obsidian
        kb_context = get_company_context(zh, company.get("en", ""))
        if kb_context:
            logger.info(f"[{zh}] KB context: {len(kb_context)} chars injected")

        summary, usage = synthesize_with_llm(
            zh, new_context, historical_context, kb_context,
            last_week_section=last_week_section,
            length_hint=length_hint,
        )
        if summary is None:
            logger.warning(f"[{zh}] LLM synthesis failed after retries, skipping section")
            logger.info(f"[{zh}] FUNNEL {json.dumps(funnel, ensure_ascii=False)}")
            continue

        in_t = usage.get("prompt_tokens", 0)
        out_t = usage.get("completion_tokens", 0)
        total_input += in_t
        total_output += out_t
        logger.info(f"[{zh}] tokens: in={in_t} out={out_t}")
        logger.info(f"[{zh}] FUNNEL {json.dumps(funnel, ensure_ascii=False)}")
        sections.append(f"## {zh}\n\n{summary}\n\n---")

        if not force:
            ts = datetime.now().timestamp()
            for r in new_results:
                seen_urls[r["url"]] = {"ts": ts, "company": zh}
            fetch_log[zh] = date_str

    if not force:
        _save_seen_urls(seen_urls)
        _save_fetch_log(fetch_log)

    if not sections:
        logger.info("No new intel for any company — sending notification email.")
        sid = send_report(
            subject=f"[Hermes MI] 本周无新情报 {date_str}",
            markdown_body=(
                f"# 本周无新情报\n\n"
                f"所有监控企业（{', '.join(c['zh'] for c in companies)}）"
                f"均未发现实质新内容，情报已是最新。\n\n任务ID：{uid}\n{get_footer()}"
            ),
            recipients=get_recipients(),
        )
        if sid:
            _save_processed_id(sid)
        _send_telegram_alert(
            f"[Hermes MI] 本周无新情报 {date_str}\n"
            f"监控企业：{len(companies)} 家，均无新内容\n"
            f"任务ID：{uid}"
        )
        post_slack_report(
            f"*[Hermes MI] 本周无新情报 {date_str}*\n"
            f"监控企业 {len(companies)} 家均无新内容。任务ID：{uid}"
        )
        return

    # V4 Pro permanent pricing (promotion made permanent): $0.435/M input, $0.87/M output
    cost = total_input / 1e6 * 0.435 + total_output / 1e6 * 0.87
    logger.info(f"Total tokens: in={total_input} out={total_output} | est. cost=${cost:.4f}")

    output_dir = OBSIDIAN_DIR / "Hermes" / "MI"
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"{date_str}-china-companies.md"

    body = f"""---
date: {date_str}
tags: [intelligence, china-companies]
---

# 中国企业情报日报 {date_str}

> 搜索层：Tavily (topic=general, days={SEARCH_DAYS}) + Serper News (CN) | 推理层：{LLM_MODEL_PRO} (synthesis) / {LLM_MODEL_FLASH} (prefilter) via DeepSeek API
> 生成时间：{datetime.now(SGT).strftime("%Y-%m-%d %H:%M SGT")}{"（force）" if force else ""} | tokens: in={total_input} out={total_output} | ~${cost:.4f}

{chr(10).join(sections)}

---
任务ID：{uid}
{get_footer()}"""

    filepath.write_text(body, encoding="utf-8")
    logger.info(f"Intel written → {filepath}")

    target_recipients = recipients if recipients is not None else get_recipients()
    sid = send_report(
        subject=f"[Hermes MI] 中国企业情报日报 {date_str}",
        markdown_body=body,
        recipients=target_recipients,
    )
    if sid:
        _save_processed_id(sid)
    logger.info(f"Intel sent → {target_recipients}")
    _send_telegram_alert(
        f"[Hermes MI] 情报日报 {date_str}\n"
        f"有新情报：{len(sections)}/{len(companies)} 家企业\n"
        f"tokens: in={total_input} out={total_output} | ~${cost:.4f}\n"
        f"任务ID：{uid}"
    )
    post_slack_report(body)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Bypass dedup and per-day rate limit; does not update seen_urls")
    parser.add_argument("--recipients", default=None,
                        help="Override recipient list (comma-separated emails)")
    args = parser.parse_args()
    from log_utils import setup_logging
    setup_logging("intel")
    override = [r.strip() for r in args.recipients.split(",")] if args.recipients else None
    try:
        run_intel(force=args.force, recipients=override)
    except Exception:
        logger.exception("run_intel crashed")
        raise
