from datetime import datetime, timezone, timedelta

SGT = timezone(timedelta(hours=8))


def get_footer() -> str:
    """生成标准邮件 footer，含操作说明和下次轮询时间。"""
    now_sgt = datetime.now(SGT)

    # 下次 5 分钟轮询（SGT 显示）
    minutes_to_next = 5 - (now_sgt.minute % 5)
    next_poll = (now_sgt + timedelta(minutes=minutes_to_next)).replace(second=0, microsecond=0)
    next_poll_str = next_poll.strftime("%H:%M SGT")

    # 下次情报：下一个周日 18:00 SGT
    days_until_sunday = (6 - now_sgt.weekday()) % 7  # weekday(): Mon=0, Sun=6
    next_sunday = (now_sgt + timedelta(days=days_until_sunday)).replace(
        hour=23, minute=59, second=0, microsecond=0
    )
    if next_sunday <= now_sgt:
        next_sunday += timedelta(days=7)
    next_intel_str = next_sunday.strftime("%Y-%m-%d 23:59 SGT")

    return f"""

---
**直接回复此邮件即可与 Hermes 交互，支持自然语言：**

- **跟进提问** — 针对报告中任何内容深入追问，例如："比亚迪加拿大建厂对麦肯锡这类咨询公司意味着什么机会？"
- **调整监控公司** — 例如："添加华为" / "删除李宁集团"
- **调整收件人** — 例如："添加收件人 colleague@company.com"
- **查看系统状态** — 例如："当前在监控哪些公司？"

下次邮件检查：{next_poll_str} | 下次情报推送：{next_intel_str}
"""
