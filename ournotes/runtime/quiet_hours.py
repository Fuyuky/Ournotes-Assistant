"""Daily new-account admission window in Beijing time, independent of host timezone."""
from datetime import UTC, datetime, timedelta, timezone


def admission_delay(now=None):
    now = (now or datetime.now(UTC)).astimezone(timezone(timedelta(hours=8)))
    start = now.replace(hour=22, minute=50, second=0, microsecond=0)
    end = now.replace(hour=23, minute=5, second=0, microsecond=0)
    return max(0, (end - now).total_seconds()) if start <= now < end else 0
