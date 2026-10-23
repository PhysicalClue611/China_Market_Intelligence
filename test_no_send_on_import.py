#!/usr/bin/env python3
"""Guards against accidental real email sends and stale footer copy.

2026-10-23: running `pytest` collected the manual send script (then named
test_email.py), whose module-level code emailed every recipient.
"""
import importlib
import sys
from pathlib import Path
from unittest import mock

import email_sender

ROOT = Path(__file__).parent


def test_no_collected_module_sends_on_import():
    assert not (ROOT / "test_email.py").exists(), "manual send script must not be named test_*.py"
    with mock.patch.object(email_sender, "send_report", side_effect=AssertionError("send_report called on import")):
        sys.modules.pop("manual_send_test_email", None)
        importlib.import_module("manual_send_test_email")


def test_manual_script_copy_names_resend_not_gmail():
    text = (ROOT / "manual_send_test_email.py").read_text(encoding="utf-8")
    assert "Gmail" not in text
    assert "Resend" in text


def test_footer_has_no_reply_interaction_copy():
    from hermes_footer import get_footer
    footer = get_footer()
    assert "回复此邮件" not in footer
    assert "邮件检查" not in footer
    assert "下次情报推送" in footer
