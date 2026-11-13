"""Unit tests for the Northside Plumbing seed."""

from datetime import timedelta

from sqlalchemy import func, select

from app.clock import advance_clock, read_clock
from app.models import (
    Appointment,
    AppointmentStatus,
    AuditEvent,
    Automation,
    AutomationRun,
    Business,
    Conversation,
    Customer,
    Escalation,
    Memory,
    MemoryKind,
    Message,
    Setting,
    SimClock,
    Technician,
)
from app.seed import seed
from app.settings_store import get_setting
from tests.times import assert_wall, pt

ONBOARDED = pt(2026, 1, 15, 10, 0)
THURSDAY_AFTERNOON = pt(2026, 11, 12, 16, 0)

EXPECTED_COUNTS = {
    Business: 1,
    Setting: 4,
    Technician: 4,
    Customer: 5,
    Memory: 3,
    Appointment: 3,
    Conversation: 0,
    Message: 0,
    Automation: 0,
    AutomationRun: 0,
    AuditEvent: 0,
    Escalation: 0,
    SimClock: 1,
}

CUSTOMER_ROSTER = {
    "James Okonkwo": {
        "phone": "206-555-0101",
        "email": "james.okonkwo@example.com",
        "address": "1842 N 92nd St, Seattle, WA 98115",
    },
    "Priya Shah": {
        "phone": "206-555-0102",
        "email": "priya.shah@example.com",
        "address": "4107 Linden Ave N, Seattle, WA 98103",
    },
    "Sam Rivera": {
        "phone": "206-555-0103",
        "email": "sam.rivera@example.com",
        "address": "1205 NE 115th St, Seattle, WA 98125",
    },
    "Luis Alvarez": {
        "phone": "206-555-0104",
        "email": "luis.alvarez@example.com",
        "address": "5515 8th Ave NE, Seattle, WA 98103",
    },
    "Elena Vasquez": {
        "phone": "206-555-0105",
        "email": "elena.vasquez@example.com",
        "address": "2100 N 80th St, Seattle, WA 98103",
    },
}


def _counts(session):
    return {
        model: session.scalar(select(func.count()).select_from(model))
        for model in EXPECTED_COUNTS
    }


def _customer(session, name: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.name == name))
    assert customer is not None
    return customer


def _appointments(session, customer: Customer) -> list[Appointment]:
    return list(session.scalars(select(Appointment).where(Appointment.customer_id == customer.id)))


def test_seed_row_counts(session):
    seed(session)
    assert _counts(session) == EXPECTED_COUNTS


def test_business_is_northside(session):
    business = seed(session)
    assert business.name == "Northside Plumbing"
    assert business.owner_name == "Maria Chen"
    assert business.timezone == "America/Los_Angeles"
    assert business.phone == "206-555-0148"
    assert session.scalar(select(func.count()).select_from(Business)) == 1


def test_four_technicians_and_their_skills(session):
    seed(session)
    technicians = session.scalars(select(Technician)).all()
    assert {tech.name: tech.skills for tech in technicians} == {
        "Andre Williams": ["water_heater", "leak"],
        "Brooke Hale": ["drain", "fixture"],
        "Chris Patel": ["leak", "fixture", "water_heater"],
        "Dana Okada": ["drain", "water_heater"],
    }


def test_price_book_and_shop_rules(session):
    seed(session)
    assert get_setting(session, "sunday_calls") is True
    assert get_setting(session, "service_area") == ["98103", "98115", "98125", "98133", "98177"]
    assert get_setting(session, "hours") == {
        "monday": ["08:00", "17:00"],
        "tuesday": ["08:00", "17:00"],
        "wednesday": ["08:00", "17:00"],
        "thursday": ["08:00", "17:00"],
        "friday": ["08:00", "17:00"],
        "saturday": ["08:00", "17:00"],
        "sunday": None,
    }
    services = {item["id"]: item for item in get_setting(session, "price_book")["services"]}
    assert services == {
        "water_heater_diagnostic": {
            "id": "water_heater_diagnostic",
            "name": "Water heater diagnostic",
            "duration_min": 90,
            "price": 89,
            "skills": ["water_heater"],
        },
        "drain": {
            "id": "drain",
            "name": "Drain cleaning",
            "duration_min": 60,
            "price": 149,
            "skills": ["drain"],
        },
        "leak": {
            "id": "leak",
            "name": "Leak repair visit",
            "duration_min": 90,
            "price": 129,
            "skills": ["leak"],
        },
        "faucet": {
            "id": "faucet",
            "name": "Faucet/toilet",
            "duration_min": 60,
            "price": 119,
            "skills": ["fixture"],
        },
    }


def test_five_customers_on_file_since_january(session):
    seed(session)
    customers = session.scalars(select(Customer)).all()
    assert {customer.name: {
        "phone": customer.phone,
        "email": customer.email,
        "address": customer.address,
    } for customer in customers} == CUSTOMER_ROSTER
    for customer in customers:
        assert_wall(customer.created_at, ONBOARDED)


def test_every_customer_address_is_inside_the_service_area(session):
    seed(session)
    zips = set(get_setting(session, "service_area"))
    for customer in session.scalars(select(Customer)):
        assert customer.address is not None
        assert customer.address[-5:] in zips


def test_james_gate_code_is_only_his(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    memories = session.scalars(select(Memory)).all()
    hits = [memory for memory in memories if "4481" in memory.content]
    assert len(hits) == 1
    assert hits[0].customer_id == james.id
    assert hits[0].kind == MemoryKind.access
    assert hits[0].content == "Gate code 4481"
    assert hits[0].active is True
    assert hits[0].source_message_id is None
    assert_wall(hits[0].created_at, ONBOARDED)
    assert _appointments(session, james) == []


def test_priya_preferences(session):
    seed(session)
    priya = _customer(session, "Priya Shah")
    memories = session.scalars(select(Memory).where(Memory.customer_id == priya.id)).all()
    assert {(memory.kind, memory.content) for memory in memories} == {
        (MemoryKind.preference, "Prefers morning appointments"),
        (MemoryKind.fact, "Dog in the yard"),
    }
    assert all(memory.active is True for memory in memories)
    assert _appointments(session, priya) == []


def test_sam_quote_was_sent_monday_and_is_not_booked(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    jobs = _appointments(session, sam)
    assert len(jobs) == 1
    quote = jobs[0]
    sent = pt(2026, 11, 9, 10, 0)
    assert quote.status == AppointmentStatus.quoted
    assert quote.service == "drain"
    assert quote.summary == "Kitchen sink draining slowly"
    assert quote.technician_id is None
    assert quote.start is None
    assert quote.end is None
    assert quote.quote_amount == 149
    assert quote.quote_amount == next(
        item["price"] for item in get_setting(session, "price_book")["services"] if item["id"] == "drain"
    )
    assert_wall(quote.quote_sent_at, sent)
    assert quote.quote_sent_at is not None
    assert quote.quote_sent_at.astimezone(sent.tzinfo).strftime("%A") == "Monday"
    assert_wall(quote.last_customer_message_at, sent)
    assert quote.address == sam.address
    assert quote.access_notes is None
    assert read_clock(session) - quote.quote_sent_at == timedelta(days=3, hours=6)


def test_luis_is_booked_friday_at_nine(session):
    seed(session)
    luis = _customer(session, "Luis Alvarez")
    jobs = _appointments(session, luis)
    assert len(jobs) == 1
    visit = jobs[0]
    start = pt(2026, 11, 13, 9, 0)
    assert visit.status == AppointmentStatus.scheduled
    assert visit.service == "drain"
    assert visit.summary == "Main bathroom drain"
    assert_wall(visit.start, start)
    assert visit.start is not None
    assert visit.start.astimezone(start.tzinfo).strftime("%A") == "Friday"
    assert_wall(visit.end, pt(2026, 11, 13, 10, 0))
    assert visit.quote_amount is None
    assert visit.quote_sent_at is None
    assert_wall(visit.last_customer_message_at, pt(2026, 11, 10, 15, 0))
    assert visit.address == luis.address
    assert visit.access_notes is None
    technician = session.get(Technician, visit.technician_id)
    assert technician is not None
    assert technician.name == "Brooke Hale"
    assert "drain" in technician.skills
    assert visit.start - read_clock(session) == timedelta(hours=17)


def test_elena_has_a_completed_faucet_visit(session):
    seed(session)
    elena = _customer(session, "Elena Vasquez")
    jobs = _appointments(session, elena)
    assert len(jobs) == 1
    visit = jobs[0]
    assert visit.status == AppointmentStatus.completed
    assert visit.service == "faucet"
    assert visit.summary == "Kitchen faucet cartridge"
    assert_wall(visit.start, pt(2026, 11, 4, 10, 0))
    assert visit.start is not None
    assert visit.start.astimezone(pt(2026, 11, 4).tzinfo).strftime("%A") == "Wednesday"
    assert_wall(visit.end, pt(2026, 11, 4, 11, 0))
    assert visit.quote_amount is None
    assert visit.quote_sent_at is None
    assert_wall(visit.last_customer_message_at, pt(2026, 11, 4, 10, 0))
    assert visit.address == elena.address
    technician = session.get(Technician, visit.technician_id)
    assert technician is not None
    assert technician.name == "Chris Patel"
    assert "fixture" in technician.skills


def test_seed_twice_does_not_duplicate_rows_or_primary_keys(session):
    first = seed(session)
    customer_ids = session.scalars(select(Customer.id).order_by(Customer.id)).all()
    memory_ids = session.scalars(select(Memory.id).order_by(Memory.id)).all()
    appointment_ids = session.scalars(select(Appointment.id).order_by(Appointment.id)).all()
    second = seed(session)
    assert second.id == first.id
    assert _counts(session) == EXPECTED_COUNTS
    assert session.scalars(select(Customer.id).order_by(Customer.id)).all() == customer_ids
    assert session.scalars(select(Memory.id).order_by(Memory.id)).all() == memory_ids
    assert session.scalars(select(Appointment.id).order_by(Appointment.id)).all() == appointment_ids


def test_reseed_does_not_rewind_the_clock(session):
    seed(session)
    advance_clock(session, hours=4)
    moved = read_clock(session)
    seed(session)
    assert read_clock(session) == moved
    assert moved == pt(2026, 11, 12, 20, 0)
    assert _counts(session) == EXPECTED_COUNTS
