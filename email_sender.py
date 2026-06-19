import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
FROM_ADDRESS = os.getenv("FROM_ADDRESS", "")


def _md_to_html(md: str) -> str:
    lines = []
    for line in md.split("\n"):
        if line.startswith("# "):
            line = f"<h1>{line[2:]}</h1>"
        elif line.startswith("## "):
            line = f"<h2 style='border-bottom:1px solid #eee;padding-bottom:4px'>{line[3:]}</h2>"
        elif line.startswith("### "):
            line = f"<h3>{line[4:]}</h3>"
        elif line.startswith("#### "):
            line = f"<h4>{line[5:]}</h4>"
        elif line.startswith("- ") or line.startswith("* "):
            line = f"<li>{line[2:]}</li>"
        elif line.startswith("> "):
            line = f"<blockquote style='color:#666;border-left:3px solid #ccc;padding-left:8px;margin:4px 0'>{line[2:]}</blockquote>"
        elif line.startswith("---"):
            line = "<hr>"
        elif line.strip() == "":
            line = "<br>"
        else:
            line = f"<p style='margin:4px 0'>{line}</p>"
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        lines.append(line)

    body = "\n".join(lines)
    return f"""<html><body style="font-family:sans-serif;font-size:14px;max-width:800px;margin:auto;padding:20px;color:#222">
{body}
</body></html>"""


def send_report(
    subject: str,
    markdown_body: str,
    recipients: list[str] | None = None,
    reply_to_msg_id: str | None = None,
    recipient_override: str | None = None,
    # thread_id kept for signature compatibility but unused (was Gmail-specific)
    thread_id: str | None = None,
) -> list[str] | None:
    if recipient_override:
        to_list = [recipient_override]
    elif recipients:
        to_list = recipients
    else:
        from config_store import get_recipients
        to_list = get_recipients()

    if not to_list:
        logger.error("No recipients configured, skipping email")
        return None

    payload: dict = {
        "from": f"Hermes MI <{FROM_ADDRESS}>",
        "to": to_list,
        "reply_to": FROM_ADDRESS,
        "subject": subject,
        "text": markdown_body,
        "html": _md_to_html(markdown_body),
    }

    if reply_to_msg_id:
        mid = reply_to_msg_id if reply_to_msg_id.startswith("<") else f"<{reply_to_msg_id}>"
        payload["headers"] = {"In-Reply-To": mid, "References": mid}

    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        email_id = resp.json().get("id")
        logger.info(f"Email sent via Resend → {to_list} (id={email_id})")
        return [email_id] if email_id else None
    except Exception as e:
        logger.error(f"Resend send failed: {type(e).__name__}: {e}")
        return None
