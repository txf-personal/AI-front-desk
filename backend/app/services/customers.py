"""Record a caller on the shop's book."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import read_clock
from app.errors import PolicyError, require_text
from app.models import AuditActor, Customer
from app.services.audit import write_audit
from app.settings_store import require_business


def create_customer(
    session: Session,
    *,
    name: str,
    phone: str,
    email: str | None = None,
    address: str | None = None,
) -> Customer:
    name_text = require_text(name, "name is required")
    phone_text = require_text(phone, "phone is required")
    business = require_business(session)
    existing = session.scalar(
        select(Customer).where(Customer.business_id == business.id, Customer.phone == phone_text)
    )
    if existing is not None:
        raise PolicyError("phone is already on file")
    row = Customer(
        business_id=business.id,
        name=name_text,
        phone=phone_text,
        email=_blank_to_none(email),
        address=_blank_to_none(address),
        created_at=read_clock(session),
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        business_id=business.id,
        actor=AuditActor.owner,
        action="create_customer",
        reasoning="New caller recorded",
        payload={"customer_id": row.id, "name": name_text, "phone": phone_text},
        related_type="customer",
        related_id=row.id,
    )
    session.commit()
    return row


def set_address(session: Session, *, customer_id: int, address: str, reason: str) -> Customer:
    """Store the street address booking requires. A blank address writes nothing."""
    text = require_text(address, "address is required")
    reason_text = require_text(reason, "reason is required")
    business = require_business(session)
    customer = session.get(Customer, customer_id)
    if customer is None or customer.business_id != business.id:
        raise PolicyError("unknown customer")
    customer.address = text
    write_audit(
        session,
        business_id=business.id,
        action="set_address",
        reasoning=reason_text,
        payload={"customer_id": customer.id, "address": text},
        related_type="customer",
        related_id=customer.id,
    )
    session.commit()
    return customer


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
