"""JSON shapes for the shop HTTP API. Times are shop-local."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import read_clock, shop_iso as iso
from app.models import Appointment, AuditEvent, Automation, Customer, Escalation, Memory, Technician
from app.settings_store import all_settings, require_business


def build_bootstrap(session: Session) -> dict:
    business = require_business(session)
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
