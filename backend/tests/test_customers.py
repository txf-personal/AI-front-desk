"""Unit tests for recording a new caller."""

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import AuditActor, AuditEvent, Customer
from app.seed import seed
from app.services.customers import create_customer
from tests.times import assert_wall, pt


def _count(session) -> int:
    return session.scalar(select(func.count()).select_from(Customer))


def test_create_customer_strips_fields_and_stamps_the_clock(session):
    seed(session)
    row = create_customer(
        session,
        name="  Casey Brooks ",
        phone=" 206-555-0188 ",
        email="  ",
        address=" 9 Pike St ",
    )
    assert row.name == "Casey Brooks"
    assert row.phone == "206-555-0188"
    assert row.email is None
    assert row.address == "9 Pike St"
    assert_wall(row.created_at, pt(2026, 11, 12, 16, 0))
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "create_customer"))
    assert audit is not None
    assert audit.actor == AuditActor.owner
    assert audit.reasoning == "New caller recorded"
    assert audit.related_id == row.id


def test_duplicate_phone_writes_nothing(session):
    seed(session)
    before = _count(session)
    with pytest.raises(PolicyError, match="phone is already on file"):
        create_customer(session, name="Copy of James", phone="206-555-0101")
    assert _count(session) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


@pytest.mark.parametrize(("name", "phone", "message"), [("  ", "206-555-0188", "name is required"), ("Casey Brooks", " ", "phone is required")])
def test_blank_caller_fields_write_nothing(session, name, phone, message):
    seed(session)
    before = _count(session)
    with pytest.raises(PolicyError, match=message):
        create_customer(session, name=name, phone=phone)
    assert _count(session) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
