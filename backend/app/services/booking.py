"""Schedule a visit or record a quote. Rules run before any insert."""

from datetime import datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock
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
    summary_text = require_text(summary, "summary is required")
    reason_text = require_text(reason, "reason is required")
    start_at = _aware(start)
    customer = _customer(session, customer_id)
    if not customer.address:
        raise PolicyError("customer has no address")
    spec = _service_spec(session, service)
    _check_price(spec, price)
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
    summary_text = require_text(summary, "summary is required")
    reason_text = require_text(reason, "reason is required")
    customer = _customer(session, customer_id)
    if not customer.address:
        raise PolicyError("customer has no address")
    spec = _service_spec(session, service)
    _check_price(spec, price)
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


def _check_price(spec: dict, price: int | None) -> None:
    if price is None:
        return
    if isinstance(price, bool) or not isinstance(price, int) or price != spec["price"]:
        raise PolicyError("price is not on the book")


def _check_skill(technician: Technician, spec: dict) -> None:
    if not set(spec["skills"]) <= set(technician.skills):
        raise PolicyError("technician lacks the required skill")


def _check_hours(session: Session, start: datetime, end: datetime) -> None:
    if end.date() != start.date():
        raise PolicyError("outside business hours")
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
    opening = _clock_time(span[0])
    closing = _clock_time(span[1])
    if start.time() < opening or end.time() > closing:
        raise PolicyError("outside business hours")


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
        if row.technician_id == technician.id:
            raise PolicyError("technician is already booked")
        if row.customer_id == customer.id:
            raise PolicyError("customer is already booked")


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
