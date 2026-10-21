import os
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Almaty"))
except Exception:  # нет tzdata
    TZ = timezone(timedelta(hours=5))


def now() -> datetime:
    """Локальное время владельца без tzinfo (SQLite хранит наивные даты)."""
    return datetime.now(TZ).replace(tzinfo=None)


def today():
    return now().date()
