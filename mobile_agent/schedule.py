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


def local_timezone(environ=None, localtime="/etc/localtime"):
    """The Mac's IANA zone ("America/Los_Angeles"), so a new schedule reads in the user's own time.

    TZ wins when it names a real zone; otherwise /etc/localtime, which macOS keeps as a symlink into
    a zoneinfo tree (/var/db/timezone/zoneinfo/America/Los_Angeles). Anything unreadable is UTC."""
    import os
    environ = os.environ if environ is None else environ
    candidates = [environ.get("TZ", "").lstrip(":")]
    for resolve in (os.readlink, os.path.realpath):
        try:
            target = resolve(localtime)
        except OSError:
            continue
        if "/zoneinfo/" in target:
            candidates.append(target.split("/zoneinfo/", 1)[1])
    for candidate in candidates:
        if candidate and ("/" in candidate or candidate == "UTC"):
            try:
                timezone(candidate)
                return candidate
            except ValueError:
                continue
    return "UTC"


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


# Bounds the skipping below: clocks going back repeat an hour or two, at most 12 five-minute slots an hour.
MAX_REPEATS_SKIPPED = 288


def runs_through_repeated_hours(expression):
    """Whether a schedule runs again when a wall-clock time repeats as clocks go back. Only when its hour field
    is '*' or a step: each repeat is then a real later hour ("every 30 minutes" keeps going). A schedule at
    fixed hours ("30 1 * * *") runs once, at the first of the two, as cron runs a fixed-time job."""
    hour = expression.split()[1]
    return hour.startswith("*") or "/" in hour


def next_occurrences(expression, zone, after_ms, count=1):
    if not 1 <= count <= 3:
        raise ValueError("Preview supports one to three occurrences")
    try:
        zone_info = timezone(zone)
        iterator = croniter(expression, datetime.fromtimestamp(after_ms / 1000, zone_info),
                            max_years_between_matches=8)
        repeats = runs_through_repeated_hours(expression)
        values, skipped = [], 0
        while len(values) < count:
            moment = iterator.get_next(datetime).timestamp()
            # fold=1 is the second of two identical wall-clock times; the first one matched the same fields.
            if not repeats and datetime.fromtimestamp(moment, zone_info).fold:
                skipped += 1
                if skipped > MAX_REPEATS_SKIPPED:
                    raise ValueError("too many repeated wall-clock times")
                continue
            values.append(round(moment * 1000))
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
