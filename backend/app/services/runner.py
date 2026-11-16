"""Run seeded jobs when the simulated clock jumps across their due time.

Message text is a template filled from the rows the query returned. A saved
automation run is the idempotency key, so crossing the same time again is a no-op.
"""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ
from app.models import (
    Appointment,
    AppointmentStatus,
    Automation,
    AutomationKind,
    AutomationRun,
    Conversation,
    ConversationKind,
    Customer,
    Message,
    MessageRole,
    Technician,
)
from app.services.audit import write_audit
from app.settings_store import get_setting, require_business

_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def run_due(session: Session, *, start: datetime, end: datetime) -> list[AutomationRun]:
    """Run each enabled job once for every due time in (start, end]."""
    window_start = start.astimezone(SHOP_TZ)
    window_end = end.astimezone(SHOP_TZ)
    if window_end <= window_start:
        return []
    business = require_business(session)
    price_book = get_setting(session, "price_book")
    names = {
        row.id: row.name
        for row in session.scalars(select(Customer).where(Customer.business_id == business.id))
    }
    technicians = {
        row.id: row.name
        for row in session.scalars(select(Technician).where(Technician.business_id == business.id))
    }
    ran: list[AutomationRun] = []
    jobs = session.scalars(
        select(Automation)
        .where(Automation.business_id == business.id, Automation.enabled.is_(True))
        .order_by(Automation.id)
    ).all()
    for job in jobs:
        clock = _clock_time(job.schedule)
        if clock is None:
            continue
        hour, minute = clock
        weekday = _weekday_index(job.schedule)
        if weekday == -1:
            continue
        for occurrence in _occurrences(window_start, window_end, hour, minute, weekday):
            ran.extend(
                _run_job(
                    session,
                    job,
                    occurrence,
                    owner_name=business.owner_name,
                    names=names,
                    technicians=technicians,
                    price_book=price_book,
                )
            )
    return ran


def run_one(session: Session, job: Automation, *, start: datetime, end: datetime) -> list[AutomationRun]:
    """Run one job for each due time in (start, end]."""
    window_start = start.astimezone(SHOP_TZ)
    window_end = end.astimezone(SHOP_TZ)
    if window_end <= window_start:
        return []
    business = require_business(session)
    price_book = get_setting(session, "price_book")
    names = {
        row.id: row.name
        for row in session.scalars(select(Customer).where(Customer.business_id == business.id))
    }
    technicians = {
        row.id: row.name
        for row in session.scalars(select(Technician).where(Technician.business_id == business.id))
    }
    clock = _clock_time(job.schedule)
    if clock is None:
        return []
    hour, minute = clock
    weekday = _weekday_index(job.schedule)
    if weekday == -1:
        return []
    ran: list[AutomationRun] = []
    for occurrence in _occurrences(window_start, window_end, hour, minute, weekday):
        ran.extend(
            _run_job(
                session,
                job,
                occurrence,
                owner_name=business.owner_name,
                names=names,
                technicians=technicians,
                price_book=price_book,
            )
        )
    return ran


def _run_job(session, job, occurrence, *, owner_name, names, technicians, price_book) -> list[AutomationRun]:
    if job.kind == AutomationKind.morning_rundown:
        run = _rundown(session, job, occurrence, owner_name, names, technicians)
    elif job.kind == AutomationKind.appointment_reminder:
        run = _reminder(session, job, occurrence, names, technicians)
    elif job.kind == AutomationKind.quote_followup:
        return _chase(session, job, occurrence, names, price_book)
    elif job.kind == AutomationKind.quote_report:
        run = _quote_report(session, job, occurrence, owner_name, names, price_book)
    else:
        return []
    return [] if run is None else [run]


def _rundown(session, job, occurrence, owner_name, names, technicians):
    key = f"rundown:{occurrence.date().isoformat()}"
    if _already_ran(session, job.id, key):
        return None
    day = occurrence.date()
    visits = _visits_on(session, job.business_id, day, names, technicians)
    when = _day_label(occurrence)
    if visits:
        body = f"{owner_name}, morning rundown for {when}: {'; '.join(visits)}."
    else:
        body = f"{owner_name}, morning rundown for {when}: no visits booked."
    _say(session, job.business_id, None, body, occurrence)
    return _finish(
        session,
        job,
        occurrence,
        key,
        body,
        f"Morning rundown lists visits scheduled on {day.isoformat()}.",
        {"date": day.isoformat(), "visits": len(visits)},
    )


def _reminder(session, job, occurrence, names, technicians):
    key = f"reminder:{occurrence.date().isoformat()}"
    if _already_ran(session, job.id, key):
        return None
    offset = _day_offset(job.schedule)
    visit_day = occurrence.date() + timedelta(days=offset)
    visits = _scheduled(session, job.business_id, visit_day)
    bodies = []
    for visit in visits:
        technician = technicians.get(visit.technician_id, "the technician")
        start = visit.start.astimezone(SHOP_TZ)
        body = (
            f"{names.get(visit.customer_id, 'Customer')}, reminder from Northside Plumbing: "
            f"{technician} is scheduled {_day_label(start)} at {_clock_label(start)} for {visit.summary}."
        )
        _say(session, job.business_id, visit.customer_id, body, occurrence)
        bodies.append(body)
    summary = " ".join(bodies) if bodies else f"No visits on {visit_day.isoformat()}."
    return _finish(
        session,
        job,
        occurrence,
        key,
        summary,
        (
            f"Same-day reminder lists visits scheduled on {visit_day.isoformat()}."
            if offset == 0
            else f"Day-before reminder lists visits scheduled on {visit_day.isoformat()}."
        ),
        {"date": visit_day.isoformat(), "visits": len(visits)},
    )


def _chase(session, job, occurrence, names, price_book):
    quiet_days = _whole_number(job.schedule.get("quiet_days"))
    max_sends = _whole_number(job.schedule.get("max_sends"))
    if quiet_days is None or max_sends is None:
        return []
    created: list[AutomationRun] = []
    quotes = session.scalars(
        select(Appointment)
        .where(
            Appointment.business_id == job.business_id,
            Appointment.status == AppointmentStatus.quoted,
            Appointment.quote_sent_at.is_not(None),
        )
        .order_by(Appointment.id)
    ).all()
    for quote in quotes:
        if not _still_waiting(quote):
            continue
        sent, anchor = _chase_progress(session, job.id, quote.id, max_sends)
        if sent >= max_sends or anchor is None:
            continue
        if occurrence < anchor.astimezone(SHOP_TZ) + timedelta(days=quiet_days):
            continue
        key = f"quote:{quote.id}:{sent + 1}"
        if _already_ran(session, job.id, key):
            continue
        service = _service_name(price_book, quote.service)
        price = f" for ${quote.quote_amount}" if quote.quote_amount is not None else ""
        body = (
            f"{names.get(quote.customer_id, 'Customer')}, your {service} quote{price} "
            f"is still waiting. {quote.summary}."
        )
        _say(session, job.business_id, quote.customer_id, body, occurrence)
        created.append(
            _finish(
                session,
                job,
                occurrence,
                key,
                body,
                f"Quote has had no customer reply for {quiet_days} days.",
                {"appointment_id": quote.id, "follow_up": sent + 1, "max_sends": max_sends},
            )
        )
    return created


def _quote_report(session, job, occurrence, owner_name, names, price_book):
    key = f"quote-report:{occurrence.date().isoformat()}"
    if _already_ran(session, job.id, key):
        return None
    quotes = [
        quote
        for quote in session.scalars(
            select(Appointment)
            .where(Appointment.business_id == job.business_id, Appointment.status == AppointmentStatus.quoted)
            .order_by(Appointment.id)
        ).all()
        if _still_waiting(quote)
    ]
    when = _day_label(occurrence)
    if not quotes:
        body = f"{owner_name}, quotes still waiting on {when}: nobody is waiting."
    else:
        parts = []
        for quote in quotes:
            service = _service_name(price_book, quote.service)
            price = f" for ${quote.quote_amount}" if quote.quote_amount is not None else ""
            parts.append(f"{names.get(quote.customer_id, 'Customer')}, {service}{price} ({quote.summary})")
        body = f"{owner_name}, quotes still waiting on {when}: {'; '.join(parts)}."
    _say(session, job.business_id, None, body, occurrence)
    return _finish(
        session,
        job,
        occurrence,
        key,
        body,
        "Weekly report lists quotes that are still waiting.",
        {"date": occurrence.date().isoformat(), "quotes": len(quotes)},
    )


def _visits_on(session, business_id, day, names, technicians) -> list[str]:
    phrases = []
    for visit in _scheduled(session, business_id, day):
        technician = technicians.get(visit.technician_id, "the technician")
        start = visit.start.astimezone(SHOP_TZ)
        phrases.append(
            f"{names.get(visit.customer_id, 'Customer')} at {_clock_label(start)} "
            f"with {technician} ({visit.summary})"
        )
    return phrases


def _scheduled(session, business_id, day) -> list[Appointment]:
    rows = session.scalars(
        select(Appointment)
        .where(
            Appointment.business_id == business_id,
            Appointment.status == AppointmentStatus.scheduled,
            Appointment.start.is_not(None),
        )
        .order_by(Appointment.start, Appointment.id)
    ).all()
    return [row for row in rows if row.start.astimezone(SHOP_TZ).date() == day]


def _still_waiting(quote: Appointment) -> bool:
    sent = quote.quote_sent_at
    if sent is None:
        return False
    replied = quote.last_customer_message_at
    return replied is None or replied <= sent


def _chase_progress(session, automation_id: int, appointment_id: int, max_sends: int):
    keys = [f"quote:{appointment_id}:{number}" for number in range(1, max_sends + 1)]
    runs = session.scalars(
        select(AutomationRun)
        .where(AutomationRun.automation_id == automation_id, AutomationRun.dedupe_key.in_(keys))
        .order_by(AutomationRun.ran_at, AutomationRun.id)
    ).all()
    if not runs:
        quote = session.get(Appointment, appointment_id)
        return 0, None if quote is None else quote.quote_sent_at
    return len(runs), runs[-1].ran_at


def _finish(session, job, occurrence, key, summary, reasoning, payload) -> AutomationRun:
    run = AutomationRun(
        automation_id=job.id,
        dedupe_key=key,
        ran_at=occurrence,
        summary=summary,
    )
    session.add(run)
    session.flush()
    write_audit(
        session,
        business_id=job.business_id,
        action=job.kind.value,
        reasoning=reasoning,
        payload={"automation_id": job.id, "dedupe_key": key, **payload},
        related_type="automation",
        related_id=job.id,
    )
    job.last_run_at = occurrence
    step_days = 7 if _weekday_index(job.schedule) is not None else 1
    job.next_run_at = occurrence + timedelta(days=step_days)
    return run


def _say(session, business_id: int, customer_id: int | None, body: str, when: datetime) -> None:
    kind = ConversationKind.owner if customer_id is None else ConversationKind.customer
    filters = [Conversation.business_id == business_id, Conversation.kind == kind]
    if customer_id is None:
        filters.append(Conversation.customer_id.is_(None))
    else:
        filters.append(Conversation.customer_id == customer_id)
    conversation = session.scalar(select(Conversation).where(*filters).order_by(Conversation.id.desc()))
    if conversation is None:
        conversation = Conversation(
            business_id=business_id,
            customer_id=customer_id,
            kind=kind,
            created_at=when,
        )
        session.add(conversation)
        session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.assistant,
            body=body,
            created_at=when,
        )
    )


def _already_ran(session, automation_id: int, key: str) -> bool:
    existing = session.scalar(
        select(AutomationRun.id).where(
            AutomationRun.automation_id == automation_id,
            AutomationRun.dedupe_key == key,
        )
    )
    return existing is not None


def _occurrences(start: datetime, end: datetime, hour: int, minute: int, weekday: int | None):
    candidate = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= start:
        candidate += timedelta(days=1)
    while candidate <= end:
        if weekday is None or candidate.weekday() == weekday:
            yield candidate
        candidate += timedelta(days=1)


def _clock_time(schedule: dict) -> tuple[int, int] | None:
    if not isinstance(schedule, dict):
        return None
    hour = schedule.get("hour")
    minute = schedule.get("minute", 0)
    if not _clock_part(hour, 23) or not _clock_part(minute, 59):
        return None
    return hour, minute


def _day_offset(schedule: dict) -> int:
    if not isinstance(schedule, dict):
        return 1
    value = schedule.get("day_offset", 1)
    if value in (0, 1):
        return value
    return 1


def _weekday_index(schedule: dict) -> int | None:
    if not isinstance(schedule, dict) or "weekday" not in schedule:
        return None
    raw = schedule.get("weekday")
    if not isinstance(raw, str):
        return -1
    try:
        return ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday").index(raw.lower())
    except ValueError:
        return -1


def _clock_part(value, limit: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= limit


def _whole_number(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _service_name(price_book, service_id: str) -> str:
    services = price_book.get("services", []) if isinstance(price_book, dict) else []
    for item in services:
        if isinstance(item, dict) and item.get("id") == service_id:
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                return name
    return service_id


def _day_label(value: datetime) -> str:
    local = value.astimezone(SHOP_TZ)
    return f"{_DAYS[local.weekday()]}, {_MONTHS[local.month - 1]} {local.day}"


def _clock_label(value: datetime) -> str:
    local = value.astimezone(SHOP_TZ)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"{hour}:{local.minute:02d} {suffix}"
