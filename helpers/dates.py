"""
Shared timezone/period-range helpers. Originally lived only in
admin/service.py; pulled out here so other pages (e.g. the SABIC Outbound
order-tracking panel) can filter by "Today"/"This week"/"This month" using
the exact same calendar-boundary semantics as the admin dashboard, instead
of a second, potentially-drifting implementation.
"""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_iso(dt: datetime.datetime) -> str:
    """
    A naive UTC datetime's plain .isoformat() has no "Z"/offset suffix, and a
    timezone-less ISO string is parsed as LOCAL time by JS's Date
    constructor — silently displaying the raw UTC clock value as if it were
    already the viewer's local time. Appending "Z" here is what makes the
    frontend convert it to the viewer's actual local time correctly.
    """
    return dt.isoformat() + "Z"


def resolve_tz(tz_name: str | None) -> datetime.tzinfo:
    if not tz_name:
        return datetime.timezone.utc
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return datetime.timezone.utc


def period_range(period: str, since: str | None = None,
                  until: str | None = None, tz_name: str | None = None,
                  ) -> tuple[datetime.datetime, datetime.datetime]:
    """Resolves a period keyword into a half-open [since, until) UTC datetime
    range, server-side, so "Today"/"This week"/"This month" mean the same
    thing everywhere instead of being recomputed against the viewer's local
    clock in JS. `since`/`until` are plain "YYYY-MM-DD" date strings, only
    used (and required) when period == "custom".

    "Today" etc. are calendar days in the *viewer's* timezone (`tz_name`, an
    IANA name like "Asia/Kolkata" sent by the frontend) — not the server's
    UTC day. Naive-UTC-stored timestamps are compared against the resulting
    local-midnight boundaries converted back to naive UTC.
    Falls back to UTC if no/unrecognized tz_name is given."""
    tz = resolve_tz(tz_name)
    today = datetime.datetime.now(tz).date()
    tomorrow = today + datetime.timedelta(days=1)

    if period == "custom":
        if not since or not until:
            raise ValueError("since and until are required for a custom period")
        since_date = datetime.date.fromisoformat(since)
        until_date = datetime.date.fromisoformat(until) + datetime.timedelta(days=1)
        if since_date >= until_date:
            raise ValueError("since must be before until")
    elif period == "week":
        # Calendar week, Sunday through Saturday (not a trailing 7-day
        # window) — date.weekday() is Mon=0..Sun=6, so this steps back to
        # the most recent Sunday (today itself, if today is a Sunday).
        since_date = today - datetime.timedelta(days=(today.weekday() + 1) % 7)
        until_date = since_date + datetime.timedelta(days=7)
    elif period == "month":
        since_date, until_date = today.replace(day=1), tomorrow
    elif period == "today":
        since_date, until_date = today, tomorrow
    else:
        raise ValueError(f"Unknown period: {period}")

    since_local = datetime.datetime.combine(since_date, datetime.time.min, tzinfo=tz)
    until_local = datetime.datetime.combine(until_date, datetime.time.min, tzinfo=tz)
    return (
        since_local.astimezone(datetime.timezone.utc).replace(tzinfo=None),
        until_local.astimezone(datetime.timezone.utc).replace(tzinfo=None),
    )
