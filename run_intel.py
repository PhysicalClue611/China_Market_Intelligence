#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json
import logging
import os
import errno
from datetime import datetime, timezone, timedelta
from pathlib import Path

import time

import httpx

from email_sender import send_report
from slack_sender import post_report as post_slack_report
from config_store import get_companies_full, get_recipients
from hermes_footer import get_footer
from memory_context import get_company_context
from intel_sources import (ENTITY_MAP, RULES, SourceStats, collect_company,
                           collect_industry, fetch_eastmoney_body)
from intel_clean import (OR_ATTRIBUTION_HEADERS, clean_company, classify_two_stage,
                         in_window, is_farm, is_low_value, dedupe, published_key)
from intel_synth import compose_section, build_material

logger = logging.getLogger(__name__)


class IntelConfigError(RuntimeError):
    """必需的情报配置缺失，属于部署错误，不应被当作单次抓取失败静默吞掉（exit 0）。"""


def _validate_intel_config():
    """启动时校验必需环境变量非空，缺失则直接抛出（fail fast），日志只报变量名不报值。"""
    required = {
        "EXA_API_KEY": EXA_API_KEY,
        "OPENROUTER_API_KEY": OPENROUTER_API_KEY,
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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
EXA_API_KEY = os.getenv("EXA_API_KEY", "")

SEEN_URLS_PATH = DATA_DIR / "seen_urls.json"
FETCH_LOG_PATH = DATA_DIR / "fetch_log.json"
SEEN_URL_TTL_DAYS = 90

EINTR_MAX_ATTEMPTS = 4     # iCloud scandir EINTR retries before degrading (issue #15)
EINTR_RETRY_SLEEP = 0.1    # seconds between retries

SGT = timezone(timedelta(hours=8))


# ── Last report section loader ─────────────────────────────────────────────────

def _glob_report_files(report_dir: Path) -> list | None:
    """Sorted glob of *-china-companies.md, retrying transient EINTR.

    The Obsidian vault lives on iCloud Drive; the file provider can interrupt
    os.scandir mid-iteration, which CPython's Path.glob surfaces as
    InterruptedError (issue #15). Returns None when retries are exhausted so
    the caller can degrade instead of killing the whole run.
    """
    for attempt in range(1, EINTR_MAX_ATTEMPTS + 1):
        try:
            return sorted(report_dir.glob("*-china-companies.md"), reverse=True)
        except InterruptedError:
            pass
        except OSError as e:
            if e.errno != errno.EINTR:
                raise
        logger.warning(
            "Report dir glob interrupted (EINTR), attempt %d/%d: %s",
            attempt, EINTR_MAX_ATTEMPTS, report_dir,
        )
        if attempt < EINTR_MAX_ATTEMPTS:
            time.sleep(EINTR_RETRY_SLEEP)
    return None


def load_last_report_section(company_zh: str) -> str:
    """Extract company_zh's section from the most recent previous weekly report.
    Returns "" if no previous report or company not found.
    """
    report_dir = OBSIDIAN_DIR / "Hermes" / "MI"
    today = datetime.now().strftime("%Y-%m-%d")

    reports = _glob_report_files(report_dir)
    if reports is None:
        logger.error(
            "[%s] Report dir glob failed after %d attempts (EINTR), "
            "skipping cross-week dedup",
            company_zh, EINTR_MAX_ATTEMPTS,
        )
        return ""
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
    if len(section) > RULES["synth"]["last_week_chars"]:
        section = section[:RULES["synth"]["last_week_chars"]] + "\n[...截断]"
    return section


# ── Dedup state ───────────────────────────────────────────────────────────────

def _load_seen_urls(*, persist_prune: bool = True) -> dict:
    if not SEEN_URLS_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_URLS_PATH.read_text())
        cutoff = datetime.now().timestamp() - SEEN_URL_TTL_DAYS * 86400
        valid = {url: meta for url, meta in data.items() if meta.get("ts", 0) >= cutoff}
        if persist_prune and len(valid) != len(data):
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

def run_intel(recipients: list[str] | None = None, force: bool = False, sample: bool = False):
    logger.info('Starting intelligence pull (force=%s, sample=%s)...', force, sample)
    _validate_intel_config()
    date_str = datetime.now().strftime('%Y-%m-%d')
    uid = f"HRM-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    companies = get_companies_full()
    if not companies:
        logger.error('No companies in config, skipping')
        return
    seen_urls = _load_seen_urls(persist_prune=not sample)
    fetch_log = _load_fetch_log()
    sections, failed_companies = [], []
    since_dt = datetime.now(timezone.utc) - timedelta(days=RULES['window_days'])
    stats = SourceStats()
    llm_cost = 0.0

    def add_cost(cost):
        nonlocal llm_cost
        llm_cost += cost

    industry_results = {}
    industry_names = dict.fromkeys(name for company in companies
        for name in ENTITY_MAP.get(company['zh'], {}).get('industries', []))
    for name in industry_names:
        try:
            items = collect_industry(name, RULES, since_dt, stats)
            items = [row for row in items if in_window(row, since_dt)
                     and (force or row['url'] not in seen_urls)
                     and not is_farm(row, RULES) and not is_low_value(row['title'], RULES)]
            items, _ = dedupe(items)
            industry_results[name] = classify_two_stage(name, items, RULES, 'industry', cost_cb=add_cost)
        except Exception:
            logger.exception('[%s] Industry collection/classification failed; continuing without industry context', name)
            industry_results[name] = []

    for company in companies:
        zh, en = company['zh'], company.get('en', '')
        try:
            if not force and not sample and fetch_log.get(zh) == date_str:
                logger.info('[%s] Already fetched today, skipping', zh)
                continue
            last_week = load_last_report_section(zh) if not force else ''
            group_cfg = ENTITY_MAP.get(zh)
            group = group_cfg or {'aliases': [a for a in (zh, en) if a], 'entities': [], 'industries': []}
            funnel = {key: 0 for key in ('raw', 'raw_eastmoney', 'raw_gnews', 'raw_exa', 'undated',
                'window', 'after_window', 'seen', 'after_seen', 'farm', 'after_farm', 'routine', 'after_routine',
                'low_value', 'after_low_value', 'no_entity', 'after_entity', 'deduped', 'after_dedupe',
                'classify_keep', 'classify_mention', 'classify_dropped', 'classify_failed',
                'filings_total', 'filings_bodies', 'material_chars')}
            items = collect_company(group_cfg, zh, en, RULES, since_dt, stats)
            if items is None:
                logger.warning('[%s] all sources failed, will retry next run', zh)
                logger.info('[%s] FUNNEL %s', zh, json.dumps(funnel, ensure_ascii=False))
                continue
            filings, others, clean_funnel = clean_company(items, group, RULES, {} if force else seen_urls, since_dt)
            funnel.update(clean_funnel)
            before_classify = len(others)
            others = classify_two_stage(zh, others, RULES, 'company', cost_cb=add_cost)
            funnel.update(classify_keep=sum(row['label'] == 'keep' for row in others),
                          classify_mention=sum(row['label'] == 'mention' for row in others),
                          classify_dropped=before_classify - len(others),
                          classify_failed=getattr(others, 'classify_failed', 0))
            for row in sorted(filings, key=published_key, reverse=True)[:RULES['filings']['bodies_max']]:
                art_code = row['url'].rsplit('/', 1)[-1].removesuffix('.html')
                row['body'] = fetch_eastmoney_body(art_code, stats)
                funnel['filings_bodies'] += bool(row['body'])
            if not filings and not others:
                logger.info('[%s] no new intel', zh)
                logger.info('[%s] FUNNEL %s', zh, json.dumps(funnel, ensure_ascii=False))
                if not sample:
                    fetch_log[zh] = date_str
                continue
            kb = get_company_context(zh, en)
            industry_items = [row for name in group.get('industries', []) for row in industry_results.get(name, [])]
            material = build_material(zh, group, filings, others, industry_items, RULES)
            funnel['material_chars'] = len(material)
            text = compose_section(zh, en, material, last_week, kb, RULES, cost_cb=add_cost)
            if text is None:
                logger.warning('[%s] LLM synthesis failed after retries, skipping section', zh)
                logger.info('[%s] FUNNEL %s', zh, json.dumps(funnel, ensure_ascii=False))
                continue
            sections.append(f'## {zh}\n\n{text}')
            logger.info('[%s] FUNNEL %s', zh, json.dumps(funnel, ensure_ascii=False))
            if not force and not sample:
                ts = datetime.now().timestamp()
                for row in filings + others:
                    seen_urls[row['url']] = {'ts': ts, 'company': zh}
                fetch_log[zh] = date_str
        except IntelConfigError:
            raise
        except Exception:
            failed_companies.append(zh)
            logger.exception('[%s] Unexpected error processing company, skipping', zh)
            continue

    if not force and not sample:
        _save_seen_urls(seen_urls)
        _save_fetch_log(fetch_log)
    if not sections:
        if failed_companies:
            logger.error('No sections produced; %d/%d companies failed with errors (%s) — '
                         "not sending 'no new intel' success notification",
                         len(failed_companies), len(companies), ', '.join(failed_companies))
            raise RuntimeError('run_intel: '
                f"{len(failed_companies)}/{len(companies)} companies failed "
                f"with errors ({', '.join(failed_companies)}) — no sections produced, not sending 'no new intel'")
        if sample:
            logger.info('Sample: 本周无新情报; sending nothing')
            return
        logger.info('No new intel for any company — sending notification email.')
        sid = send_report(subject=f'[Hermes MI] 本周无新情报 {date_str}', markdown_body=(
            f"# 本周无新情报\n\n所有监控企业（{', '.join(c['zh'] for c in companies)}）"
            f'均未发现实质新内容，情报已是最新。\n\n任务ID：{uid}\n{get_footer()}'), recipients=get_recipients())
        if sid:
            _save_processed_id(sid)
        _send_telegram_alert(f'[Hermes MI] 本周无新情报 {date_str}\n'
            f'监控企业：{len(companies)} 家，均无新内容\n任务ID：{uid}')
        post_slack_report(f'*[Hermes MI] 本周无新情报 {date_str}*\n'
            f'监控企业 {len(companies)} 家均无新内容。任务ID：{uid}')
        return

    cost = llm_cost + stats.exa_cost_usd
    failed_note = f"\n> [!] 跳过（处理异常）：{'、'.join(failed_companies)}\n" if failed_companies else ''
    sample_line = ('\n> 样文：新采集与合成流程的试运行，用于展示今后周报的样子，不计入正式周报的去重。' if sample else '')
    screen, synth = RULES['classify'], RULES['synth']
    # Sections are separated by one rule; load_last_report_section relies on it ending a section.
    joined = '\n\n---\n\n'.join(sections)
    footer = get_footer()
    # Archive (Obsidian): frontmatter, pipeline/cost header and task id for operations.
    body = f'''---
date: {date_str}
tags: [intelligence, china-companies]
---

# 中国企业情报周报 {date_str}{sample_line}

> 采集：东方财富公告 + Google News + Exa | 筛选：{screen['screen_model']} + {screen['review_model']} | 合成：{synth['draft_model']}（{synth['check_model']} 核查）
> 生成时间：{datetime.now(SGT).strftime('%Y-%m-%d %H:%M SGT')}{'（force）' if force else ''} | 费用 ~${cost:.4f}
{failed_note}
{joined}

---
任务ID：{uid}{footer}'''
    # Email/Slack (readers): title, sections and footer only.
    email_body = f'''# 中国企业情报周报 {date_str}{sample_line}
{failed_note}
{joined}{footer}'''
    if not sample:
        output_dir = OBSIDIAN_DIR / 'Hermes' / 'MI'
        output_dir.mkdir(parents=True, exist_ok=True)
        filepath = output_dir / f'{date_str}-china-companies.md'
        filepath.write_text(body, encoding='utf-8')
        logger.info('Intel written → %s', filepath)
    report_subject = f"{'[Hermes MI][样文]' if sample else '[Hermes MI]'} 中国企业情报周报 {date_str}"
    if failed_companies:
        report_subject += f"（跳过：{'、'.join(failed_companies)}）"
    target_recipients = recipients if recipients is not None else get_recipients()
    sid = send_report(subject=report_subject, markdown_body=email_body, recipients=target_recipients)
    logger.info('Intel sent → %s', target_recipients)
    if not sample:
        if sid:
            _save_processed_id(sid)
        tg_alert = (f'[Hermes MI] 情报周报 {date_str}\n有新情报：{len(sections)}/{len(companies)} 家企业\n'
                    f'费用 ~${cost:.4f}\n任务ID：{uid}')
        if failed_companies:
            tg_alert += f"\n跳过（处理异常）：{'、'.join(failed_companies)}"
        _send_telegram_alert(tg_alert)
        post_slack_report(email_body)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help='Bypass dedup and per-day rate limit; does not update seen_urls')
    parser.add_argument('--sample', action='store_true', help='Email a sample without publishing or saving dedup state')
    parser.add_argument('--recipients', default=None, help='Override recipient list (comma-separated emails)')
    args = parser.parse_args()
    from log_utils import setup_logging
    setup_logging('intel')
    override = [r.strip() for r in args.recipients.split(',')] if args.recipients else None
    try:
        run_intel(force=args.force, recipients=override, sample=args.sample)
    except Exception:
        logger.exception('run_intel crashed')
        raise
