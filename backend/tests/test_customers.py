"""Unit tests for recording a new caller."""

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import AuditActor, AuditEvent, Customer
from app.seed import seed
from app.services.customers import create_customer, update_contact
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


def test_a_phone_without_dashes_is_stored_with_them(session):
    seed(session)
    row = create_customer(session, name="Casey Brooks", phone="2065550188")
    assert row.phone == "206-555-0188"


def test_dashes_do_not_make_a_second_copy_of_a_phone(session):
    seed(session)
    before = _count(session)
    with pytest.raises(PolicyError, match="phone is already on file"):
        create_customer(session, name="Copy of James", phone="2065550101")
    assert _count(session) == before


def test_duplicate_phone_writes_nothing(session):
    seed(session)
    before = _count(session)
    with pytest.raises(PolicyError, match="phone is already on file"):
        create_customer(session, name="Copy of James", phone="206-555-0101")
    assert _count(session) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_contact_update_strips_phone_and_address(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    row = update_contact(
        session,
        customer_id=james.id,
        phone=" 206-555-0199 ",
        address="  9 Pike St, Seattle, WA 98115  ",
    )
    assert row.phone == "206-555-0199"
    assert row.address == "9 Pike St, Seattle, WA 98115"
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "update_customer"))
    assert audit is not None
    assert audit.actor == AuditActor.owner
    assert audit.reasoning == "Contact updated"
    assert audit.payload == {
        "customer_id": james.id,
        "phone": "206-555-0199",
        "address": "9 Pike St, Seattle, WA 98115",
    }


def test_a_blank_address_clears_the_one_on_file(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    row = update_contact(session, customer_id=james.id, phone=james.phone, address="  ")
    assert row.address is None


def test_an_unchanged_contact_writes_nothing(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    before = session.scalar(select(func.count()).select_from(AuditEvent))
    update_contact(session, customer_id=james.id, phone=james.phone, address=james.address)
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == before


def test_the_same_number_without_dashes_is_not_a_change(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    before = session.scalar(select(func.count()).select_from(AuditEvent))
    row = update_contact(session, customer_id=james.id, phone="2065550101", address=james.address)
    assert row.phone == "206-555-0101"
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == before


def test_a_phone_already_on_file_is_left_alone(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    sam = session.scalar(select(Customer).where(Customer.name == "Sam Rivera"))
    assert james is not None and sam is not None
    with pytest.raises(PolicyError, match="phone is already on file"):
        update_contact(session, customer_id=james.id, phone="2065550103", address=james.address)
    session.refresh(james)
    assert james.phone == "206-555-0101"


def test_a_blank_phone_is_rejected(session):
    seed(session)
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    with pytest.raises(PolicyError, match="phone is required"):
        update_contact(session, customer_id=james.id, phone=" ", address=james.address)
    session.refresh(james)
    assert james.phone == "206-555-0101"


@pytest.mark.parametrize(("name", "phone", "message"), [("  ", "206-555-0188", "name is required"), ("Casey Brooks", " ", "phone is required")])
def test_blank_caller_fields_write_nothing(session, name, phone, message):
    seed(session)
    before = _count(session)
    with pytest.raises(PolicyError, match=message):
        create_customer(session, name=name, phone=phone)
    assert _count(session) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
