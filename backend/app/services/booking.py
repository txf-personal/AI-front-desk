"""Schedule a visit or record a quote. Rules run before any insert."""

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock, shop_iso
from app.errors import PolicyError, require_text
from app.models import (
    Appointment,
    AppointmentStatus,
    Customer,
    Memory,
    MemoryKind,
    Technician,
)
from app.services.audit import write_audit
from app.settings_store import get_setting

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_BLOCKING = (AppointmentStatus.scheduled, AppointmentStatus.completed)


def book_appointment(
    session: Session,
    *,
    customer_id: int,
    service: str,
    start: datetime,
    summary: str,
    reason: str,
    technician_id: int,
    price: int | None = None,
) -> Appointment:
    """Book a visit inside shop hours, on a technician who has the skill and is free."""
    customer, spec, summary_text, reason_text = _subject(
        session, customer_id, service, summary, reason, price, price_required=False
    )
    start_at = _aware(start)
    technician = _technician(session, technician_id, customer.business_id)
    _check_skill(technician, spec)
    end_at = start_at + timedelta(minutes=int(spec["duration_min"]))
    _check_hours(session, start_at, end_at)
    _check_conflicts(session, customer, technician, start_at, end_at)
    appointment = Appointment(
        business_id=customer.business_id,
        customer_id=customer.id,
        technician_id=technician.id,
        service=spec["id"],
        summary=summary_text,
        status=AppointmentStatus.scheduled,
        start=start_at,
        end=end_at,
        quote_amount=price,
        quote_sent_at=None,
        last_customer_message_at=read_clock(session),
        address=customer.address,
        access_notes=_access_notes(session, customer.id),
    )
    session.add(appointment)
    session.flush()
    write_audit(
        session,
        business_id=customer.business_id,
        action="book_appointment",
        reasoning=reason_text,
        payload={
            "customer_id": customer.id,
            "service": spec["id"],
            "technician_id": technician.id,
            "start": start_at.isoformat(),
            "end": end_at.isoformat(),
            "price": price,
        },
        related_type="appointment",
        related_id=appointment.id,
    )
    session.commit()
    return appointment


def create_quote(
    session: Session,
    *,
    customer_id: int,
    service: str,
    summary: str,
    reason: str,
    price: int,
) -> Appointment:
    """Record a quote at the published price. The slot stays empty."""
    customer, spec, summary_text, reason_text = _subject(
        session, customer_id, service, summary, reason, price, price_required=True
    )
    now = read_clock(session)
    appointment = Appointment(
        business_id=customer.business_id,
        customer_id=customer.id,
        technician_id=None,
        service=spec["id"],
        summary=summary_text,
        status=AppointmentStatus.quoted,
        start=None,
        end=None,
        quote_amount=price,
        quote_sent_at=now,
        last_customer_message_at=now,
        address=customer.address,
        access_notes=_access_notes(session, customer.id),
    )
    session.add(appointment)
    session.flush()
    write_audit(
        session,
        business_id=customer.business_id,
        action="create_quote",
        reasoning=reason_text,
        payload={"customer_id": customer.id, "service": spec["id"], "price": price},
        related_type="appointment",
        related_id=appointment.id,
    )
    session.commit()
    return appointment


def _subject(session, customer_id, service, summary, reason, price, *, price_required: bool):
    summary_text = require_text(summary, "summary is required")
    reason_text = require_text(reason, "reason is required")
    customer = _customer(session, customer_id)
    if not customer.address:
        raise PolicyError("customer has no address")
    spec = _service_spec(session, service)
    _check_price(spec, price, required=price_required)
    return customer, spec, summary_text, reason_text


def _aware(start: datetime) -> datetime:
    if not isinstance(start, datetime) or start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return start.astimezone(SHOP_TZ)


def _customer(session: Session, customer_id: int) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise PolicyError("unknown customer")
    return customer


def _technician(session: Session, technician_id: int, business_id: int) -> Technician:
    technician = session.get(Technician, technician_id)
    if technician is None or technician.business_id != business_id:
        raise PolicyError("unknown technician")
    return technician


def _service_spec(session: Session, service: str) -> dict:
    for item in get_setting(session, "price_book")["services"]:
        if item["id"] == service:
            return item
    raise PolicyError("unknown service")


def _check_price(spec: dict, price: int | None, *, required: bool) -> None:
    if price is None and not required:
        return
    if isinstance(price, bool) or not isinstance(price, int) or price != spec["price"]:
        raise PolicyError("price is not on the book")


def _check_skill(technician: Technician, spec: dict) -> None:
    if not set(spec["skills"]) <= set(technician.skills):
        raise PolicyError("technician lacks the required skill")


def available_slots(session: Session, *, customer_id: int, service: str, day: date) -> list[dict]:
    """Open slots for one service on one shop day. Nothing is written."""
    customer = _customer(session, customer_id)
    spec = _service_spec(session, service)
    opening, closing = _open_span(session, datetime.combine(day, time(12), tzinfo=SHOP_TZ))
    duration = timedelta(minutes=int(spec["duration_min"]))
    window_start = datetime.combine(day, opening, tzinfo=SHOP_TZ)
    window_end = datetime.combine(day, closing, tzinfo=SHOP_TZ)
    technicians = session.scalars(
        select(Technician).where(Technician.business_id == customer.business_id).order_by(Technician.id)
    ).all()
    slots = []
    cursor_step = timedelta(minutes=30)
    for technician in technicians:
        if not set(spec["skills"]) <= set(technician.skills):
            continue
        cursor = window_start
        while cursor + duration <= window_end:
            end = cursor + duration
            try:
                _check_conflicts(session, customer, technician, cursor, end)
            except PolicyError:
                cursor += cursor_step
                continue
            slots.append(
                {
                    "technician_id": technician.id,
                    "technician_name": technician.name,
                    "start": shop_iso(cursor),
                    "end": shop_iso(end),
                }
            )
            cursor += cursor_step
    return slots


def _check_hours(session: Session, start: datetime, end: datetime) -> None:
    if end.date() != start.date():
        raise PolicyError("outside business hours")
    opening, closing = _open_span(session, start)
    if start.time() < opening or end.time() > closing:
        raise PolicyError("outside business hours")


def _open_span(session: Session, start: datetime) -> tuple[time, time]:
    day = _DAYS[start.weekday()]
    sunday_calls = get_setting(session, "sunday_calls")
    if day == "sunday" and sunday_calls is not True:
        raise PolicyError("sunday calls are off")
    hours = get_setting(session, "hours")
    span = hours.get(day)
    if span is None and day == "sunday" and sunday_calls is True:
        span = hours.get("monday")
    if not span:
        raise PolicyError("outside business hours")
    return _clock_time(span[0]), _clock_time(span[1])


def _clock_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _check_conflicts(session, customer, technician, start, end) -> None:
    rows = session.scalars(
        select(Appointment).where(
            Appointment.business_id == customer.business_id,
            Appointment.status.in_(_BLOCKING),
            Appointment.start.is_not(None),
            Appointment.end.is_not(None),
        )
    ).all()
    for row in rows:
        if not _overlaps(start, end, row.start, row.end):
            continue
        if row.customer_id == customer.id:
            raise PolicyError("customer is already booked")
        if row.technician_id == technician.id:
            raise PolicyError("technician is already booked")


def _overlaps(start, end, other_start, other_end) -> bool:
    return start < other_end and end > other_start


def _access_notes(session: Session, customer_id: int) -> str | None:
    rows = session.scalars(
        select(Memory)
        .where(
            Memory.customer_id == customer_id,
            Memory.kind == MemoryKind.access,
            Memory.active.is_(True),
        )
        .order_by(Memory.id)
    ).all()
    if not rows:
        return None
    return "\n".join(row.content for row in rows)
