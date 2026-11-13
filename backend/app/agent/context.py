"""What the model is allowed to see for one turn."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock
from app.errors import PolicyError
from app.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    ConversationKind,
    Customer,
    Memory,
    Message,
    Setting,
    Technician,
)
from app.settings_store import require_business

MESSAGE_LIMIT = 30


def build_context_pack(session: Session, *, audience: str, customer_id: int | None = None) -> dict:
    if audience not in {"customer", "owner"}:
        raise PolicyError("unknown audience")
    business = require_business(session)
    settings = {
        row.key: row.value
        for row in session.scalars(select(Setting).where(Setting.business_id == business.id).order_by(Setting.key))
    }
    shop = {
        "name": business.name,
        "owner_name": business.owner_name,
        "timezone": business.timezone,
        "phone": business.phone,
    }
    now = _iso(read_clock(session))
    if audience == "owner":
        return {
            "audience": "owner",
            "now": now,
            "business": shop,
            "settings": settings,
            "open_quotes": _open_quotes(session, business.id),
            "schedule": _schedule(session, business.id),
            "recent_messages": _recent_messages(session, business.id, customer_id=None, kind=ConversationKind.owner),
        }
    if customer_id is None:
        raise PolicyError("unknown customer")
    customer = session.get(Customer, customer_id)
    if customer is None or customer.business_id != business.id:
        raise PolicyError("unknown customer")
    return {
        "audience": "customer",
        "now": now,
        "business": shop,
        "settings": settings,
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone": customer.phone,
            "email": customer.email,
            "address": customer.address,
            "memories": [
                {"kind": memory.kind.value, "content": memory.content}
                for memory in session.scalars(
                    select(Memory)
                    .where(Memory.customer_id == customer.id, Memory.active.is_(True))
                    .order_by(Memory.id)
                )
            ],
        },
        "open_jobs": _open_jobs(session, customer.id),
        "recent_messages": _recent_messages(
            session, business.id, customer_id=customer.id, kind=ConversationKind.customer
        ),
    }


def _open_jobs(session: Session, customer_id: int) -> list[dict]:
    rows = session.scalars(
        select(Appointment)
        .where(
            Appointment.customer_id == customer_id,
            Appointment.status.in_((AppointmentStatus.quoted, AppointmentStatus.scheduled)),
        )
        .order_by(Appointment.id)
    ).all()
    return [_job(row) for row in rows]


def _open_quotes(session: Session, business_id: int) -> list[dict]:
    rows = session.scalars(
        select(Appointment)
        .where(Appointment.business_id == business_id, Appointment.status == AppointmentStatus.quoted)
        .order_by(Appointment.id)
    ).all()
    names = _customer_names(session, business_id)
    return [
        {
            "appointment_id": row.id,
            "customer_id": row.customer_id,
            "customer_name": names.get(row.customer_id),
            "service": row.service,
            "summary": row.summary,
            "quote_amount": row.quote_amount,
            "quote_sent_at": _iso(row.quote_sent_at),
        }
        for row in rows
    ]


def _schedule(session: Session, business_id: int) -> list[dict]:
    rows = session.scalars(
        select(Appointment)
        .where(Appointment.business_id == business_id, Appointment.status == AppointmentStatus.scheduled)
        .order_by(Appointment.start)
    ).all()
    names = _customer_names(session, business_id)
    technicians = {
        row.id: row.name
        for row in session.scalars(select(Technician).where(Technician.business_id == business_id))
    }
    return [
        {
            "appointment_id": row.id,
            "customer_id": row.customer_id,
            "customer_name": names.get(row.customer_id),
            "technician_name": technicians.get(row.technician_id),
            "service": row.service,
            "summary": row.summary,
            "start": _iso(row.start),
            "end": _iso(row.end),
        }
        for row in rows
    ]


def _job(row: Appointment) -> dict:
    return {
        "appointment_id": row.id,
        "service": row.service,
        "summary": row.summary,
        "status": row.status.value,
        "start": _iso(row.start),
        "end": _iso(row.end),
        "quote_amount": row.quote_amount,
    }


def _recent_messages(session: Session, business_id: int, *, customer_id: int | None, kind: ConversationKind) -> list[dict]:
    filters = [Conversation.business_id == business_id, Conversation.kind == kind]
    if customer_id is None:
        filters.append(Conversation.customer_id.is_(None))
    else:
        filters.append(Conversation.customer_id == customer_id)
    conversation_ids = select(Conversation.id).where(*filters)
    rows = session.scalars(
        select(Message).where(Message.conversation_id.in_(conversation_ids)).order_by(Message.id)
    ).all()
    window = rows[-MESSAGE_LIMIT:]
    return [{"role": row.role.value, "body": row.body, "created_at": _iso(row.created_at)} for row in window]


def _customer_names(session: Session, business_id: int) -> dict[int, str]:
    return {
        row.id: row.name
        for row in session.scalars(select(Customer).where(Customer.business_id == business_id))
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(SHOP_TZ).isoformat()
