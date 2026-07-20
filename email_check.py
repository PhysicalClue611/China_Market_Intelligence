#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from http_utils import post_with_retry, extract_llm_text, call_llm_json
import config_store
from email_sender import send_report
from hermes_footer import get_footer
from search_utils import search as search_news

logger = logging.getLogger(__name__)

_background_threads: list = []

JMAP_BASE = os.getenv("JMAP_BASE", "")
JMAP_ACCOUNT_ID = os.getenv("JMAP_ACCOUNT_ID", "")
JMAP_INBOX_ID = os.getenv("JMAP_INBOX_ID", "")
STALWART_API_KEY = os.getenv("STALWART_API_KEY", "")

_REQUIRED_JMAP_VARS = {
    "JMAP_BASE": JMAP_BASE,
    "JMAP_ACCOUNT_ID": JMAP_ACCOUNT_ID,
    "JMAP_INBOX_ID": JMAP_INBOX_ID,
    "STALWART_API_KEY": STALWART_API_KEY,
}


class JMAPConfigError(RuntimeError):
    """必需的 JMAP 配置缺失，属于部署错误，不应被当作普通轮询失败吞掉。"""


def _validate_jmap_config():
    """启动时校验必需 JMAP 变量非空，缺失则直接抛出（fail fast），日志只报变量名不报值。"""
    missing = [name for name, value in _REQUIRED_JMAP_VARS.items() if not value]
    if missing:
        raise JMAPConfigError(
            f"Missing required JMAP config: {', '.join(missing)} — check .env"
        )

DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
LOG_PATH = DATA_DIR / "email_actions.log"
PROCESSED_IDS_PATH = DATA_DIR / "processed_email_ids.json"
CURSOR_PATH = DATA_DIR / "jmap_cursor.json"
CURSOR_OVERLAP_MINUTES = 10  # 重叠窗口，防止游标边界的时钟误差漏信；重复邮件靠 processed_ids 去重


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


def _load_processed_ids() -> set:
    """加载已处理 ID，自动丢弃超过 72 小时的记录，返回有效 ID 集合。"""
    if not PROCESSED_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(PROCESSED_IDS_PATH.read_text())
        cutoff = datetime.now().timestamp() - 72 * 3600
        valid = {mid: ts for mid, ts in data.items() if ts >= cutoff}
        if len(valid) != len(data):
            PROCESSED_IDS_PATH.write_text(json.dumps(valid))
        return set(valid.keys())
    except Exception:
        return set()


def _save_processed_id(msg_id):
    """记录已处理 ID，附带当前时间戳。接受字符串或 ID 列表。"""
    if isinstance(msg_id, list):
        for mid in msg_id:
            _save_processed_id(mid)
        return
    try:
        data = json.loads(PROCESSED_IDS_PATH.read_text()) if PROCESSED_IDS_PATH.exists() else {}
    except Exception:
        data = {}
    data[msg_id] = datetime.now().timestamp()
    PROCESSED_IDS_PATH.write_text(json.dumps(data))


def _load_cursor(default_hours: int = 24) -> str:
    """加载上次成功轮询的游标（最新 receivedAt）。缺失/损坏时退化为过去 default_hours 小时。"""
    if CURSOR_PATH.exists():
        try:
            data = json.loads(CURSOR_PATH.read_text())
            ts = data.get("last_received_at")
            if ts:
                return ts
        except Exception as e:
            logger.warning(f"Cursor read failed, falling back to {default_hours}h window: {e}")
    return (datetime.now(timezone.utc) - timedelta(hours=default_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _save_cursor(ts: str):
    """原子写入游标：写临时文件再 os.replace，避免中途崩溃截断文件。"""
    tmp = CURSOR_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"last_received_at": ts}))
    os.replace(tmp, CURSOR_PATH)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OR_ATTRIBUTION_HEADERS = {
    "HTTP-Referer": "https://github.com/PhysicalClue611/China_Market_Intelligence",
    "X-OpenRouter-Title": "MI",
}
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
LLM_MODEL = "openai/gpt-oss-20b"
LLM_MODEL_REASON = "deepseek-v4-pro"  # Followup reasoning (step 3) — DeepSeek direct API

SYSTEM_PROMPT = (
    "你是一名国际顶尖IT咨询公司的合伙人，专注于拓展战略咨询与IT咨询业务，"
    "包括接触高层领导、发掘咨询机会、引领咨询项目签约及落地，以及后续实施交付和收款工作。"
    "你的分析始终服务于识别潜在咨询机会、评估客户付费意愿与能力、以及规避商务风险。"
)


# ── JMAP helpers ─────────────────────────────────────────────────────────────

def _jmap_request(method_calls: list) -> list:
    resp = httpx.post(
        f"{JMAP_BASE}/jmap/",
        headers={"Authorization": f"Bearer {STALWART_API_KEY}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": method_calls,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["methodResponses"]


def _jmap_fetch_inbox(after: str, limit: int = 50) -> list[dict]:
    """拉取 after 之后的全部邮件，按 receivedAt 升序分页直到取尽（不再受单次 50 条上限截断）。"""
    all_emails: list[dict] = []
    position = 0
    while True:
        responses = _jmap_request([
            ["Email/query", {
                "accountId": JMAP_ACCOUNT_ID,
                "filter": {"inMailbox": JMAP_INBOX_ID, "after": after},
                "sort": [{"property": "receivedAt", "isAscending": True}],
                "position": position,
                "limit": limit,
            }, "q0"],
            ["Email/get", {
                "accountId": JMAP_ACCOUNT_ID,
                "#ids": {"resultOf": "q0", "name": "Email/query", "path": "/ids"},
                "properties": ["subject", "from", "messageId", "receivedAt", "textBody", "bodyValues"],
                "fetchTextBodyValues": True,
                "maxBodyValueBytes": 50000,
            }, "g0"],
        ])
        batch = responses[1][1].get("list", [])
        all_emails.extend(batch)
        if len(batch) < limit:
            break
        position += limit
    return all_emails


def _jmap_body(email: dict) -> str:
    body_values = email.get("bodyValues", {})
    for part in email.get("textBody", []):
        pid = part.get("partId")
        if pid and pid in body_values:
            return body_values[pid].get("value", "")
    return ""


# ── Message parsing ───────────────────────────────────────────────────────────

def _find_uid(text: str) -> str | None:
    match = re.search(r"HRM-\d{8}-\d{6}", text)
    return match.group(0) if match else None


# ── LLM command parsing ───────────────────────────────────────────────────────

def _parse_command(email_text: str) -> dict:
    prompt = f"""你是 Hermes 邮件指令解析器。用户发来的邮件内容如下：

{email_text}

请分析用户意图，输出 JSON 格式（只输出 JSON，不要其他内容）：
- 增加监控公司：{{"action": "add_company", "targets": ["公司名1"]}}
- 删除监控公司：{{"action": "remove_company", "targets": ["公司名1"]}}
- 增加收件人：{{"action": "add_recipient", "targets": ["email@domain.com"]}}
- 删除收件人：{{"action": "remove_recipient", "targets": ["email@domain.com"]}}
- 查看/汇报当前状态、配置、服务健康：{{"action": "status_report"}}
- 立即发送/生成情报日报（如"现在发情报"、"立即推送"、"发一次日报"）：{{"action": "run_intel"}}
- 分析问题、跟进提问、观点请求（非配置操作）：{{"action": "followup_question"}}
- 无法识别：{{"action": "unknown"}}"""

    result = call_llm_json(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", **OR_ATTRIBUTION_HEADERS},
        json_body={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "provider": {
                "order": ["Inceptron", "AkashML", "Nebius", "NovitaAI", "Parasail"],
                "allow_fallbacks": True,
            },
            "response_format": {"type": "json_object"},
        },
        timeout=30,
        logger=logger,
        label="ParseCommand",
    )
    return result if result is not None else {"action": "unknown"}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_quoted_lines(text: str) -> str:
    """去掉引用行和邮件客户端的 'On ... wrote:' 分隔线，保留用户新写内容。"""
    result = []
    for line in text.splitlines():
        stripped = line.strip()
        # 引用行
        if stripped.startswith(">"):
            continue
        # 邮件客户端引用分隔符
        if re.match(r"^On .{10,} wrote:$", stripped):
            break
        # Hermes footer 分隔符（不把 footer 传给 LLM）
        if stripped == "---":
            break
        result.append(line)
    return "\n".join(result).strip()


def _followup_three_stage(question: str) -> str:
    """Three-stage followup: 分析 → 收集情报 → 推理 (V4 Pro)."""
    provider_cfg = {
        "order": ["Inceptron", "AkashML", "Nebius", "NovitaAI", "Parasail"],
        "allow_fallbacks": True,
    }

    # Stage 1 — 分析: intent + search queries (V4 Flash, cheap)
    stage1_prompt = f"""用户发来的问题：
{question}

请提取：
1. 核心问题（一句话概括）
2. 涉及的公司/行业/宏观主题（列表）
3. 用于搜索的最优英文 query（2条）
4. 分析框架建议（SWOT/竞争格局/风险评估/机会识别，选一种）

只返回JSON，不要其他内容：{{"summary": "...", "entities": ["..."], "queries": ["...","..."], "framework": "..."}}"""
    stage1 = call_llm_json(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", **OR_ATTRIBUTION_HEADERS},
        json_body={
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": stage1_prompt}],
            "max_tokens": 2048,
            "provider": provider_cfg,
            "response_format": {"type": "json_object"},
        },
        timeout=30,
        logger=logger,
        label="FollowupStage1",
    )
    if stage1 is None:
        return "（分析阶段失败，请稍后重试）"

    summary = stage1.get("summary", question)
    queries = stage1.get("queries", [question])[:2]
    framework = stage1.get("framework", "")
    logger.info(f"Followup stage1: summary='{summary}' queries={queries}")

    # Stage 2 — 收集情报: search (Tavily → SerpApi → Serper)
    context_parts = []
    for q in queries:
        try:
            results = search_news(q, days=30, max_results=5)
            for r in results:
                context_parts.append(
                    f"标题：{r['title']}\n摘要：{r['content'][:400]}\n来源：{r['url']}"
                )
        except Exception as e:
            logger.warning(f"Followup search failed for '{q}': {e}")
    context_block = "\n\n---\n\n".join(context_parts) if context_parts else "（未检索到相关情报）"
    logger.info(f"Followup stage2: {len(context_parts)} results gathered")

    # Stage 3 — 推理: V4 Pro deep synthesis
    stage3_prompt = f"""用户问题：{summary}

相关情报：
{context_block}

分析框架：{framework}

请用中文，以专业IT咨询顾问视角，围绕上述框架给出结构化分析回答。约400字，全程第三人称，无列举符号。"""
    try:
        r3_data, r3_err = post_with_retry(
            DEEPSEEK_BASE_URL,
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
            json_body={
                "model": LLM_MODEL_REASON,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": stage3_prompt},
                ],
                "max_tokens": 2000,
                "reasoning_effort": "high",
            },
            timeout=120,
        )
        if r3_err:
            logger.error(f"Followup stage3 failed: {r3_err}")
            return f"（推理阶段失败：{r3_err}）"
        return extract_llm_text(r3_data["choices"][0]["message"])
    except Exception as e:
        logger.error(f"Followup stage3 failed: {e}")
        return f"（推理阶段失败：{e}）"


def _lookup_english_name(zh_name: str) -> str:
    """用 LLM 推断公司最常用英文名，返回空字符串表示未识别。"""
    prompt = (
        f'请提供"{zh_name}"最常用的英文名称或英文缩写（如有多个，仅返回最主要的一个）。'
        f'只返回英文名称本身，不要任何解释。例如："比亚迪" → "BYD"，"海尔集团" → "Haier Group"。'
        f'如果无法确定，返回空字符串。'
    )
    try:
        data, err = post_with_retry(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", **OR_ATTRIBUTION_HEADERS},
            json_body={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 300,
                "provider": {
                    "order": ["Inceptron", "AkashML", "Nebius", "NovitaAI", "Parasail"],
                    "allow_fallbacks": True,
                },
            },
            timeout=15,
        )
        if err:
            logger.warning(f"English name lookup failed for {zh_name}: {err}")
            return ""
        msg = data["choices"][0]["message"]
        return (msg.get("content") or "").strip().strip('"')
    except Exception as e:
        logger.warning(f"English name lookup failed for {zh_name}: {e}")
        return ""


def _build_status_report() -> str:
    cfg = config_store.load_config()
    companies = ", ".join(c.get("zh", "") for c in cfg.get("companies", []))
    recipients = ", ".join(cfg.get("recipients", []))
    return f"""**Hermes 服务状态**

- 服务：正常运行
- 情报任务：每周日 08:59 PDT（北京时间周日 23:59）
- 邮件检查：每 5 分钟

**监控公司**：{companies}

**收件人**：{recipients}"""


# ── Command execution ─────────────────────────────────────────────────────────

def _describe_action(cmd: dict) -> str:
    """生成指令的中文简述，用于邮件标题行。"""
    action = cmd.get("action", "unknown")
    targets = cmd.get("targets", [])
    t = "、".join(targets) if targets else ""
    mapping = {
        "add_company":        f"添加监控公司：{t}",
        "remove_company":     f"删除监控公司：{t}",
        "add_recipient":      f"添加收件人：{t}",
        "remove_recipient":   f"删除收件人：{t}",
        "show_config":        "查看配置",
        "status_report":      "查看服务状态",
        "run_intel":          "立即生成情报日报",
        "followup_question":  "分析提问",
        "unknown":            "未识别指令",
    }
    return mapping.get(action, action)


def _execute_command(cmd: dict, new_uid: str, email_body: str = "",
                     on_async_done=None) -> tuple:
    """返回 (result_text, is_async)。
    is_async=True 表示结果将通过 on_async_done 回调另行发出。"""
    import threading
    action = cmd.get("action", "unknown")
    targets = cmd.get("targets", [])

    if action == "add_company":
        results = []
        for t in targets:
            en_name = _lookup_english_name(t)
            ok = config_store.add_company(t, en_name)
            en_note = f"（英文名：{en_name}）" if en_name else "（未识别英文名，可回复纠正）"
            results.append(f"{'已添加' if ok else '已存在'}: {t} {en_note}")
        text = "\n".join(results) + f"\n\n当前监控公司：{', '.join(config_store.get_companies())}"
        return text, False

    elif action == "remove_company":
        results = []
        for t in targets:
            ok = config_store.remove_company(t)
            results.append(f"{'已删除' if ok else '未找到'}: {t}")
        text = "\n".join(results) + f"\n\n当前监控公司：{', '.join(config_store.get_companies())}"
        return text, False

    elif action == "add_recipient":
        from run_intel import run_intel
        results = []
        new_ones = []
        for t in targets:
            ok = config_store.add_recipient(t)
            results.append(f"{'已添加' if ok else '已存在'}: {t}")
            if ok:
                new_ones.append(t)
        config_summary = f"\n\n当前收件人：{', '.join(config_store.get_recipients())}"
        if new_ones:
            def _run_and_notify():
                run_intel(recipients=new_ones)
                if on_async_done:
                    on_async_done(f"情报日报已发送至：{', '.join(new_ones)}{config_summary}")
            t = threading.Thread(target=_run_and_notify, daemon=False)
            t.start()
            _background_threads.append(t)
            return "\n".join(results) + config_summary, True
        return "\n".join(results) + config_summary, False

    elif action == "remove_recipient":
        results = []
        for t in targets:
            ok = config_store.remove_recipient(t)
            results.append(f"{'已删除' if ok else '未找到'}: {t}")
        text = "\n".join(results) + f"\n\n当前收件人：{', '.join(config_store.get_recipients())}"
        return text, False

    elif action == "show_config":
        cfg = config_store.load_config()
        companies = "\n".join(f"  - {c}" for c in cfg.get("companies", []))
        recipients = "\n".join(f"  - {r}" for r in cfg.get("recipients", []))
        return f"**监控公司**\n{companies}\n\n**收件人**\n{recipients}", False

    elif action == "status_report":
        return _build_status_report(), False

    elif action == "run_intel":
        from run_intel import run_intel
        def _run_and_notify():
            run_intel()
            if on_async_done:
                recipients = ", ".join(config_store.get_recipients())
                on_async_done(f"情报日报已发送至所有收件人：{recipients}")
        t = threading.Thread(target=_run_and_notify, daemon=False)
        t.start()
        _background_threads.append(t)
        return "情报收集已启动，约需数分钟。", True

    elif action == "followup_question":
        question = _strip_quoted_lines(email_body) or email_body[:500]
        answer = _followup_three_stage(question)
        return answer, False

    else:
        return "未能识别指令。支持：增减监控公司、增减收件人、查看状态、分析提问。", False


# ── Logging ───────────────────────────────────────────────────────────────────

def _write_log(uid: str, action: str, targets: list, result: str):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {uid} | {action} | {','.join(targets)} | {result[:60]}\n"
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line)


# ── Main polling function ─────────────────────────────────────────────────────

def run_email_check(backfill_hours: int | None = None):
    logger.info("Checking MI mailbox for commands...")
    _validate_jmap_config()  # 缺失关键配置直接抛出，不落入下面 fetch 的 try/except 被静默吞掉
    authorized = set(config_store.get_recipients())
    processed_ids = _load_processed_ids()

    if backfill_hours is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=backfill_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info(f"Backfill mode: scanning last {backfill_hours}h (after={cutoff})")
    else:
        cursor = _load_cursor()
        cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00")) - timedelta(minutes=CURSOR_OVERLAP_MINUTES)
        cutoff = cursor_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        emails = _jmap_fetch_inbox(after=cutoff)
    except Exception as e:
        logger.error(f"JMAP fetch failed: {e}")
        return

    def _advance_cursor(email: dict):
        received = email.get("receivedAt")
        if received:
            _save_cursor(received)

    new_emails = [e for e in emails if e["id"] not in processed_ids]
    if not new_emails:
        logger.info("No new command emails.")
        if emails:
            # 全部是重叠窗口内已处理过的旧邮件，仍需把游标推进到本批最新时间，
            # 否则窗口会永远卡在旧游标处，每次重复重新拉取整批。
            _advance_cursor(emails[-1])
        return

    logger.info(f"Found {len(new_emails)} unprocessed command email(s).")

    # 一旦某封邮件的回复发送失败就停止推进游标（即使后面的邮件处理成功），
    # 否则游标会跳过失败邮件、使其永久无法被重试捕获（issue #9）。
    # processed_ids 仍会为后续成功的邮件正常写入，靠它们各自的 id 避免重复处理。
    cursor_stalled = False

    for email in new_emails:
        email_id = email["id"]

        from_list = email.get("from") or []
        sender_name = from_list[0].get("name", "") if from_list else ""
        sender_email = (from_list[0].get("email", "") if from_list else "").lower()
        sender = f"{sender_name} <{sender_email}>" if sender_name else sender_email

        subject = email.get("subject", "")

        # messageId is a list of strings (angle brackets stripped by JMAP)
        msg_id_list = email.get("messageId") or []
        original_msg_id = msg_id_list[0] if msg_id_list else ""

        # 安全过滤：只处理授权发件人（无需回复，可安全标记已处理）
        if sender_email not in {r.lower() for r in authorized}:
            logger.info(f"Ignored email from unauthorized sender: {sender}")
            _save_processed_id(email_id)
            if not cursor_stalled:
                _advance_cursor(email)
            continue

        body = _jmap_body(email)
        ref_uid = _find_uid(body)

        new_uid = f"HRM-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        logger.info(f"Processing command from {sender} | ref={ref_uid or 'none'} | new={new_uid}")

        cmd = _parse_command(body)
        desc = _describe_action(cmd)
        reply_subject = f"Re: {subject}" if not subject.startswith("Re: Re:") else subject
        ref_info = f" | 引用ID：{ref_uid}" if ref_uid else ""
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        def _send_result_email(result_text: str, is_followup: bool = False) -> bool:
            """发送回复邮件，返回是否发送成功（issue #9：调用方据此决定是否推进 inbound 处理状态）。"""
            if is_followup:
                body_text = f"""指令：{desc}

结果如下：

{result_text}

---
任务ID：{new_uid}{ref_info} | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{get_footer()}"""
            else:
                body_text = f"""收到邮件指令：{desc}

结果如下：

{result_text}

---
任务ID：{new_uid}{ref_info} | {ts}
{get_footer()}"""
            sid = send_report(
                subject=reply_subject,
                markdown_body=body_text,
                reply_to_msg_id=original_msg_id,
                recipient_override=sender_email,
            )
            if sid:
                _save_processed_id(sid)
                return True
            return False

        def _on_async_done(result_text: str):
            if _send_result_email(result_text, is_followup=True):
                logger.info(f"Async done, result sent: {new_uid}")
            else:
                logger.error(f"Async done but result send failed: {new_uid} (email_id={email_id})")
            _send_telegram_alert(f"[Hermes] 邮件指令完成 ✓\n指令：{desc}\nID：{new_uid}")

        result_text, is_async = _execute_command(
            cmd, new_uid, email_body=body, on_async_done=_on_async_done
        )
        _write_log(new_uid, cmd.get("action", "unknown"), cmd.get("targets", []), result_text)

        if is_async:
            ack_body = f"""收到邮件指令：{desc}

结果随后发出。

---
任务ID：{new_uid}{ref_info} | {ts}
{get_footer()}"""
            sid = send_report(
                subject=reply_subject,
                markdown_body=ack_body,
                reply_to_msg_id=original_msg_id,
                recipient_override=sender_email,
            )
            reply_sent = bool(sid)
            if sid:
                _save_processed_id(sid)
            _send_telegram_alert(f"[Hermes] 邮件指令 ⏳\n指令：{desc}\n执行中，完成后通知\nID：{new_uid}")
        else:
            reply_sent = _send_result_email(result_text, is_followup=False)
            _send_telegram_alert(f"[Hermes] 邮件指令 ✓\n指令：{desc}\nID：{new_uid}")

        # 仅在回复（同步结果或异步 ack）确认送达后才推进 inbound 处理状态；
        # 发送失败则不标记已处理、不推进游标，下一轮靠 processed_ids 未命中重试（issue #9）。
        if reply_sent:
            _save_processed_id(email_id)
            if not cursor_stalled:
                _advance_cursor(email)
            logger.info(f"Done: {new_uid}")
        else:
            cursor_stalled = True
            logger.error(
                f"Reply send failed for {new_uid} (email_id={email_id}, subject={subject!r}) — "
                f"inbound not marked processed, cursor stalled, will retry next poll"
            )

    # Wait for any background tasks (run_intel / add_recipient) before process exits.
    for t in _background_threads:
        t.join()
    _background_threads.clear()

if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging("emailcheck")
    import argparse
    parser = argparse.ArgumentParser(description="MI 邮件指令轮询")
    parser.add_argument(
        "--backfill-hours", type=int, default=None,
        help="灾后补抓：忽略持久化游标，扫描最近 N 小时（有界范围，成功后仍会推进游标）",
    )
    args = parser.parse_args()
    try:
        run_email_check(backfill_hours=args.backfill_hours)
    except Exception:
        logger.exception("run_email_check crashed")
        raise
