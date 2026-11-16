"""JSON shapes for the shop HTTP API. Times are shop-local."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import read_clock, shop_iso as iso
from app.models import (
    Appointment,
    AuditEvent,
    Automation,
    Conversation,
    Customer,
    Escalation,
    Memory,
    Message,
    Technician,
)
from app.settings_store import all_settings, require_business


def build_bootstrap(session: Session) -> dict:
    from app.services.booking import complete_past_visits

    from app.services.automations import apply_stated_reminder_removals, install_stated_reminders

    install_stated_reminders(session)
    apply_stated_reminder_removals(session)
    from app.settings_store import apply_stated_service_areas

    apply_stated_service_areas(session)
    business = require_business(session)
    from app.agent.visit_choice import book_pending_choices, fill_stated_offers

    book_pending_choices(session)
    fill_stated_offers(session)
    if complete_past_visits(session):
        session.commit()
    customers = session.scalars(
        select(Customer).where(Customer.business_id == business.id).order_by(Customer.id)
    ).all()
    memories = session.scalars(
        select(Memory).where(Memory.business_id == business.id, Memory.active.is_(True)).order_by(Memory.id)
    ).all()
    by_customer: dict[int, list] = {}
    shop_memories = []
    for memory in memories:
        if memory.customer_id is None:
            shop_memories.append(memory)
        else:
            by_customer.setdefault(memory.customer_id, []).append(memory)
    settings = all_settings(session, business.id)
    return {
        "business": {
            "id": business.id,
            "name": business.name,
            "owner_name": business.owner_name,
            "timezone": business.timezone,
            "phone": business.phone,
        },
        "clock": iso(read_clock(session)),
        "settings": settings,
        "shop_memories": [
            {"id": memory.id, "kind": memory.kind.value, "content": memory.content}
            for memory in shop_memories
        ],
        "technicians": [
            {"id": row.id, "name": row.name, "skills": row.skills}
            for row in session.scalars(
                select(Technician).where(Technician.business_id == business.id).order_by(Technician.id)
            )
        ],
        "customers": [_customer(row, by_customer.get(row.id, [])) for row in customers],
        "appointments": [
            _appointment(row)
            for row in session.scalars(
                select(Appointment).where(Appointment.business_id == business.id).order_by(Appointment.id)
            )
        ],
        "automations": [
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "kind": row.kind.value,
                "enabled": row.enabled,
                "schedule": row.schedule,
            }
            for row in session.scalars(
                select(Automation).where(Automation.business_id == business.id).order_by(Automation.id)
            )
        ],
        "escalations": [
            _escalation(row)
            for row in session.scalars(
                select(Escalation).where(Escalation.business_id == business.id).order_by(Escalation.id)
            )
        ],
        "audits": [
            {
                "id": row.id,
                "actor": row.actor.value,
                "action": row.action,
                "reasoning": row.reasoning,
                "payload": row.payload,
                "created_at": iso(row.created_at),
                "related_type": row.related_type,
                "related_id": row.related_id,
            }
            for row in session.scalars(
                select(AuditEvent).where(AuditEvent.business_id == business.id).order_by(AuditEvent.id)
            )
        ],
        "conversations": _conversations(session, business.id),
    }


def customer_payload(customer: Customer) -> dict:
    return _customer(customer, [])


def escalation_payload(row: Escalation) -> dict:
    return _escalation(row)


def _customer(customer: Customer, memories: list) -> dict:
    return {
        "id": customer.id,
        "name": customer.name,
        "phone": customer.phone,
        "email": customer.email,
        "address": customer.address,
        "memories": [
            {"id": memory.id, "kind": memory.kind.value, "content": memory.content}
            for memory in memories
        ],
    }


def _appointment(row: Appointment) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "technician_id": row.technician_id,
        "service": row.service,
        "summary": row.summary,
        "status": row.status.value,
        "start": iso(row.start),
        "end": iso(row.end),
        "quote_amount": row.quote_amount,
        "quote_sent_at": iso(row.quote_sent_at),
        "address": row.address,
        "access_notes": row.access_notes,
    }


def _conversations(session: Session, business_id: int) -> list[dict]:
    rows = session.scalars(
        select(Conversation).where(Conversation.business_id == business_id).order_by(Conversation.id)
    ).all()
    if not rows:
        return []
    grouped: dict[int, list[Message]] = {}
    messages = session.scalars(
        select(Message)
        .where(Message.conversation_id.in_([row.id for row in rows]))
        .order_by(Message.id)
    ).all()
    for message in messages:
        grouped.setdefault(message.conversation_id, []).append(message)
    return [
        {
            "id": row.id,
            "customer_id": row.customer_id,
            "kind": row.kind.value,
            "messages": [
                {
                    "id": message.id,
                    "role": message.role.value,
                    "body": message.body,
                    "created_at": iso(message.created_at),
                }
                for message in grouped.get(row.id, [])
            ],
        }
        for row in rows
    ]


def _escalation(row: Escalation) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "reason": row.reason,
        "detail": row.detail,
        "status": row.status.value,
        "created_at": iso(row.created_at),
        "resolved_at": iso(row.resolved_at),
    }
