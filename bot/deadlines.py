"""Deadlines (Stage 7): reading the time an admin types after /deadline, and showing a deadline.

Times are stored in PostgreSQL as TIMESTAMPTZ (an instant). The admin types and reads them in the
bot's time zone, TIMEZONE in .env (UTC by default).

Accepted forms (case does not matter):

    2d · 36h · 90m · 1w · 1d 12h · in 3 days     — from now
    2026-11-30 17:00 · 2026-11-30                — a date (and time) in TIMEZONE; a date alone is 23:59
    today 18:00 · tomorrow · tomorrow 9:30       — tomorrow alone is 23:59
    off                                          — remove the deadline
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo

MIN_AHEAD = timedelta(minutes=5)
MAX_AHEAD = timedelta(days=365)
OFF_WORDS = {"off", "none", "remove", "clear", "no"}
EXAMPLES = ("2d", "tomorrow 17:00", "2026-11-30 17:00")  # shown when the input cannot be read

_UNITS = {"w": timedelta(weeks=1), "d": timedelta(days=1), "h": timedelta(hours=1), "m": timedelta(minutes=1)}
_RELATIVE = re.compile(r"(\d+)\s*(w|weeks?|d|days?|h|hrs?|hours?|m|mins?|minutes?)\b")
_TIME = re.compile(r"^(\d{1,2})(?::(\d{2}))?$")
END_OF_DAY = time(23, 59)


class DeadlineError(ValueError):
    """The text is not a time the bot understands, or not an acceptable deadline. The message
    says why, in words for the admin."""


def _clock(text: str) -> time:
    match = _TIME.match(text)
    if not match or int(match[1]) > 23 or int(match[2] or 0) > 59:
        raise DeadlineError(f"“{text}” is not a time of day; write it as 17:00.")
    return time(int(match[1]), int(match[2] or 0))


def _relative(text: str) -> timedelta | None:
    text = text.removeprefix("in ").strip()
    if not _RELATIVE.search(text) or _RELATIVE.sub("", text).replace("and", "").strip(" ,"):
        return None  # something besides amounts and units
    total = timedelta()
    for amount, unit in _RELATIVE.findall(text):
        total += int(amount) * _UNITS[unit[0]]
    return total


def parse(text: str, now: datetime, tz: tzinfo) -> datetime | None:
    """The deadline `text` stands for, as an aware datetime; None for "off".

    Raises DeadlineError if it cannot be read, is in the past or less than MIN_AHEAD away, or is
    more than MAX_AHEAD away.
    """
    text = " ".join(text.lower().split())
    if text in OFF_WORDS:
        return None
    local_now = now.astimezone(tz)
    moment: datetime | None = None
    if (delta := _relative(text)) is not None:
        moment = now + delta
    else:
        day_word, _, rest = text.partition(" ")
        day: date | None = None
        if day_word in ("today", "tomorrow"):
            day = local_now.date() + timedelta(days=day_word == "tomorrow")
        else:
            try:
                day = date.fromisoformat(day_word)
            except ValueError:
                day = None
        if day is None:
            raise DeadlineError(f"I can't read “{text}” as a time.")
        at = _clock(rest) if rest else END_OF_DAY
        moment = datetime.combine(day, at, tzinfo=tz)
    if moment < now + MIN_AHEAD:
        raise DeadlineError(f"{format_moment(moment, tz, now)} is not in the future "
                            f"(a deadline must be at least {int(MIN_AHEAD.total_seconds() // 60)} minutes away).")
    if moment > now + MAX_AHEAD:
        raise DeadlineError(f"{format_moment(moment, tz, now)} is more than a year away.")
    return moment.astimezone(timezone.utc)


def format_moment(moment: datetime, tz: tzinfo, now: datetime | None = None) -> str:
    """Sat 30 Nov, 17:00 UTC (the year is added when it is not the current one)."""
    local = moment.astimezone(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    day = f"{local:%a} {local.day} {local:%b}" + (f" {local.year}" if local.year != now.year else "")
    return f"{day}, {local:%H:%M} {local.tzname()}"


def time_left(moment: datetime, now: datetime | None = None) -> str:
    """'in 2 days 3 h', 'in 45 min', or 'passed'."""
    seconds = (moment - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds <= 0:
        return "passed"
    minutes = round(seconds / 60)  # to the nearest minute: a deadline set "2d" ago reads "in 2 days"
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"in {days} day{'s' if days != 1 else ''}" + (f" {hours} h" if hours else "")
    if hours:
        return f"in {hours} h" + (f" {minutes} min" if minutes else "")
    return f"in {max(minutes, 1)} min"


def describe(moment: datetime | None, tz: tzinfo, now: datetime | None = None) -> str:
    """'Sat 30 Nov, 17:00 UTC (in 2 days)', or 'none'."""
    if moment is None:
        return "none"
    return f"{format_moment(moment, tz, now)} ({time_left(moment, now)})"
