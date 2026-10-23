#!/usr/bin/env python3
"""手动发一封测试邮件，核对 Resend 发信链路。

只在直接运行时发信：`.venv/bin/python manual_send_test_email.py`。
文件名刻意不以 test_ 开头，避免被 pytest 收集后在导入时真的发信
（2026-10-23 曾因此给全部收件人发出一封测试邮件）。
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))


def main():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    from datetime import datetime
    from email_sender import send_report
    from hermes_footer import get_footer

    uid = f"HRM-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    sent_id = send_report(
        subject=f"[Hermes MI] 邮件测试 {datetime.now().strftime('%H:%M:%S')}",
        markdown_body=f"这是测试邮件，确认 Resend 发信正常。\n\n---\n任务ID：{uid}\n{get_footer()}",
    )
    print(f"发送成功：{sent_id}" if sent_id else "发送失败")


if __name__ == "__main__":
    main()
