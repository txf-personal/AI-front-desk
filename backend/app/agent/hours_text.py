"""Keep a customer-facing sentence aligned with the shop's current hours."""

import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.clock import SHOP_TZ
from app.settings_store import UNSUPPORTED_HOURS, effective_hours, get_setting

_WEEKDAY_WORDS = (
    ("monday", re.compile(r"\bmondays?\b", re.IGNORECASE)),
    ("tuesday", re.compile(r"\btuesdays?\b", re.IGNORECASE)),
    ("wednesday", re.compile(r"\bwednesdays?\b", re.IGNORECASE)),
    ("thursday", re.compile(r"\bthursdays?\b", re.IGNORECASE)),
    ("friday", re.compile(r"\bfridays?\b", re.IGNORECASE)),
    ("saturday", re.compile(r"\bsaturdays?\b", re.IGNORECASE)),
    ("sunday", re.compile(r"\bsundays?\b", re.IGNORECASE)),
)
_CLOSED_CLAIM = re.compile(
    r"\bclosed\b|not open|aren'?t open|\bis off\b",
    re.IGNORECASE,
)
_OPEN_CLAIM = re.compile(r"\bopen\b", re.IGNORECASE)
_ASKS_HOURS = re.compile(r"closed|\bopen\b|hours", re.IGNORECASE)
_ASKS_SCHEDULE = re.compile(r"what|when|current", re.IGNORECASE)
_RANGE = re.compile(
    r"(?<!\d)(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?\s*(?:to|–|—|-)\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)?",
    re.IGNORECASE,
)


_HOURS_CHANGE = re.compile(
    r"closed|clos(?:e|ing)|\bopen\b|hours",
    re.IGNORECASE,
)


_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)
_CLOSE_DAY = re.compile(r"\bclos(?:e|ed|ing)\b", re.IGNORECASE)
_OPEN_DAY = re.compile(r"\bopen\b", re.IGNORECASE)


def relative_shop_day(text: str, now: datetime) -> tuple[str, bool, datetime] | None:
    """The weekday today or tomorrow means, when the message names no weekday.

    The bool is true when that day should close. The datetime is that shop day.
    A named weekday is left for the model.
    """
    if any(pattern.search(text) for _day, pattern in _WEEKDAY_WORDS):
        return None
    local = now.astimezone(SHOP_TZ)
    if _TOMORROW.search(text):
        when = local + timedelta(days=1)
    elif _TODAY.search(text):
        when = local
    else:
        return None
    closing = _CLOSE_DAY.search(text) is not None
    opening = _OPEN_DAY.search(text) is not None
    if closing == opening:
        return None
    return _WEEKDAY_WORDS[when.weekday()][0], closing, when


def unsupported_hours_request(text: str) -> str | None:
    """A weekday is open 8:00 AM–5:00 PM or closed. Any other window is refused."""
    if _HOURS_CHANGE.search(text) is None:
        return None
    for _day, pattern in _WEEKDAY_WORDS:
        stated = _stated_span(text, pattern)
        if stated is not None and stated != ("08:00", "17:00"):
            return UNSUPPORTED_HOURS
    return None


def agree_with_hours(session: Session, reply: str, asked: str) -> str:
    """Replace text that contradicts the shop's current hours.

    A question about a weekday that the reply never answers gets the factual sentence added.
    A question about the schedule, with no weekday named, gets the whole week.
    """
    hours = get_setting(session, "hours")
    if not isinstance(hours, dict):
        return reply
    shown = effective_hours(hours, sunday_calls=get_setting(session, "sunday_calls") is True)
    asking = _ASKS_HOURS.search(asked) is not None
    asked_days = [day for day, pattern in _WEEKDAY_WORDS if pattern.search(asked)]
    corrections = []
    missing = []
    for day, pattern in _WEEKDAY_WORDS:
        claim = _day_claim(reply, pattern)
        span = shown.get(day)
        closed = not (isinstance(span, list) and len(span) == 2)
        sentence = _day_sentence(day, closed, span)
        stated = _stated_span(reply, pattern)
        wrong_span = stated is not None and (closed or list(stated) != list(span))
        if (claim == "open" and closed) or (claim == "closed" and not closed) or wrong_span:
            corrections.append(sentence)
        elif asking and day in asked_days and claim is None and stated is None:
            missing.append(sentence)
    if corrections:
        return " ".join(dict.fromkeys(corrections))
    if missing:
        return f"{reply.rstrip()} {' '.join(missing)}"
    week = _week_sentence(shown)
    if asking and not asked_days and _ASKS_SCHEDULE.search(asked) and week not in reply:
        return f"{reply.rstrip()} {week}"
    return reply


def _day_sentence(day: str, closed: bool, span) -> str:
    title = day[:1].upper() + day[1:]
    if closed:
        return f"{title} is closed."
    return f"{title} is open from {_clock_phrase(span[0])} to {_clock_phrase(span[1])}."


def _week_sentence(shown: dict) -> str:
    groups: list[list[str]] = []
    for day, _pattern in _WEEKDAY_WORDS:
        span = shown.get(day)
        if isinstance(span, list) and len(span) == 2:
            label = f"{_clock_phrase(span[0])}–{_clock_phrase(span[1])}"
        else:
            label = "closed"
        if groups and groups[-1][2] == label:
            groups[-1][1] = day
        else:
            groups.append([day, day, label])
    parts = []
    for start, end, label in groups:
        name = _title(start) if start == end else f"{_title(start)}–{_title(end)}"
        parts.append(f"{name} closed" if label == "closed" else f"{name} {label}")
    return ". ".join(parts) + "."


def _title(day: str) -> str:
    return day[:1].upper() + day[1:]


def _stated_span(text: str, pattern: re.Pattern[str]) -> tuple[str, str] | None:
    found = None
    for match in pattern.finditer(text):
        start = max(text.rfind(mark, 0, match.start()) for mark in "\n.!?")
        ends = [text.find(mark, match.end()) for mark in "\n.!?"]
        ends = [pos for pos in ends if pos != -1]
        sentence = text[start + 1 : min(ends) if ends else len(text)]
        if _HOURS_CHANGE.search(sentence) is None:
            continue
        for clocks in _RANGE.finditer(sentence):
            pair = _pair_from(clocks)
            if pair is not None:
                found = pair
    return found


def _pair_from(match: re.Match[str]) -> tuple[str, str] | None:
    start_hour, start_minute, start_suffix, end_hour, end_minute, end_suffix = match.groups()
    start_suffix = start_suffix or ""
    end_suffix = end_suffix or ""
    if start_suffix and not end_suffix:
        end_suffix = start_suffix
    elif end_suffix and not start_suffix:
        if int(start_hour) > int(end_hour) and end_suffix.upper() == "PM":
            start_suffix = "AM"
        else:
            start_suffix = end_suffix
    start = _clock_from(start_hour, start_minute, start_suffix)
    end = _clock_from(end_hour, end_minute, end_suffix)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _clock_from(hour_text: str, minute_text: str | None, suffix: str) -> str | None:
    hour = int(hour_text)
    minute = int(minute_text or "0")
    if not 0 <= minute <= 59:
        return None
    suffix = suffix.upper()
    if suffix == "AM":
        if hour == 12:
            hour = 0
        elif hour > 12:
            return None
    elif suffix == "PM":
        if hour < 12:
            hour += 12
        elif hour > 12:
            return None
    elif not 0 <= hour <= 23:
        return None
    if not 0 <= hour <= 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def _day_claim(text: str, pattern: re.Pattern[str]) -> str | None:
    found_open = False
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 16) : min(len(text), match.end() + 24)]
        if _CLOSED_CLAIM.search(window):
            return "closed"
        if _OPEN_CLAIM.search(window):
            found_open = True
    return "open" if found_open else None


def _clock_phrase(value: str) -> str:
    hour_text, minute = value.split(":")
    hour = int(hour_text)
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12}:{minute} {suffix}"
