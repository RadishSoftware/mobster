"""Bounded five-field cron validation and timezone-aware occurrence calculation."""

from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter


MIN_INTERVAL_MS = 300_000
DAILY_LIMIT = 60


def timezone(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("Choose an IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Choose a recognized IANA timezone, such as America/Los_Angeles") from None


def validate_cron(expression, zone, daily_limit=DAILY_LIMIT):
    timezone(zone)
    if not isinstance(expression, str) or len(expression) > 128 or len(expression.split()) != 5:
        raise ValueError("Use a standard five-field cron expression")
    expression = " ".join(expression.lower().split())
    # Support numeric fields and standard month/weekday names, not seconds, years,
    # random/hash expressions, nearest weekdays, or nth/last-day extensions.
    for index, field in enumerate(expression.split()):
        permitted = ("jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec" if index == 3 else
                     "sun|mon|tue|wed|thu|fri|sat" if index == 4 else "")
        stripped = re.sub(permitted, "0", field) if permitted else field
        if not re.fullmatch(r"[0-9*,/\-]+", stripped):
            raise ValueError("Use standard cron fields without seconds, aliases, or special extensions")
    try:
        expanded, _ = croniter.expand(expression, strict=True)
    except (CroniterBadCronError, ValueError, KeyError):
        raise ValueError("The cron expression is invalid or cannot match a calendar date") from None
    minutes = range(60) if expanded[0] == ["*"] else expanded[0]
    hours = range(24) if expanded[1] == ["*"] else expanded[1]
    daily = sorted(hour * 60 + minute for hour in hours for minute in minutes)
    if len(daily) > daily_limit:
        raise ValueError(f"Schedules may contain at most {daily_limit} occurrences per day")
    if any(b - a < 5 for a, b in zip(daily, daily[1:] + [daily[0] + 1440])):
        raise ValueError("Scheduled occurrences must be at least five minutes apart")
    return expression


def next_occurrences(expression, zone, after_ms, count=1):
    if not 1 <= count <= 3:
        raise ValueError("Preview supports one to three occurrences")
    try:
        iterator = croniter(expression, datetime.fromtimestamp(after_ms / 1000, timezone(zone)),
                            max_years_between_matches=8)
        values = [round(iterator.get_next(datetime).timestamp() * 1000) for _ in range(count)]
    except (CroniterBadCronError, CroniterBadDateError, ValueError, OverflowError):
        raise ValueError("The schedule has no valid occurrence within eight years") from None
    if any(b <= a for a, b in zip([after_ms] + values, values)):
        raise ValueError("The schedule did not advance in time")
    return values


def preview(body, now_ms):
    if not isinstance(body, dict) or set(body) != {"cron", "timezone"}:
        raise ValueError("Supply cron and timezone")
    expression = validate_cron(body["cron"], body["timezone"])
    return {"next": next_occurrences(expression, body["timezone"], now_ms, 3)}
