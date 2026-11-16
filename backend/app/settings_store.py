"""Read and update the single shop's settings."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import PolicyError, require_text
from app.models import Business, Setting
from app.services.audit import write_audit

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def require_business(session: Session) -> Business:
    businesses = session.scalars(select(Business)).all()
    if len(businesses) != 1:
        raise LookupError("expected exactly one business")
    return businesses[0]


def all_settings(session: Session, business_id: int) -> dict:
    return {
        row.key: row.value
        for row in session.scalars(select(Setting).where(Setting.business_id == business_id).order_by(Setting.key))
    }


def get_setting(session: Session, key: str):
    business = require_business(session)
    row = _setting(session, business.id, key)
    if row is None:
        raise KeyError(key)
    return row.value


def set_setting(session: Session, key: str, value, *, reason: str):
    """Replace one existing setting and record why."""
    reason_text = require_text(reason, "reason is required")
    business = require_business(session)
    row = _setting(session, business.id, key)
    if row is None:
        raise KeyError(key)
    row.value = value
    write_audit(
        session,
        business_id=business.id,
        action="set_setting",
        reasoning=reason_text,
        payload={"key": key, "value": value},
        related_type="setting",
        related_id=row.id,
    )
    session.commit()
    return row.value


def weekday_of(key: str) -> str | None:
    """hours.saturday and saturday both name one day. Other keys do not."""
    name = key[6:] if key.startswith("hours.") else key
    name = name.lower()
    if name in _WEEKDAYS:
        return name
    return None


def merge_hours(session: Session, patch: dict, *, reason: str) -> dict:
    """Change only the weekdays named in patch. The other days stay as they are."""
    if not isinstance(patch, dict) or not patch:
        raise PolicyError("hours must be a start and end")
    unknown = [day for day in patch if day not in _WEEKDAYS]
    if unknown:
        raise PolicyError("unknown setting")
    reason_text = require_text(reason, "reason is required")
    current = get_setting(session, "hours")
    hours = {name: current.get(name) for name in _WEEKDAYS} if isinstance(current, dict) else {}
    for day, value in patch.items():
        hours[day] = _span(value)
    set_setting(session, "hours", hours, reason=reason_text)
    if "sunday" in patch:
        set_setting(session, "sunday_calls", hours["sunday"] is not None, reason=reason_text)
    return hours


def set_weekday(session: Session, day: str, value, *, reason: str):
    """Open or close one weekday without replacing the rest of the week.

    null or false closes the day. true opens 8:00 AM to 5:00 PM. Sunday also sets sunday_calls.
    """
    day_name = weekday_of(day) or ""
    if day_name not in _WEEKDAYS:
        raise PolicyError("unknown setting")
    reason_text = require_text(reason, "reason is required")
    current = get_setting(session, "hours")
    hours = {name: current.get(name) for name in _WEEKDAYS} if isinstance(current, dict) else {}
    span = _span(value)
    hours[day_name] = span
    set_setting(session, "hours", hours, reason=reason_text)
    if day_name == "sunday":
        set_setting(session, "sunday_calls", span is not None, reason=reason_text)
    return span


_AREA_NOTE = re.compile(r"service area", re.IGNORECASE)
_AREA_ADD = re.compile(r"\b(?:add|include|allow)\w*\b", re.IGNORECASE)
_AREA_ZIP = re.compile(r"\b\d{5}\b")


def service_zips_to_add(text: str) -> list[str]:
    """Zips an owner sentence is adding to the service area, in the order written."""
    if not isinstance(text, str) or _AREA_NOTE.search(text) is None or _AREA_ADD.search(text) is None:
        return []
    found: list[str] = []
    for zip_code in _AREA_ZIP.findall(text):
        if zip_code not in found:
            found.append(zip_code)
    return found


def add_service_zips(session: Session, zips: list[str], *, reason: str) -> list[str]:
    """Append zips that are not already served. Returns the full list."""
    current = get_setting(session, "service_area")
    area = [item for item in current if isinstance(item, str)] if isinstance(current, list) else []
    added = False
    for zip_code in zips:
        if isinstance(zip_code, str) and zip_code not in area:
            area.append(zip_code)
            added = True
    if added:
        set_setting(session, "service_area", area, reason=reason)
    return area


def apply_stated_service_areas(session: Session) -> None:
    """Add zips the owner already asked for, when that sentence was not saved as a setting."""
    from app.clock import read_clock
    from app.models import Conversation, ConversationKind, Message, MessageRole

    business = require_business(session)
    bodies = session.scalars(
        select(Message.body)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.business_id == business.id,
            Conversation.kind == ConversationKind.owner,
            Conversation.customer_id.is_(None),
            Message.role == MessageRole.owner,
        )
        .order_by(Message.id)
    ).all()
    zips: list[str] = []
    for body in bodies:
        for zip_code in service_zips_to_add(body):
            if zip_code not in zips:
                zips.append(zip_code)
    if not zips:
        return
    current = get_setting(session, "service_area")
    had = {item for item in current if isinstance(item, str)} if isinstance(current, list) else set()
    newly = [zip_code for zip_code in zips if zip_code not in had]
    if not newly:
        return
    add_service_zips(session, newly, reason="The owner added a service area.")
    reply = service_area_reply(newly, added=True)
    conversation = session.scalar(
        select(Conversation)
        .where(
            Conversation.business_id == business.id,
            Conversation.kind == ConversationKind.owner,
            Conversation.customer_id.is_(None),
        )
        .order_by(Conversation.id)
    )
    if conversation is None:
        return
    if session.scalar(select(Message.id).where(Message.conversation_id == conversation.id, Message.body == reply)):
        return
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.assistant,
            body=reply,
            created_at=read_clock(session),
        )
    )
    session.commit()


def service_area_reply(zips: list[str], *, added: bool) -> str:
    shown = ", ".join(zips)
    verb = "is" if len(zips) == 1 else "are"
    if added:
        return f"{shown} {verb} now in the service area."
    return f"{shown} {verb} already in the service area."


def effective_service_area(stored, notes: list[str]) -> list[str]:
    """The zips the shop serves: the setting, then any zip a service-area policy adds."""
    zips: list[str] = []
    if isinstance(stored, list):
        for item in stored:
            if isinstance(item, str) and item not in zips:
                zips.append(item)
    for note in notes:
        if not isinstance(note, str) or _AREA_NOTE.search(note) is None:
            continue
        for found in _AREA_ZIP.findall(note):
            if found not in zips:
                zips.append(found)
    return zips


OPEN_HOURS = ["08:00", "17:00"]
UNSUPPORTED_HOURS = (
    "A day is either open from 8:00 AM to 5:00 PM or closed. Other hours are not supported."
)


def effective_hours(hours: dict, *, sunday_calls: bool) -> dict:
    """Hours a visit can actually use. An open day is always 8:00 AM to 5:00 PM."""
    shown = {}
    for day in _WEEKDAYS:
        span = hours.get(day) if isinstance(hours, dict) else None
        if day == "sunday" and not sunday_calls:
            shown[day] = None
        elif day == "sunday" and not _pair(span):
            shown[day] = list(OPEN_HOURS)
        else:
            shown[day] = open_window(span)
    return shown


def open_window(value) -> list[str] | None:
    """A stored pair means the day is open. The only window is 8:00 AM to 5:00 PM."""
    if _pair(value):
        return list(OPEN_HOURS)
    return None


def _span(value):
    if value is None or value is False:
        return None
    if value is True:
        return list(OPEN_HOURS)
    if isinstance(value, list) and len(value) == 2:
        span = [_clock(value[0]), _clock(value[1])]
        if span != OPEN_HOURS:
            raise PolicyError(UNSUPPORTED_HOURS)
        return span
    raise PolicyError("hours must be a start and end")


def _pair(value) -> bool:
    return isinstance(value, list) and len(value) == 2


def _clock(value) -> str:
    if not isinstance(value, str) or value.count(":") != 1:
        raise PolicyError("hours must be a start and end")
    hour, minute = value.split(":")
    if not hour.isdigit() or not minute.isdigit():
        raise PolicyError("hours must be a start and end")
    hour_i, minute_i = int(hour), int(minute)
    if not 0 <= hour_i <= 23 or not 0 <= minute_i <= 59:
        raise PolicyError("hours must be a start and end")
    return f"{hour_i:02d}:{minute_i:02d}"


def _setting(session: Session, business_id: int, key: str) -> Setting | None:
    return session.scalar(select(Setting).where(Setting.business_id == business_id, Setting.key == key))
