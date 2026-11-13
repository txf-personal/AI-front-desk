"""Unit tests for opening an escalation."""

import pytest
from sqlalchemy import func, select

from app.clock import advance_clock
from app.errors import PolicyError
from app.models import AuditActor, AuditEvent, Customer, Escalation, EscalationStatus
from app.seed import seed
from app.services.escalations import open_escalation, resolve_escalation
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


def test_resolve_marks_it_resolved_at_the_clock(session):
    seed(session)
    opened = open_escalation(session, reason="Gas smell", detail="Customer is still in the house.")
    resolved = resolve_escalation(session, opened.id, note="Called the gas company")

    assert resolved.status == EscalationStatus.resolved
    assert_wall(resolved.resolved_at, CLOCK)
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "resolve_escalation"))
    assert audit is not None
    assert audit.reasoning == "Called the gas company"
    assert audit.actor == AuditActor.owner
    assert audit.related_id == opened.id


def test_resolve_without_a_note_uses_a_default_reason(session):
    seed(session)
    opened = open_escalation(session, reason="Gas smell", detail="Customer is still in the house.")
    resolve_escalation(session, opened.id, note="  ")
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "resolve_escalation"))
    assert audit is not None
    assert audit.reasoning == "Owner resolved the escalation"


def test_second_resolve_keeps_the_first_timestamp(session):
    seed(session)
    opened = open_escalation(session, reason="Gas smell", detail="Customer is still in the house.")
    resolve_escalation(session, opened.id, note="Handled")
    advance_clock(session, hours=4)
    before = session.scalar(select(func.count()).select_from(AuditEvent))

    with pytest.raises(PolicyError, match="already resolved"):
        resolve_escalation(session, opened.id, note="Again")

    stored = session.get(Escalation, opened.id)
    assert stored is not None
    assert stored.status == EscalationStatus.resolved
    assert_wall(stored.resolved_at, CLOCK)
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == before


def test_unknown_escalation_cannot_be_resolved(session):
    seed(session)
    with pytest.raises(LookupError, match="unknown escalation"):
        resolve_escalation(session, 9999)
