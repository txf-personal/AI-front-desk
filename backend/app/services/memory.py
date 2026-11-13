"""Customer and shop memories."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import read_clock
from app.errors import PolicyError, require_text
from app.models import Customer, Memory, MemoryKind
from app.services.audit import write_audit
from app.settings_store import require_business


def remember(
    session: Session,
    *,
    content: str,
    kind: MemoryKind,
    reason: str,
    customer_id: int | None = None,
) -> Memory:
    """Store one active memory. A customer fact is visible only on that customer."""
    text = require_text(content, "memory is required")
    reason_text = require_text(reason, "reason is required")
    if not isinstance(kind, MemoryKind):
        raise PolicyError("unknown memory kind")
    business = require_business(session)
    if customer_id is not None:
        customer = session.get(Customer, customer_id)
        if customer is None or customer.business_id != business.id:
            raise PolicyError("unknown customer")
    row = Memory(
        business_id=business.id,
        customer_id=customer_id,
        kind=kind,
        content=text,
        created_at=read_clock(session),
        active=True,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        business_id=business.id,
        action="remember",
        reasoning=reason_text,
        payload={"customer_id": customer_id, "kind": kind.value, "content": text},
        related_type="memory",
        related_id=row.id,
    )
    session.commit()
    return row


def active_memories(session: Session, customer_id: int) -> list[Memory]:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise PolicyError("unknown customer")
    return list(
        session.scalars(
            select(Memory)
            .where(
                Memory.customer_id == customer.id,
                Memory.active.is_(True),
            )
            .order_by(Memory.id)
        )
    )


def business_memories(session: Session) -> list[Memory]:
    business = require_business(session)
    return list(
        session.scalars(
            select(Memory)
            .where(
                Memory.business_id == business.id,
                Memory.customer_id.is_(None),
                Memory.active.is_(True),
            )
            .order_by(Memory.id)
        )
    )
