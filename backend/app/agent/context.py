"""What the model is allowed to see for one turn."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import read_clock, shop_iso
from app.errors import PolicyError
from app.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    ConversationKind,
    Customer,
    Message,
    Technician,
)
from app.services.memory import active_memories, business_memories
from app.settings_store import all_settings, require_business

MESSAGE_LIMIT = 30


def build_context_pack(session: Session, *, audience: str, customer_id: int | None = None) -> dict:
    if audience not in {"customer", "owner"}:
        raise PolicyError("unknown audience")
    business = require_business(session)
    pack = {
        "audience": audience,
        "now": shop_iso(read_clock(session)),
        "business": {
            "name": business.name,
            "owner_name": business.owner_name,
            "timezone": business.timezone,
            "phone": business.phone,
        },
        "settings": all_settings(session, business.id),
        "shop_memories": _visible(business_memories(session)),
    }
    if audience == "owner":
        pack["open_quotes"] = _open_quotes(session, business.id)
        pack["schedule"] = _schedule(session, business.id)
        pack["recent_messages"] = _recent_messages(session, business.id, customer_id=None, kind=ConversationKind.owner)
        return pack
    if customer_id is None:
        raise PolicyError("unknown customer")
    customer = session.get(Customer, customer_id)
    if customer is None or customer.business_id != business.id:
        raise PolicyError("unknown customer")
    pack["customer"] = {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "address": customer.address,
        "memories": _visible(active_memories(session, customer.id)),
    }
    pack["open_jobs"] = _open_jobs(session, customer.id)
    pack["recent_messages"] = _recent_messages(
        session, business.id, customer_id=customer.id, kind=ConversationKind.customer
    )
    return pack


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
            "quote_sent_at": shop_iso(row.quote_sent_at),
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
            "start": shop_iso(row.start),
            "end": shop_iso(row.end),
        }
        for row in rows
    ]


def _job(row: Appointment) -> dict:
    return {
        "appointment_id": row.id,
        "service": row.service,
        "summary": row.summary,
        "status": row.status.value,
        "start": shop_iso(row.start),
        "end": shop_iso(row.end),
        "quote_amount": row.quote_amount,
    }


def _visible(rows) -> list[dict]:
    return [{"kind": row.kind.value, "content": row.content} for row in rows]


def _recent_messages(session: Session, business_id: int, *, customer_id: int | None, kind: ConversationKind) -> list[dict]:
    filters = [Conversation.business_id == business_id, Conversation.kind == kind]
    if kind == ConversationKind.customer:
        filters.append(Conversation.customer_id == customer_id)
    conversation_ids = select(Conversation.id).where(*filters)
    rows = session.scalars(
        select(Message).where(Message.conversation_id.in_(conversation_ids)).order_by(Message.id)
    ).all()
    window = rows[-MESSAGE_LIMIT:]
    return [{"role": row.role.value, "body": row.body, "created_at": shop_iso(row.created_at)} for row in window]


def _customer_names(session: Session, business_id: int) -> dict[int, str]:
    return {
        row.id: row.name
        for row in session.scalars(select(Customer).where(Customer.business_id == business_id))
    }

