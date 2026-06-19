#!/usr/bin/env python3
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from datetime import datetime
from email_sender import send_report
from email_check import _save_processed_id
from hermes_footer import get_footer

uid = f"HRM-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
sent_id = send_report(
    subject=f"[Hermes MI] 邮件测试 {datetime.now().strftime('%H:%M:%S')}",
    markdown_body=f"这是迁移后的测试邮件，确认 Gmail API 发送正常。\n\n---\n任务ID：{uid}\n{get_footer()}",
)
if sent_id and isinstance(sent_id, str):
    _save_processed_id(sent_id)
    print(f"发送成功，已记录 ID: {sent_id}")
else:
    print("发送失败")
