"""Owner-created jobs. The runner executes them when the clock crosses the time."""

import re
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock
from app.errors import PolicyError, require_text
from app.models import Automation, AutomationAuthor, AutomationKind, AutomationRun
from app.services.audit import write_audit
from app.services.memory import business_memories
from app.settings_store import require_business

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
REPORT_NAME = "Quotes still waiting"
SAME_DAY_NAME = "Same-day reminder"
_DAILY = re.compile(r"\bevery\s+day\b", re.IGNORECASE)
_VISIT = re.compile(r"\b(?:visits?|appointments?)\b", re.IGNORECASE)
_CUSTOMERS = re.compile(r"\bcustomers?\b", re.IGNORECASE)
_CLOCK = re.compile(
    r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)
_REMOVAL = re.compile(
    r"\b(?:remove|stop|cancel|disable|drop)\b|\bturn(?:ing)?\s+off\b|\bdo\s+not\b|\bdon't\b|\bno longer\b",
    re.IGNORECASE,
)


def create_quote_report(
    session: Session,
    *,
    instruction: str,
    weekday: str,
    hour: int,
    minute: int,
    reason: str,
    conversation_id: int | None = None,
) -> Automation:
    """Store one weekly report of quotes that are not booked yet."""
    words = require_text(instruction, "instruction is required")
    reason_text = require_text(reason, "reason is required")
    day = _weekday(weekday)
    if not _clock_part(hour, 23) or not _clock_part(minute, 59):
        raise PolicyError("hour and minute must be a clock time")
    business = require_business(session)
    schedule = {"weekday": day, "hour": hour, "minute": minute}
    existing = _same_report(session, business.id, schedule)
    if existing is not None:
        return existing
    row = Automation(
        business_id=business.id,
        name=REPORT_NAME,
        description=words,
        kind=AutomationKind.quote_report,
        schedule=schedule,
        enabled=True,
        created_by=AutomationAuthor.owner,
        next_run_at=_next_occurrence(read_clock(session), _WEEKDAYS.index(day), hour, minute),
        last_run_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        business_id=business.id,
        action="create_automation",
        reasoning=reason_text,
        payload={"name": row.name, "schedule": schedule, "instruction": words},
        related_type="automation",
        related_id=row.id,
        conversation_id=conversation_id,
    )
    session.commit()
    return row


def _same_report(session: Session, business_id: int, schedule: dict) -> Automation | None:
    rows = session.scalars(
        select(Automation).where(
            Automation.business_id == business_id,
            Automation.kind == AutomationKind.quote_report,
            Automation.enabled.is_(True),
        )
    ).all()
    for row in rows:
        current = row.schedule if isinstance(row.schedule, dict) else {}
        if (
            current.get("weekday") == schedule["weekday"]
            and current.get("hour") == schedule["hour"]
            and current.get("minute") == schedule["minute"]
        ):
            return row
    return None


def _next_occurrence(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    local = now.astimezone(SHOP_TZ)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate


def parse_daily_reminder(text: str) -> tuple[int, int, int] | None:
    """A daily customer visit reminder: hour, minute, and 0 for today or 1 for tomorrow."""
    if _asks_to_remove(text):
        return None
    return _parsed_daily_reminder(text)


def parse_reminder_removal(text: str) -> tuple[int, int, int] | None:
    """The daily reminder an owner sentence is turning off."""
    if not _asks_to_remove(text):
        return None
    return _parsed_daily_reminder(text)


def _asks_to_remove(text: str) -> bool:
    return isinstance(text, str) and _REMOVAL.search(text) is not None


def _parsed_daily_reminder(text: str) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    if _DAILY.search(text) is None or _VISIT.search(text) is None or _CUSTOMERS.search(text) is None:
        return None
    found = _CLOCK.search(text)
    if found is None:
        return None
    today = _TODAY.search(text) is not None
    tomorrow = _TOMORROW.search(text) is not None
    if today == tomorrow:
        return None
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    suffix = found.group(3).lower().replace(".", "")
    if suffix == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = hour if hour == 12 else hour + 12
    return hour, minute, 0 if today else 1


def reminder_confirmation(hour: int, minute: int, day_offset: int) -> str:
    clock = _clock_phrase(hour, minute)
    if day_offset == 0:
        return f"Every day at {clock}, customers with a visit that day will hear about it."
    return f"Every day at {clock}, customers will hear about tomorrow's visit."


def reminder_removed_reply(hour: int, minute: int, day_offset: int) -> str:
    clock = _clock_phrase(hour, minute)
    if day_offset == 0:
        return f"Every day at {clock}, customers will no longer hear about that day's visit."
    return f"Every day at {clock}, customers will no longer hear about tomorrow's visit."


def create_visit_reminder(
    session: Session,
    *,
    instruction: str,
    hour: int,
    minute: int,
    day_offset: int,
    reason: str,
    conversation_id: int | None = None,
    commit: bool = True,
) -> tuple[Automation, bool]:
    """Store one daily reminder. The same clock and day offset returns the existing row."""
    words = require_text(instruction, "instruction is required")
    reason_text = require_text(reason, "reason is required")
    if day_offset not in (0, 1) or not _clock_part(hour, 23) or not _clock_part(minute, 59):
        raise PolicyError("hour and minute must be a clock time")
    business = require_business(session)
    schedule = {"hour": hour, "minute": minute, "day_offset": day_offset}
    existing = _same_reminder(session, business.id, schedule)
    if existing is not None:
        return existing, False
    _delete_reminders(session, business.id, schedule, enabled=False)
    row = Automation(
        business_id=business.id,
        name=SAME_DAY_NAME if day_offset == 0 else "Day-before reminder",
        description=words,
        kind=AutomationKind.appointment_reminder,
        schedule=schedule,
        enabled=True,
        created_by=AutomationAuthor.owner,
        next_run_at=_next_daily(read_clock(session), hour, minute),
        last_run_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        business_id=business.id,
        action="create_automation",
        reasoning=reason_text,
        payload={"name": row.name, "schedule": schedule, "instruction": words},
        related_type="automation",
        related_id=row.id,
        conversation_id=conversation_id,
    )
    if commit:
        session.commit()
    return row, True


def remove_visit_reminder(
    session: Session,
    *,
    hour: int,
    minute: int,
    day_offset: int,
    reason: str,
    conversation_id: int | None = None,
) -> bool:
    """Delete the daily reminder at this clock. Returns whether one was still saved."""
    reason_text = require_text(reason, "reason is required")
    business = require_business(session)
    schedule = {"hour": hour, "minute": minute, "day_offset": day_offset}
    removed = _delete_reminders(session, business.id, schedule)
    cleared = False
    for memory in business_memories(session):
        if parse_daily_reminder(memory.content) == (hour, minute, day_offset):
            memory.active = False
            cleared = True
    if not removed and not cleared:
        return False
    target = removed[0] if removed else None
    write_audit(
        session,
        business_id=business.id,
        action="delete_automation",
        reasoning=reason_text,
        payload={"name": target.name if target is not None else "Visit reminder", "schedule": schedule},
        related_type="automation" if target is not None else None,
        related_id=target.id if target is not None else None,
        conversation_id=conversation_id,
    )
    return True


def apply_stated_reminder_removals(session: Session) -> None:
    """Delete a reminder when the owner's latest sentence about it asked to remove it."""
    _drop_disabled_reminders(session)
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
    pending: dict[tuple[int, int, int], None] = {}
    for body in bodies:
        removal = parse_reminder_removal(body)
        if removal is not None:
            pending[removal] = None
            continue
        stated = parse_daily_reminder(body)
        if stated is not None:
            pending.pop(stated, None)
    if not pending:
        return
    replies = []
    for hour, minute, day_offset in pending:
        if remove_visit_reminder(
            session,
            hour=hour,
            minute=minute,
            day_offset=day_offset,
            reason="The owner removed a reminder.",
        ):
            replies.append(reminder_removed_reply(hour, minute, day_offset))
    if not replies:
        return
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
        session.commit()
        return
    existing = set(
        session.scalars(select(Message.body).where(Message.conversation_id == conversation.id)).all()
    )
    now = read_clock(session)
    for reply in replies:
        if reply in existing:
            continue
        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.assistant,
                body=reply,
                created_at=now,
            )
        )
    session.commit()


def _same_clock(stored, schedule: dict) -> bool:
    current = stored if isinstance(stored, dict) else {}
    offset = current.get("day_offset", 1)
    return (
        current.get("hour") == schedule["hour"]
        and current.get("minute") == schedule["minute"]
        and offset == schedule["day_offset"]
    )


def _drop_disabled_reminders(session: Session) -> None:
    """A reminder is saved or gone. Rows left from turning one off are deleted."""
    rows = session.scalars(
        select(Automation).where(
            Automation.kind == AutomationKind.appointment_reminder,
            Automation.enabled.is_(False),
        )
    ).all()
    if not rows:
        return
    session.execute(delete(AutomationRun).where(AutomationRun.automation_id.in_([row.id for row in rows])))
    session.flush()
    for row in rows:
        session.delete(row)
    session.commit()


def _delete_reminders(
    session: Session,
    business_id: int,
    schedule: dict,
    *,
    enabled: bool | None = None,
) -> list[Automation]:
    rows = session.scalars(
        select(Automation).where(
            Automation.business_id == business_id,
            Automation.kind == AutomationKind.appointment_reminder,
        )
    ).all()
    removed = [row for row in rows if _same_clock(row.schedule, schedule) and (enabled is None or row.enabled == enabled)]
    if not removed:
        return []
    session.execute(delete(AutomationRun).where(AutomationRun.automation_id.in_([row.id for row in removed])))
    session.flush()
    for row in removed:
        session.delete(row)
    session.flush()
    return removed


def install_stated_reminders(session: Session) -> None:
    """Turn a saved daily-reminder policy into a job, and send any reminder the clock already passed."""
    pending = []
    for memory in business_memories(session):
        parsed = parse_daily_reminder(memory.content)
        if parsed is None:
            continue
        pending.append((memory, *parsed))
    if not pending:
        return
    now = read_clock(session)
    runs: list[tuple[Automation, datetime]] = []
    for memory, hour, minute, day_offset in pending:
        job, _created = create_visit_reminder(
            session,
            instruction=memory.content,
            hour=hour,
            minute=minute,
            day_offset=day_offset,
            reason="The owner asked customers to hear about a visit.",
            commit=False,
        )
        memory.active = False
        runs.append((job, memory.created_at))
    session.commit()
    from app.services.runner import run_one

    for job, started in runs:
        if started < now:
            run_one(session, job, start=started, end=now)
    session.commit()


def _same_reminder(session: Session, business_id: int, schedule: dict) -> Automation | None:
    rows = session.scalars(
        select(Automation).where(
            Automation.business_id == business_id,
            Automation.kind == AutomationKind.appointment_reminder,
            Automation.enabled.is_(True),
        )
    ).all()
    for row in rows:
        current = row.schedule if isinstance(row.schedule, dict) else {}
        offset = current.get("day_offset", 1)
        if (
            current.get("hour") == schedule["hour"]
            and current.get("minute") == schedule["minute"]
            and offset == schedule["day_offset"]
        ):
            return row
    return None


def _next_daily(now: datetime, hour: int, minute: int) -> datetime:
    local = now.astimezone(SHOP_TZ)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


def _clock_phrase(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    shown = hour % 12 or 12
    return f"{shown}:{minute:02d} {suffix}"


def _weekday(value: str) -> str:
    if not isinstance(value, str):
        raise PolicyError("weekday must be a day name")
    day = value.strip().lower()
    if day not in _WEEKDAYS:
        raise PolicyError("weekday must be a day name")
    return day


def _clock_part(value, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit
