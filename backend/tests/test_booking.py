"""Unit tests for booking and quote rules."""

from datetime import date, datetime

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import (
    Appointment,
    AppointmentStatus,
    AuditActor,
    AuditEvent,
    Customer,
    Memory,
    MemoryKind,
    Technician,
)
from app.seed import seed
from app.services.booking import available_slots, book_appointment, create_quote
from app.settings_store import set_setting
from tests.times import assert_wall, pt

CLOCK = pt(2026, 11, 12, 16, 0)
FRIDAY = (2026, 11, 13)


def _named(session, model, name):
    row = session.scalar(select(model).where(model.name == name))
    assert row is not None
    return row


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _audits(session, action: str) -> list[AuditEvent]:
    return list(session.scalars(select(AuditEvent).where(AuditEvent.action == action)))


def _book(session, **overrides):
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    values = {
        "customer_id": james.id,
        "service": "leak",
        "start": pt(*FRIDAY, 13, 0),
        "summary": "Water heater is leaking",
        "reason": "Customer asked for the first afternoon leak visit",
        "technician_id": andre.id,
    }
    values.update(overrides)
    return book_appointment(session, **values)


def test_booking_james_copies_address_gate_code_and_reason(session, session_factory):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    booked = _book(session)

    assert booked.address == "1842 N 92nd St, Seattle, WA 98115"
    assert booked.address == james.address
    assert booked.access_notes == "Gate code 4481"
    assert booked.summary == "Water heater is leaking"
    assert booked.service == "leak"
    assert booked.status == AppointmentStatus.scheduled
    assert booked.quote_amount is None
    assert_wall(booked.start, pt(*FRIDAY, 13, 0))
    assert_wall(booked.end, pt(*FRIDAY, 14, 30))
    assert_wall(booked.last_customer_message_at, CLOCK)

    audits = _audits(session, "book_appointment")
    assert len(audits) == 1
    assert audits[0].reasoning == "Customer asked for the first afternoon leak visit"
    assert audits[0].actor == AuditActor.system
    assert audits[0].related_type == "appointment"
    assert audits[0].related_id == booked.id
    assert_wall(audits[0].created_at, CLOCK)

    with session_factory() as other:
        stored = other.get(Appointment, booked.id)
        assert stored is not None
        assert stored.address == james.address
        assert stored.access_notes == "Gate code 4481"


def test_inactive_gate_code_is_not_copied(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    gate = session.scalar(select(Memory).where(Memory.customer_id == james.id))
    assert gate is not None
    gate.active = False
    session.commit()

    booked = _book(session)
    assert booked.address == james.address
    assert booked.access_notes is None


def test_booking_joins_active_access_notes_in_id_order(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    session.add(
        Memory(
            business_id=james.business_id,
            customer_id=james.id,
            kind=MemoryKind.access,
            content="Side gate 2290",
            created_at=CLOCK,
            active=True,
        )
    )
    session.commit()

    booked = _book(session)
    assert booked.access_notes == "Gate code 4481\nSide gate 2290"


def test_booking_does_not_copy_another_customers_gate_code(session):
    seed(session)
    priya = _named(session, Customer, "Priya Shah")
    chris = _named(session, Technician, "Chris Patel")
    booked = _book(
        session,
        customer_id=priya.id,
        service="faucet",
        start=pt(2026, 11, 14, 9, 0),
        summary="Kitchen faucet drips",
        technician_id=chris.id,
    )
    assert booked.address == priya.address
    assert booked.access_notes is None
    assert "4481" not in (booked.access_notes or "")


def test_sunday_is_rejected_until_calls_are_enabled(session):
    seed(session)
    brooke = _named(session, Technician, "Brooke Hale")
    set_setting(session, "sunday_calls", False, reason="We stopped doing Sunday calls")
    before = _count(session, Appointment)

    with pytest.raises(PolicyError, match="sunday calls are off"):
        _book(
            session,
            service="drain",
            start=pt(2026, 11, 15, 10, 0),
            summary="Sunday drain",
            technician_id=brooke.id,
        )

    assert _count(session, Appointment) == before
    assert _audits(session, "book_appointment") == []

    set_setting(session, "sunday_calls", True, reason="Sunday calls are allowed again")
    booked = _book(
        session,
        service="drain",
        start=pt(2026, 11, 15, 10, 0),
        summary="Sunday drain",
        technician_id=brooke.id,
    )
    assert booked.status == AppointmentStatus.scheduled
    assert_wall(booked.start, pt(2026, 11, 15, 10, 0))
    assert_wall(booked.end, pt(2026, 11, 15, 11, 0))


@pytest.mark.parametrize(("hour", "minute", "tech_name"), [(7, 59, "Dana Okada"), (16, 1, "Brooke Hale")])
def test_outside_business_hours_is_rejected(session, hour, minute, tech_name):
    seed(session)
    tech = _named(session, Technician, tech_name)
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="outside business hours"):
        _book(
            session,
            service="drain",
            start=pt(*FRIDAY, hour, minute),
            summary="Too early or too late",
            technician_id=tech.id,
        )
    assert _count(session, Appointment) == before
    assert _audits(session, "book_appointment") == []


@pytest.mark.parametrize(("hour", "minute", "tech_name"), [(8, 0, "Dana Okada"), (16, 0, "Brooke Hale")])
def test_slot_on_the_hour_boundary_is_accepted(session, hour, minute, tech_name):
    seed(session)
    tech = _named(session, Technician, tech_name)
    booked = _book(
        session,
        service="drain",
        start=pt(*FRIDAY, hour, minute),
        summary="Boundary visit",
        technician_id=tech.id,
    )
    assert_wall(booked.end, pt(*FRIDAY, hour + 1, minute))


def test_overlapping_technician_is_rejected(session):
    seed(session)
    brooke = _named(session, Technician, "Brooke Hale")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="technician is already booked"):
        _book(
            session,
            service="drain",
            start=pt(*FRIDAY, 9, 0),
            summary="Same slot as Luis",
            technician_id=brooke.id,
        )
    assert _count(session, Appointment) == before


def test_adjacent_slot_is_free(session):
    seed(session)
    brooke = _named(session, Technician, "Brooke Hale")
    booked = _book(
        session,
        service="drain",
        start=pt(*FRIDAY, 10, 0),
        summary="After Luis",
        technician_id=brooke.id,
    )
    assert_wall(booked.start, pt(*FRIDAY, 10, 0))


def test_completed_visit_still_blocks_that_technician(session):
    seed(session)
    chris = _named(session, Technician, "Chris Patel")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="technician is already booked"):
        _book(
            session,
            service="faucet",
            start=pt(2026, 11, 4, 10, 0),
            summary="Same slot as Elena",
            technician_id=chris.id,
        )
    assert _count(session, Appointment) == before


def test_customer_overlap_is_rejected(session):
    seed(session)
    chris = _named(session, Technician, "Chris Patel")
    _book(session)
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="customer is already booked"):
        _book(
            session,
            service="leak",
            start=pt(*FRIDAY, 14, 0),
            summary="Overlaps James",
            technician_id=chris.id,
        )
    assert _count(session, Appointment) == before


def test_customer_can_take_the_next_adjacent_slot(session):
    seed(session)
    chris = _named(session, Technician, "Chris Patel")
    _book(session)
    booked = _book(
        session,
        service="leak",
        start=pt(*FRIDAY, 14, 30),
        summary="After the first visit",
        technician_id=chris.id,
    )
    assert_wall(booked.start, pt(*FRIDAY, 14, 30))
    assert_wall(booked.end, pt(*FRIDAY, 16, 0))


def test_cancelled_visit_does_not_block_the_technician(session):
    seed(session)
    dana = _named(session, Technician, "Dana Okada")
    first = _book(
        session,
        service="drain",
        start=pt(*FRIDAY, 11, 0),
        summary="Will be cancelled",
        technician_id=dana.id,
    )
    first.status = AppointmentStatus.cancelled
    session.commit()
    second = _book(
        session,
        service="drain",
        start=pt(*FRIDAY, 11, 0),
        summary="Same slot after cancel",
        technician_id=dana.id,
    )
    assert second.id != first.id
    assert second.status == AppointmentStatus.scheduled


def test_technician_without_the_skill_is_rejected(session):
    seed(session)
    andre = _named(session, Technician, "Andre Williams")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="technician lacks the required skill"):
        _book(
            session,
            service="drain",
            start=pt(*FRIDAY, 11, 0),
            summary="Andre does not clear drains",
            technician_id=andre.id,
        )
    assert _count(session, Appointment) == before
    assert _audits(session, "book_appointment") == []


def test_off_book_price_is_rejected_and_writes_nothing(session):
    seed(session)
    dana = _named(session, Technician, "Dana Okada")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="price is not on the book"):
        _book(
            session,
            service="drain",
            start=pt(*FRIDAY, 15, 0),
            summary="Discount drain",
            technician_id=dana.id,
            price=1,
        )
    assert _count(session, Appointment) == before
    assert _count(session, AuditEvent) == 0


def test_matching_book_price_is_stored(session):
    seed(session)
    dana = _named(session, Technician, "Dana Okada")
    booked = _book(
        session,
        service="drain",
        start=pt(*FRIDAY, 15, 0),
        summary="Priced drain",
        technician_id=dana.id,
        price=149,
    )
    assert booked.quote_amount == 149


def test_quote_rejects_an_off_book_price(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="price is not on the book"):
        create_quote(
            session,
            customer_id=james.id,
            service="faucet",
            summary="Faucet quote",
            reason="Customer asked for a price",
            price=50,
        )
    assert _count(session, Appointment) == before
    assert _audits(session, "create_quote") == []


def test_quote_requires_a_price(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="price is not on the book"):
        create_quote(
            session,
            customer_id=james.id,
            service="faucet",
            summary="Faucet quote",
            reason="Customer asked for a price",
            price=None,
        )
    assert _count(session, Appointment) == before
    assert _audits(session, "create_quote") == []


def test_quote_at_the_book_price_copies_address_and_does_not_hold_a_slot(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    quote = create_quote(
        session,
        customer_id=james.id,
        service="faucet",
        summary="Faucet quote",
        reason="Customer asked for a price",
        price=119,
    )
    assert quote.status == AppointmentStatus.quoted
    assert quote.quote_amount == 119
    assert quote.start is None
    assert quote.end is None
    assert quote.technician_id is None
    assert quote.address == james.address
    assert quote.access_notes == "Gate code 4481"
    assert_wall(quote.quote_sent_at, CLOCK)
    audits = _audits(session, "create_quote")
    assert len(audits) == 1
    assert audits[0].reasoning == "Customer asked for a price"
    assert audits[0].related_id == quote.id

    booked = book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(*FRIDAY, 13, 0),
        summary="Water heater is leaking",
        reason="Quote did not reserve the afternoon",
        technician_id=andre.id,
    )
    assert booked.status == AppointmentStatus.scheduled


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"service": "roof"}, "unknown service"),
        ({"technician_id": 9999}, "unknown technician"),
        ({"customer_id": 9999}, "unknown customer"),
        ({"reason": "   "}, "reason is required"),
        ({"summary": ""}, "summary is required"),
    ],
)
def test_rejected_booking_writes_nothing(session, overrides, message):
    seed(session)
    before_jobs = _count(session, Appointment)
    before_audits = _count(session, AuditEvent)
    with pytest.raises(PolicyError, match=message):
        _book(session, **overrides)
    assert _count(session, Appointment) == before_jobs
    assert _count(session, AuditEvent) == before_audits


def test_naive_start_is_rejected(session):
    seed(session)
    before = _count(session, Appointment)
    with pytest.raises(ValueError, match="timezone-aware"):
        _book(session, start=datetime(2026, 11, 13, 13, 0))
    assert _count(session, Appointment) == before


def test_booking_outside_the_service_area_writes_nothing(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    james.address = "900 Bellevue Way, Bellevue, WA 98004"
    session.commit()
    before = _count(session, Appointment)

    with pytest.raises(PolicyError, match="address is outside the service area"):
        _book(session)

    assert james.address == "900 Bellevue Way, Bellevue, WA 98004"
    assert _count(session, Appointment) == before
    assert _audits(session, "book_appointment") == []


def test_a_quote_outside_the_service_area_writes_nothing(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    james.address = "900 Bellevue Way, Bellevue, WA 98004"
    session.commit()
    before = _count(session, Appointment)

    with pytest.raises(PolicyError, match="address is outside the service area"):
        create_quote(
            session,
            customer_id=james.id,
            service="leak",
            summary="Leak outside the area",
            reason="Customer asked for a price",
            price=129,
        )

    assert _count(session, Appointment) == before
    assert _audits(session, "create_quote") == []


def test_an_address_without_a_zip_is_outside_the_service_area(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    james.address = "1842 N 92nd St, Seattle"
    session.commit()
    before = _count(session, Appointment)

    with pytest.raises(PolicyError, match="address is outside the service area"):
        _book(session)

    assert _count(session, Appointment) == before


def test_a_zip_plus_four_inside_the_area_can_be_booked(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    james.address = "1842 N 92nd St, Seattle, WA 98115-1234"
    session.commit()

    booked = _book(session)

    assert booked.address == "1842 N 92nd St, Seattle, WA 98115-1234"


def test_customer_without_an_address_cannot_be_booked(session):
    seed(session)
    business_id = _named(session, Customer, "James Okonkwo").business_id
    andre = _named(session, Technician, "Andre Williams")
    walker = Customer(
        business_id=business_id,
        name="Walker",
        phone="206-555-0190",
        email=None,
        address=None,
        created_at=CLOCK,
    )
    session.add(walker)
    session.commit()
    before = _count(session, Appointment)
    with pytest.raises(PolicyError, match="customer has no address"):
        book_appointment(
            session,
            customer_id=walker.id,
            service="leak",
            start=pt(*FRIDAY, 13, 0),
            summary="No address yet",
            reason="Tried to book before the address was known",
            technician_id=andre.id,
        )
    assert _count(session, Appointment) == before


def test_availability_skips_a_booked_technician_and_keeps_the_next_slot(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    slots = available_slots(session, customer_id=james.id, service="drain", day=date(2026, 11, 13))
    starts = {(slot["technician_name"], slot["start"]) for slot in slots}

    assert ("Brooke Hale", "2026-11-13T08:00:00-08:00") in starts
    assert ("Brooke Hale", "2026-11-13T09:00:00-08:00") not in starts
    assert ("Brooke Hale", "2026-11-13T10:00:00-08:00") in starts
    assert ("Dana Okada", "2026-11-13T09:00:00-08:00") in starts
    assert all(slot["technician_name"] != "Andre Williams" for slot in slots)
    assert _count(session, Appointment) == before


def test_availability_rejects_sunday_when_sunday_calls_are_off(session):
    seed(session)
    set_setting(session, "sunday_calls", False, reason="Owner turned Sunday calls off")
    james = _named(session, Customer, "James Okonkwo")
    with pytest.raises(PolicyError, match="sunday calls are off"):
        available_slots(session, customer_id=james.id, service="drain", day=date(2026, 11, 15))
