#!/usr/bin/env python3
"""Slack inbound handler for MI: polls channel for MI followup queries.

Two trigger patterns:
  1. Thread reply to a bot message (reply to an MI report)
  2. Standalone message starting with "mi: " prefix

Routes matching messages to the three-stage followup pipeline and
posts the answer back in the same thread.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx

from slack_sender import post_message
from email_check import _followup_three_stage

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("HERMES_DATA", "/opt/data"))
PROCESSED_SLACK_PATH = DATA_DIR / "processed_slack_ts.json"
LAST_CHECK_PATH = DATA_DIR / "slack_last_check.json"

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_MI_CHANNEL = os.getenv("SLACK_MI_CHANNEL", "")
SLACK_ALLOWED_USERS = set(filter(None, os.getenv("SLACK_ALLOWED_USERS", "").split(",")))
SLACK_API_BASE = "https://slack.com/api"

# Bot's Slack user ID (U0B4ETW2SCE = @hermes).
# Messages with this user or any bot_id are treated as bot output, not user queries.
SLACK_BOT_USER_ID = "U0B4ETW2SCE"


class SlackConfigError(RuntimeError):
    """必需的 Slack 配置缺失，属于部署错误，不应被当作普通轮询失败吞掉（exit 0）。"""


def _validate_slack_config():
    """启动时校验必需环境变量非空，缺失则直接抛出（fail fast），日志只报变量名不报值。"""
    missing = [name for name, value in {
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN,
        "SLACK_MI_CHANNEL": SLACK_MI_CHANNEL,
    }.items() if not value]
    if missing:
        raise SlackConfigError(
            f"Missing required Slack config: {', '.join(missing)} — check .env"
        )


MI_PREFIX = "mi:"  # explicit prefix for standalone MI queries
PROCESSED_TS_TTL = 72 * 3600  # seconds before processed-ts entries expire
CURSOR_OVERLAP_SECONDS = 600  # 重叠窗口，防止游标边界的时钟误差漏信；重复消息靠 processed_ts 去重
MAX_HISTORY_PAGES = 50  # 分页安全阀，防止 Slack API 异常导致无限循环


# ── Processed-ts tracking ────────────────────────────────────────────────────

def _load_processed_ts() -> set:
    if not PROCESSED_SLACK_PATH.exists():
        return set()
    try:
        data = json.loads(PROCESSED_SLACK_PATH.read_text())
        cutoff = datetime.now().timestamp() - PROCESSED_TS_TTL
        valid = {ts: t for ts, t in data.items() if t >= cutoff}
        if len(valid) != len(data):
            PROCESSED_SLACK_PATH.write_text(json.dumps(valid))
        return set(valid.keys())
    except Exception:
        return set()


def _save_processed_ts(ts: str) -> None:
    try:
        data = (
            json.loads(PROCESSED_SLACK_PATH.read_text())
            if PROCESSED_SLACK_PATH.exists()
            else {}
        )
    except Exception:
        data = {}
    data[ts] = datetime.now().timestamp()
    PROCESSED_SLACK_PATH.write_text(json.dumps(data))


# ── Last-check timestamp ─────────────────────────────────────────────────────

def _load_last_check_ts() -> str:
    """Return oldest ts to poll from. Defaults to 2 hours ago on first run."""
    fallback = str(datetime.now(timezone.utc).timestamp() - 7200)
    if not LAST_CHECK_PATH.exists():
        return fallback
    try:
        return json.loads(LAST_CHECK_PATH.read_text()).get("ts", fallback)
    except Exception:
        return fallback


def _save_last_check_ts(ts: str) -> None:
    LAST_CHECK_PATH.write_text(json.dumps({"ts": ts}))


# ── Slack API helpers ─────────────────────────────────────────────────────────

def _slack_get(endpoint: str, params: dict) -> dict:
    resp = httpx.get(
        f"{SLACK_API_BASE}/{endpoint}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        params=params,
        timeout=15,
    )
    return resp.json()


def _get_channel_history(channel: str, oldest_ts: str, limit: int = 100) -> list:
    """拉取 oldest_ts 之后的全部消息，跟随 has_more/cursor 分页取尽。
    Slack 返回按 ts 降序，这里翻转为升序，便于按处理顺序推进游标。"""
    all_messages: list = []
    cursor = None
    for _ in range(MAX_HISTORY_PAGES):
        params = {"channel": channel, "oldest": oldest_ts, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = _slack_get("conversations.history", params)
        if not data.get("ok"):
            raise RuntimeError(f"conversations.history: {data.get('error')}")
        all_messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not data.get("has_more") or not cursor:
            break
    all_messages.reverse()
    return all_messages


def _get_single_message(channel: str, ts: str) -> dict:
    """Fetch a specific message by its ts."""
    data = _slack_get(
        "conversations.history",
        {"channel": channel, "oldest": ts, "latest": ts, "inclusive": "true", "limit": 1},
    )
    msgs = data.get("messages", [])
    return msgs[0] if msgs else {}


def _is_bot_message(msg: dict) -> bool:
    return bool(msg.get("bot_id")) or msg.get("user") == SLACK_BOT_USER_ID


# ── Main polling loop ─────────────────────────────────────────────────────────

def run_slack_check() -> None:
    _validate_slack_config()  # 缺失关键配置直接抛出，不落入静默 return（issue #11）

    processed = _load_processed_ts()
    oldest_ts = _load_last_check_ts()
    # 重叠窗口：从游标回退一段时间再查询，防边界时钟误差漏信；重复消息靠 processed 去重吸收
    query_from_ts = str(max(0.0, float(oldest_ts) - CURSOR_OVERLAP_SECONDS))

    try:
        messages = _get_channel_history(SLACK_MI_CHANNEL, query_from_ts)
    except Exception as e:
        logger.error(f"Slack history fetch failed: {e}")
        return

    if not messages:
        logger.info("No new Slack messages.")
        return

    for msg in messages:
        ts = msg.get("ts", "")
        if not ts:
            continue
        if ts in processed:
            _save_last_check_ts(ts)
            continue
        if _is_bot_message(msg):
            _save_processed_ts(ts)
            _save_last_check_ts(ts)
            continue

        user_id = msg.get("user", "")
        if SLACK_ALLOWED_USERS and user_id not in SLACK_ALLOWED_USERS:
            logger.info(f"Ignoring message from non-allowed user {user_id}")
            _save_processed_ts(ts)
            _save_last_check_ts(ts)
            continue

        text = (msg.get("text") or "").strip()
        thread_ts = msg.get("thread_ts")
        is_reply = bool(thread_ts and thread_ts != ts)

        question: str | None = None

        if is_reply:
            # Thread reply — qualify only if the thread parent was posted by the bot
            try:
                parent = _get_single_message(SLACK_MI_CHANNEL, thread_ts)
                if _is_bot_message(parent):
                    question = text
                    logger.info(f"MI thread reply from {user_id}: {text[:80]}")
            except Exception as e:
                logger.warning(f"Could not fetch parent message {thread_ts}: {e}")
        elif text.lower().startswith(MI_PREFIX):
            # Explicit mi: prefix on a standalone message
            question = text[len(MI_PREFIX):].strip()
            logger.info(f"MI explicit query from {user_id}: {question[:80]}")

        if not question:
            # 无 question 的路径（thread 父消息非 bot 等）：无业务要执行，可安全标记已处理
            _save_processed_ts(ts)
            _save_last_check_ts(ts)
            continue

        logger.info(f"Running followup pipeline for: {question[:100]}")
        try:
            answer = _followup_three_stage(question)
        except Exception as e:
            logger.error(f"Followup pipeline failed: {e}")
            answer = f"（分析失败：{e}）"

        reply_thread = thread_ts if is_reply else ts
        posted_ts = post_message(SLACK_MI_CHANNEL, answer, thread_ts=reply_thread)
        if posted_ts:
            # 仅在回复确认投递后才推进 processed/cursor，避免崩溃或发送失败导致提问永久丢失（issue #8）
            _save_processed_ts(ts)
            _save_last_check_ts(ts)
            logger.info(f"Replied in thread {reply_thread}")
        else:
            logger.error(
                f"post_message failed for ts={ts} (thread={reply_thread}) — "
                f"not marking processed, will retry next poll"
            )


if __name__ == "__main__":
    from log_utils import setup_logging
    setup_logging("slack-check", fmt="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        run_slack_check()
    except Exception:
        logger.exception("run_slack_check crashed")
        raise
