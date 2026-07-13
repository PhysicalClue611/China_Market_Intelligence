#!/usr/bin/env python3
"""Hourly relay: oldest open [sama]-tagged issue in PC611-homepage -> email to sama@openai.org.

Issues are staged there per user instruction (see Hermes memory
feedback-external-product-feedback-routing) when a ChatGPT/OpenAI product-feedback
request has no real upstream issue tracker to land in. This script drains that
queue at a controlled rate: at most one issue per hourly run, oldest first,
closing the issue only after the email send is confirmed successful.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import json
import logging
from pathlib import Path

import httpx

from log_utils import setup_logging
from email_sender import send_report

logger = logging.getLogger(__name__)

REPO = "PhysicalClue611/PC611-homepage"
TAG = "[sama]"
TARGET_RECIPIENT = "sama@openai.org"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"
RESEND_STATUS_KEY = os.getenv("RESEND_STATUS_KEY", "")
STATE_PATH = Path(__file__).resolve().parent / "data" / "sama_relay_state.json"


def _gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _find_oldest_sama_issue() -> dict | None:
    resp = httpx.get(
        f"{GITHUB_API}/repos/{REPO}/issues",
        headers=_gh_headers(),
        params={"state": "open", "sort": "created", "direction": "asc", "per_page": 100},
        timeout=30,
    )
    resp.raise_for_status()
    issues = [
        issue for issue in resp.json()
        if "pull_request" not in issue and TAG.lower() in issue["title"].lower()
    ]
    return issues[0] if issues else None


def _strip_tag(title: str) -> str:
    stripped = title.replace(TAG, "").strip()
    return stripped or title


def _check_email_status(email_id: str) -> str | None:
    if not RESEND_STATUS_KEY:
        return None
    try:
        resp = httpx.get(
            f"https://api.resend.com/emails/{email_id}",
            headers={"Authorization": f"Bearer {RESEND_STATUS_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("last_event")
    except Exception as e:
        logger.warning(f"Status check failed for email {email_id}: {type(e).__name__}: {e}")
        return None


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE_PATH)


def _reopen_with_bounce_note(number: int, last_event: str) -> None:
    httpx.post(
        f"{GITHUB_API}/repos/{REPO}/issues/{number}/comments",
        headers=_gh_headers(),
        json={"body": f"Delivery check on next run reported '{last_event}' — reopening for retry."},
        timeout=30,
    ).raise_for_status()
    httpx.patch(
        f"{GITHUB_API}/repos/{REPO}/issues/{number}",
        headers=_gh_headers(),
        json={"state": "open"},
        timeout=30,
    ).raise_for_status()


def _check_previous_send() -> None:
    state = _load_state()
    prev = state.get("last_sent")
    if not prev:
        return
    last_event = _check_email_status(prev["email_id"])
    logger.info(f"Previous send (issue #{prev['issue_number']}, email {prev['email_id']}) status: {last_event}")
    if last_event == "bounced":
        try:
            _reopen_with_bounce_note(prev["issue_number"], last_event)
            logger.error(f"Issue #{prev['issue_number']} reopened — previous send bounced")
        except Exception as e:
            logger.error(f"Bounce detected for issue #{prev['issue_number']} but failed to reopen: {type(e).__name__}: {e}")


def _close_issue(number: int, email_id: str | None) -> None:
    note = f"Sent to {TARGET_RECIPIENT} via automated relay"
    if email_id:
        note += f" (Resend id={email_id})"
    httpx.post(
        f"{GITHUB_API}/repos/{REPO}/issues/{number}/comments",
        headers=_gh_headers(),
        json={"body": note},
        timeout=30,
    ).raise_for_status()
    httpx.patch(
        f"{GITHUB_API}/repos/{REPO}/issues/{number}",
        headers=_gh_headers(),
        json={"state": "closed"},
        timeout=30,
    ).raise_for_status()


def main() -> None:
    if not GITHUB_TOKEN:
        logger.error("GITHUB_TOKEN not set, aborting")
        return

    _check_previous_send()

    try:
        issue = _find_oldest_sama_issue()
    except Exception as e:
        logger.error(f"Failed to list issues: {type(e).__name__}: {e}")
        return

    if not issue:
        logger.info("No open [sama] issues, nothing to do")
        return

    number = issue["number"]
    subject = _strip_tag(issue["title"])
    body = (issue.get("body") or "").strip()
    body += f"\n\n---\nSource: {issue['html_url']}"

    logger.info(f"Sending issue #{number} ({issue['title']!r}) to {TARGET_RECIPIENT}")
    result = send_report(subject=subject, markdown_body=body, recipient_override=TARGET_RECIPIENT)

    if not result:
        logger.error(f"Send failed for issue #{number}, leaving open for retry next hour")
        return

    email_id = result[0] if result else None
    try:
        _close_issue(number, email_id)
        logger.info(f"Issue #{number} closed after successful send (email id={email_id})")
    except Exception as e:
        logger.error(f"Sent email but failed to close issue #{number}: {type(e).__name__}: {e}")

    if email_id:
        _save_state({"last_sent": {"issue_number": number, "email_id": email_id}})


if __name__ == "__main__":
    setup_logging("sama_relay")
    main()
