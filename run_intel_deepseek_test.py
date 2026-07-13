#!/usr/bin/env python3
"""
一次性测试脚本：用 deepseek/deepseek-v4-flash 对所有监控公司做全量分析。
- 从 article_cache.json 读取已持久化内容，不重新调 Tavily
- 如果某公司缓存为空，回退到 Tavily 拉取并存入缓存
- 不更新 seen_urls / fetch_log / Obsidian
- 发送到 TEST_RECIPIENT（从环境变量 TEST_RECIPIENT 读取）
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import logging
from datetime import datetime, timezone, timedelta

import httpx

from email_sender import send_report
from config_store import get_companies_full
from hermes_footer import get_footer
from article_cache import get_articles_by_company, save_articles

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://github.com/PhysicalClue611/China_Market_Intelligence",
    "X-OpenRouter-Title": "MI",
}
SGT = timezone(timedelta(hours=8))

LLM_MODEL = "deepseek/deepseek-v4-flash"
TEST_RECIPIENT = os.getenv("TEST_RECIPIENT", "")

SYSTEM_PROMPT = (
    "你是一名国际顶尖IT咨询公司的合伙人，专注于拓展战略咨询与IT咨询业务，"
    "包括接触高层领导、发掘咨询机会、引领咨询项目签约及落地，以及后续实施交付和收款工作。"
    "你的分析始终服务于识别潜在咨询机会、评估客户付费意愿与能力、以及规避商务风险。"
)


def tavily_fetch_and_cache(company: dict) -> list[dict]:
    """缓存为空时回退：从 Tavily 拉取并存入缓存。"""
    zh, en = company["zh"], company["en"]
    query = f"{en} {zh} latest news strategy financials" if en else f"{zh} 最新动态 战略 合作 财报 业绩"
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": TAVILY_API_KEY,
            "query": query,
            "topic": "general",
            "max_results": 20,
            "days": 30,
            "include_answer": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    results = []
    for r in resp.json().get("results", []):
        results.append({
            "title": r["title"],
            "content": r.get("content", ""),
            "url": r["url"],
        })
    zh_kw, en_kw = zh[:2], (en.split()[0].lower() if en else "")
    relevant = [
        r for r in results
        if zh_kw in r["title"] or zh_kw in r["content"]
        or (en_kw and (en_kw in r["title"].lower() or en_kw in r["content"].lower()))
    ]
    if relevant:
        save_articles(relevant, zh)
    return relevant


def synthesize(company_zh: str, articles: list[dict]) -> str:
    raw_context = "\n\n---\n\n".join(
        f"标题：{a['title']}\n内容：{a['content']}\n来源：{a['url']}"
        for a in articles
    )
    prompt = f"""以下是过去90天内关于 {company_zh} 的全量已收录情报（共 {len(articles)} 条）：

{raw_context}

请基于以上内容，用中文撰写商业情报报告，严格按以下结构输出：

### 近期重要动态

列出3-5条最重要的动态（合作/并购/战略/产品/人事变化优先）。
每条动态单独一段，约100字，说明事件本身、背景、预期影响，不要只列标题。

### 财务与业务数据

关键财务指标（营收、利润、增速）及核心业务表现，数据要具体。

### IT咨询视角：机会与风险

从IT咨询公司角度分析（约200字）：
- **咨询机会**：该公司的战略动态为哪类IT咨询项目创造需求？（具体方向）
- **财务风险信号**：有无值得关注的财务健康风险？

只基于搜索结果中有据可查的信息，不要补充推测。"""

    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            **OR_ATTRIBUTION_HEADERS,
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 8000,
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    usage = data.get("usage", {})
    msg = data["choices"][0]["message"]
    content = msg.get("content") or msg.get("reasoning") or ""
    return content.strip(), usage


def main():
    companies = get_companies_full()
    date_str = datetime.now(SGT).strftime("%Y-%m-%d")
    sections = []
    total_input = total_output = 0

    for company in companies:
        zh = company["zh"]

        articles = get_articles_by_company(zh)
        if not articles:
            logger.info(f"[{zh}] Cache empty, falling back to Tavily...")
            articles = tavily_fetch_and_cache(company)

        if not articles:
            logger.info(f"[{zh}] No data available, skipping")
            continue

        logger.info(f"[{zh}] Synthesizing {len(articles)} cached articles...")
        try:
            summary, usage = synthesize(zh, articles)
        except Exception as e:
            logger.error(f"[{zh}] LLM failed: {e}")
            continue

        input_t = usage.get("prompt_tokens", 0)
        output_t = usage.get("completion_tokens", 0)
        total_input += input_t
        total_output += output_t
        logger.info(f"[{zh}] tokens: in={input_t} out={output_t}")
        sections.append(f"## {zh}\n\n{summary}")

    if not sections:
        logger.error("No sections generated, aborting")
        return

    cost = total_input / 1e6 * 0.14 + total_output / 1e6 * 0.28
    uid = f"HRM-TEST-{datetime.now(SGT).strftime('%Y%m%d-%H%M%S')}"

    body = f"""---
date: {date_str}
tags: [intelligence, china-companies, deepseek-test]
---

# 中国企业情报 · DeepSeek V4 Flash 测试 {date_str}

> 推理层：{LLM_MODEL} via OpenRouter | 数据来源：article_cache（90天留存）
> 生成时间：{datetime.now(SGT).strftime("%Y-%m-%d %H:%M SGT")}
> 实际 tokens：输入 {total_input:,} / 输出 {total_output:,} | 实际成本：~${cost:.4f}

{chr(10).join(sections)}

---
任务ID：{uid}
{get_footer()}"""

    logger.info(f"Sending to {TEST_RECIPIENT}...")
    sid = send_report(
        subject=f"[Hermes MI · DeepSeek Test] 中国企业情报 {date_str}",
        markdown_body=body,
        recipients=[TEST_RECIPIENT],
    )
    logger.info(f"Done. sent_id={sid} | cost=${cost:.4f}")


if __name__ == "__main__":
    main()
