"""Side-effect log. Callers commit."""

from sqlalchemy.orm import Session

from app.clock import read_clock
from app.errors import require_text
from app.models import AuditActor, AuditEvent


def write_audit(
    session: Session,
    *,
    business_id: int,
    action: str,
    reasoning: str,
    payload: dict | list | None = None,
    related_type: str | None = None,
    related_id: int | None = None,
    actor: AuditActor = AuditActor.system,
    conversation_id: int | None = None,
) -> AuditEvent:
    event = AuditEvent(
        business_id=business_id,
        actor=actor,
        action=require_text(action, "action is required"),
        reasoning=require_text(reasoning, "reason is required"),
        payload=payload,
        created_at=read_clock(session),
        conversation_id=conversation_id,
        related_type=related_type,
        related_id=related_id,
    )
    session.add(event)
    return event
