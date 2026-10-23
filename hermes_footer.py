from datetime import datetime, timezone, timedelta

SGT = timezone(timedelta(hours=8))


def get_footer() -> str:
    """生成标准邮件 footer：只写下次情报推送时间。

    邮件回复交互（email_check 轮询）已于 2026-10-08 停用，操作说明一并撤下，
    重新启用回复机制时再加回。
    """
    now_sgt = datetime.now(SGT)

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
下次情报推送：{next_intel_str}
"""
