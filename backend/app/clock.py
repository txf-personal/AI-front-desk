"""Simulated shop clock.

Wall-clock waits are not required. Moving this clock runs the shop jobs that
came due in America/Los_Angeles.
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SimClock

SHOP_TZ = ZoneInfo("America/Los_Angeles")
MORNING_HOUR = 7


class ClockNotSeeded(RuntimeError):
    """Raised when a clock read happens before the shop is seeded."""


def parse_shop_time(value: str) -> datetime:
    """Read a shop-wall time. A value without a zone is America/Los_Angeles."""
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("clock time must include a date and time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHOP_TZ)
    return parsed.astimezone(SHOP_TZ).replace(second=0, microsecond=0)


def advance_time(
    current: datetime,
    *,
    hours: float | None = None,
    to: str | None = None,
    until: datetime | None = None,
) -> datetime:
    """Return the next shop-local time. `current` must be timezone-aware."""
    if sum(item is not None for item in (hours, to, until)) != 1:
        raise ValueError("provide exactly one of hours, to, or until")
    local = _as_shop(current)
    if until is not None:
        target = _as_shop(until).replace(second=0, microsecond=0)
        if target <= local:
            raise ValueError("clock only moves forward")
        return target
    if hours is not None:
        _require_positive_hours(hours)
        return local + timedelta(hours=hours)
    if to == "next_morning":
        morning = local.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
        if local >= morning:
            morning += timedelta(days=1)
        return morning
    if to == "next_day":
        return local + timedelta(days=1)
    raise ValueError(f"unknown clock target: {to}")


def read_clock(session: Session) -> datetime:
    row = _clock_row(session)
    return row.current_time.astimezone(SHOP_TZ)


def shop_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(SHOP_TZ).isoformat()


def set_clock(session: Session, when: datetime) -> datetime:
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    row = _clock_row(session)
    row.current_time = when.astimezone(SHOP_TZ)
    session.commit()
    return row.current_time.astimezone(SHOP_TZ)


def advance_clock(
    session: Session,
    *,
    hours: float | None = None,
    to: str | None = None,
    until: datetime | None = None,
) -> datetime:
    """Move the clock forward and run automations whose time was crossed."""
    previous = read_clock(session)
    current = advance_time(previous, hours=hours, to=to, until=until)
    row = _clock_row(session)
    row.current_time = current
    session.flush()
    from app.services.booking import complete_past_visits
    from app.services.runner import run_due

    run_due(session, start=previous, end=current)
    complete_past_visits(session)
    session.commit()
    return row.current_time.astimezone(SHOP_TZ)


def _clock_row(session: Session) -> SimClock:
    row = session.scalar(select(SimClock))
    if row is None:
        raise ClockNotSeeded("simulated clock is not seeded")
    return row


def _as_shop(current: datetime) -> datetime:
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return current.astimezone(SHOP_TZ)


def _require_positive_hours(hours: float) -> None:
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours):
        raise ValueError("hours must be a positive number")
    if hours <= 0:
        raise ValueError("hours must be positive")
