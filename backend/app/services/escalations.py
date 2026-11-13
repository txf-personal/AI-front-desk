"""Owner handoff. Opening one does not resolve it."""

from sqlalchemy.orm import Session

from app.clock import read_clock
from app.errors import PolicyError, require_text
from app.models import AuditActor, Conversation, Customer, Escalation, EscalationStatus
from app.services.audit import write_audit
from app.settings_store import require_business


def open_escalation(
    session: Session,
    *,
    reason: str,
    detail: str,
    customer_id: int | None = None,
    conversation_id: int | None = None,
) -> Escalation:
    reason_text = require_text(reason, "reason is required")
    detail_text = require_text(detail, "detail is required")
    business = require_business(session)
    if customer_id is not None:
        customer = session.get(Customer, customer_id)
        if customer is None or customer.business_id != business.id:
            raise PolicyError("unknown customer")
    if conversation_id is not None:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None or conversation.business_id != business.id:
            raise PolicyError("unknown conversation")
    row = Escalation(
        business_id=business.id,
        customer_id=customer_id,
        conversation_id=conversation_id,
        reason=reason_text,
        detail=detail_text,
        status=EscalationStatus.open,
        created_at=read_clock(session),
        resolved_at=None,
    )
    session.add(row)
    session.flush()
    write_audit(
        session,
        business_id=business.id,
        action="open_escalation",
        reasoning=reason_text,
        payload={"customer_id": customer_id, "detail": detail_text},
        related_type="escalation",
        related_id=row.id,
        conversation_id=conversation_id,
    )
    session.commit()
    return row


def resolve_escalation(session: Session, escalation_id: int, *, note: str | None = None) -> Escalation:
    """Mark an open escalation resolved. A second call leaves the first timestamp."""
    business = require_business(session)
    row = session.get(Escalation, escalation_id)
    if row is None or row.business_id != business.id:
        raise LookupError("unknown escalation")
    if row.status == EscalationStatus.resolved:
        raise PolicyError("escalation is already resolved")
    reasoning = note.strip() if isinstance(note, str) and note.strip() else "Owner resolved the escalation"
    row.status = EscalationStatus.resolved
    row.resolved_at = read_clock(session)
    write_audit(
        session,
        business_id=business.id,
        actor=AuditActor.owner,
        action="resolve_escalation",
        reasoning=reasoning,
        payload={"escalation_id": row.id},
        related_type="escalation",
        related_id=row.id,
        conversation_id=row.conversation_id,
    )
    session.commit()
    return row
