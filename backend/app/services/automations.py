"""Owner-created jobs. The runner executes them when the clock crosses the time."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock
from app.errors import PolicyError, require_text
from app.models import Automation, AutomationAuthor, AutomationKind
from app.services.audit import write_audit
from app.settings_store import require_business

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
REPORT_NAME = "Quotes still waiting"


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


def _weekday(value: str) -> str:
    if not isinstance(value, str):
        raise PolicyError("weekday must be a day name")
    day = value.strip().lower()
    if day not in _WEEKDAYS:
        raise PolicyError("weekday must be a day name")
    return day


def _clock_part(value, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit
