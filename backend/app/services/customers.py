"""Record a caller on the shop's book."""

import re

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
    phone_text = normalize_phone(phone)
    business = require_business(session)
    if _phone_on_file(session, business.id, phone_text) is not None:
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


def update_contact(
    session: Session,
    *,
    customer_id: int,
    phone: str,
    address: str | None,
) -> Customer:
    """Replace the phone and address on file. A blank address clears it."""
    phone_text = normalize_phone(phone)
    address_text = _blank_to_none(address)
    business = require_business(session)
    customer = session.get(Customer, customer_id)
    if customer is None or customer.business_id != business.id:
        raise PolicyError("unknown customer")
    if normalize_phone(customer.phone) == phone_text and customer.address == address_text:
        if customer.phone != phone_text:
            customer.phone = phone_text
            session.commit()
        return customer
    if _phone_on_file(session, business.id, phone_text, except_id=customer.id) is not None:
        raise PolicyError("phone is already on file")
    customer.phone = phone_text
    customer.address = address_text
    write_audit(
        session,
        business_id=business.id,
        actor=AuditActor.owner,
        action="update_customer",
        reasoning="Contact updated",
        payload={"customer_id": customer.id, "phone": phone_text, "address": address_text},
        related_type="customer",
        related_id=customer.id,
    )
    session.commit()
    return customer


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


def normalize_phone(value: str) -> str:
    """Store one shape. Dashes and other separators do not make a second number."""
    text = require_text(value, "phone is required")
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise PolicyError("phone is required")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    return digits


def _phone_on_file(session: Session, business_id: int, phone_text: str, *, except_id: int | None = None) -> Customer | None:
    rows = session.scalars(select(Customer).where(Customer.business_id == business_id)).all()
    for row in rows:
        if except_id is not None and row.id == except_id:
            continue
        if normalize_phone(row.phone) == phone_text:
            return row
    return None


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
