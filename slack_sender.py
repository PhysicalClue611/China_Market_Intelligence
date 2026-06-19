"""Slack message delivery for MI project.

Post reports and replies to a Slack channel/DM using the bot token
shared with Hermes Gateway.
"""
import os
import re
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

SLACK_API_BASE = "https://slack.com/api"
SLACK_MAX_TEXT = 3900  # Slack hard limit is 4000; leave buffer for safety


def _get_token() -> str:
    return os.getenv("SLACK_BOT_TOKEN", "")


def md_to_slack(text: str) -> str:
    """Best-effort Markdown → Slack mrkdwn conversion."""
    # **bold** → *bold*
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text, flags=re.DOTALL)
    # [text](url) → <url|text>
    text = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'<\2|\1>', text)
    # # / ## / ### Heading → *Heading*
    text = re.sub(r'^#{1,4} (.+)$', r'*\1*', text, flags=re.MULTILINE)
    # --- → horizontal line
    text = re.sub(r'^---+$', '─' * 30, text, flags=re.MULTILINE)
    return text


def _split_chunks(text: str, max_len: int = SLACK_MAX_TEXT) -> list[str]:
    """Split text into chunks ≤ max_len, breaking at double-newline boundaries."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    buf = ""
    for para in re.split(r'\n\n+', text):
        seg = ("\n\n" + para) if buf else para
        if len(buf) + len(seg) <= max_len:
            buf += seg
        else:
            if buf:
                chunks.append(buf)
            if len(para) > max_len:
                while len(para) > max_len:
                    chunks.append(para[:max_len])
                    para = para[max_len:]
                buf = para
            else:
                buf = para
    if buf:
        chunks.append(buf)
    return chunks


def post_message(
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
) -> Optional[str]:
    """Post text to a Slack channel or DM.

    Splits long messages into threaded chunks. Returns ts of the first
    message posted, or None on failure.
    """
    token = _get_token()
    if not token:
        logger.warning("SLACK_BOT_TOKEN not set, skipping Slack post")
        return None

    chunks = _split_chunks(text)
    first_ts: Optional[str] = None
    current_thread = thread_ts

    for i, chunk in enumerate(chunks):
        payload: dict = {"channel": channel, "text": chunk, "mrkdwn": True}
        if current_thread:
            payload["thread_ts"] = current_thread
        try:
            resp = httpx.post(
                f"{SLACK_API_BASE}/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                logger.error(
                    f"Slack post failed (chunk {i + 1}/{len(chunks)}): {data.get('error')}"
                )
                return first_ts
            ts = data.get("ts")
            if first_ts is None:
                first_ts = ts
            # Thread subsequent chunks under the first message
            if current_thread is None:
                current_thread = first_ts
        except Exception as e:
            logger.error(f"Slack post exception: {e}")
            return first_ts

    return first_ts


def post_report(markdown_body: str, channel: Optional[str] = None) -> Optional[str]:
    """Post an MI intel report to the configured Slack channel.

    Converts Markdown to mrkdwn, splits into chunks if needed.
    Returns ts of the first message, or None if SLACK_MI_CHANNEL is unset.
    """
    ch = channel or os.getenv("SLACK_MI_CHANNEL", "")
    if not ch:
        logger.info("SLACK_MI_CHANNEL not configured, skipping Slack delivery")
        return None
    slack_text = md_to_slack(markdown_body)
    return post_message(ch, slack_text)
