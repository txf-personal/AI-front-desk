"""Unit tests for opening an escalation."""

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import AuditEvent, Customer, Escalation, EscalationStatus
from app.seed import seed
from app.services.escalations import open_escalation
from tests.times import assert_wall, pt

CLOCK = pt(2026, 11, 12, 16, 0)


def test_new_escalation_stays_open(session, session_factory):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    opened = open_escalation(
        session,
        customer_id=james.id,
        reason="Gas smell",
        detail="Customer smells gas near the water heater and is still in the house.",
    )

    assert opened.status == EscalationStatus.open
    assert opened.resolved_at is None
    assert opened.customer_id == james.id
    assert opened.reason == "Gas smell"
    assert opened.detail == "Customer smells gas near the water heater and is still in the house."
    assert_wall(opened.created_at, CLOCK)

    audits = session.scalars(select(AuditEvent).where(AuditEvent.action == "open_escalation")).all()
    assert len(audits) == 1
    assert audits[0].reasoning == "Gas smell"
    assert audits[0].related_type == "escalation"
    assert audits[0].related_id == opened.id

    with session_factory() as other:
        stored = other.get(Escalation, opened.id)
        assert stored is not None
        assert stored.status == EscalationStatus.open
        assert stored.resolved_at is None


@pytest.mark.parametrize(("reason", "detail", "message"), [("", "details", "reason is required"), ("Gas smell", "  ", "detail is required")])
def test_incomplete_escalation_writes_nothing(session, reason, detail, message):
    seed(session)
    before = session.scalar(select(func.count()).select_from(Escalation))
    with pytest.raises(PolicyError, match=message):
        open_escalation(session, reason=reason, detail=detail)
    assert session.scalar(select(func.count()).select_from(Escalation)) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_unknown_customer_is_not_escalated(session):
    seed(session)
    before = session.scalar(select(func.count()).select_from(Escalation))
    with pytest.raises(PolicyError, match="unknown customer"):
        open_escalation(session, customer_id=9999, reason="Gas smell", detail="No such customer")
    assert session.scalar(select(func.count()).select_from(Escalation)) == before
