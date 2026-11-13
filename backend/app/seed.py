"""Northside Plumbing sample week.

The walk-in who texts about a leaking water heater is not seeded. That person
shows up during the demo. Elena Vasquez is the fifth customer already on file,
with a completed faucet visit.

Standing hours are Monday through Saturday, 08:00-17:00. Sunday calls start
allowed, so the owner can turn them off later and the change is visible.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ
from app.models import (
    Appointment,
    AppointmentStatus,
    Business,
    Customer,
    Memory,
    MemoryKind,
    Setting,
    SimClock,
    Technician,
)

ONBOARDED_AT = datetime(2026, 1, 15, 10, 0, tzinfo=SHOP_TZ)
CLOCK_START = datetime(2026, 11, 12, 16, 0, tzinfo=SHOP_TZ)

PRICE_BOOK = {
    "services": [
        {
            "id": "water_heater_diagnostic",
            "name": "Water heater diagnostic",
            "duration_min": 90,
            "price": 89,
            "skills": ["water_heater"],
        },
        {
            "id": "drain",
            "name": "Drain cleaning",
            "duration_min": 60,
            "price": 149,
            "skills": ["drain"],
        },
        {
            "id": "leak",
            "name": "Leak repair visit",
            "duration_min": 90,
            "price": 129,
            "skills": ["leak"],
        },
        {
            "id": "faucet",
            "name": "Faucet/toilet",
            "duration_min": 60,
            "price": 119,
            "skills": ["fixture"],
        },
    ]
}

HOURS = {
    "monday": ["08:00", "17:00"],
    "tuesday": ["08:00", "17:00"],
    "wednesday": ["08:00", "17:00"],
    "thursday": ["08:00", "17:00"],
    "friday": ["08:00", "17:00"],
    "saturday": ["08:00", "17:00"],
    "sunday": None,
}

SERVICE_AREA = ["98103", "98115", "98125", "98133", "98177"]

TECHNICIANS = (
    ("Andre Williams", ["water_heater", "leak"]),
    ("Brooke Hale", ["drain", "fixture"]),
    ("Chris Patel", ["leak", "fixture", "water_heater"]),
    ("Dana Okada", ["drain", "water_heater"]),
)

CUSTOMERS = (
    {
        "name": "James Okonkwo",
        "phone": "206-555-0101",
        "email": "james.okonkwo@example.com",
        "address": "1842 N 92nd St, Seattle, WA 98115",
        "memories": ((MemoryKind.access, "Gate code 4481"),),
    },
    {
        "name": "Priya Shah",
        "phone": "206-555-0102",
        "email": "priya.shah@example.com",
        "address": "4107 Linden Ave N, Seattle, WA 98103",
        "memories": (
            (MemoryKind.preference, "Prefers morning appointments"),
            (MemoryKind.fact, "Dog in the yard"),
        ),
    },
    {
        "name": "Sam Rivera",
        "phone": "206-555-0103",
        "email": "sam.rivera@example.com",
        "address": "1205 NE 115th St, Seattle, WA 98125",
        "memories": (),
    },
    {
        "name": "Luis Alvarez",
        "phone": "206-555-0104",
        "email": "luis.alvarez@example.com",
        "address": "5515 8th Ave NE, Seattle, WA 98103",
        "memories": (),
    },
    {
        "name": "Elena Vasquez",
        "phone": "206-555-0105",
        "email": "elena.vasquez@example.com",
        "address": "2100 N 80th St, Seattle, WA 98103",
        "memories": (),
    },
)


def seed(session: Session) -> Business:
    """Insert the sample shop. A second call leaves existing rows and the clock alone."""
    existing = session.scalar(select(Business).limit(1))
    if existing is not None:
        return existing

    business = Business(
        name="Northside Plumbing",
        owner_name="Maria Chen",
        timezone="America/Los_Angeles",
        phone="206-555-0148",
    )
    session.add(business)
    session.flush()

    session.add_all(
        [
            Setting(business_id=business.id, key="hours", value=HOURS),
            Setting(business_id=business.id, key="sunday_calls", value=True),
            Setting(business_id=business.id, key="service_area", value=SERVICE_AREA),
            Setting(business_id=business.id, key="price_book", value=PRICE_BOOK),
        ]
    )
    session.add_all(
        Technician(business_id=business.id, name=name, skills=skills) for name, skills in TECHNICIANS
    )
    session.flush()

    customers = _insert_customers(session, business.id)
    technicians = {
        row.name: row
        for row in session.scalars(select(Technician).where(Technician.business_id == business.id))
    }
    _insert_appointments(session, business.id, customers, technicians)
    session.add(SimClock(business_id=business.id, current_time=CLOCK_START))
    session.commit()
    return business


def _insert_customers(session: Session, business_id: int) -> dict[str, Customer]:
    rows: dict[str, Customer] = {}
    for spec in CUSTOMERS:
        customer = Customer(
            business_id=business_id,
            name=spec["name"],
            phone=spec["phone"],
            email=spec["email"],
            address=spec["address"],
            created_at=ONBOARDED_AT,
        )
        session.add(customer)
        session.flush()
        for kind, content in spec["memories"]:
            session.add(
                Memory(
                    business_id=business_id,
                    customer_id=customer.id,
                    kind=kind,
                    content=content,
                    created_at=ONBOARDED_AT,
                    active=True,
                )
            )
        rows[customer.name] = customer
    return rows


def _insert_appointments(session, business_id, customers, technicians) -> None:
    sam = customers["Sam Rivera"]
    quote_sent = datetime(2026, 11, 9, 10, 0, tzinfo=SHOP_TZ)
    session.add(
        Appointment(
            business_id=business_id,
            customer_id=sam.id,
            technician_id=None,
            service="drain",
            summary="Kitchen sink draining slowly",
            status=AppointmentStatus.quoted,
            start=None,
            end=None,
            quote_amount=149,
            quote_sent_at=quote_sent,
            last_customer_message_at=quote_sent,
            address=sam.address,
            access_notes=None,
        )
    )

    luis = customers["Luis Alvarez"]
    session.add(
        Appointment(
            business_id=business_id,
            customer_id=luis.id,
            technician_id=technicians["Brooke Hale"].id,
            service="drain",
            summary="Main bathroom drain",
            status=AppointmentStatus.scheduled,
            start=datetime(2026, 11, 13, 9, 0, tzinfo=SHOP_TZ),
            end=datetime(2026, 11, 13, 10, 0, tzinfo=SHOP_TZ),
            quote_amount=None,
            quote_sent_at=None,
            last_customer_message_at=datetime(2026, 11, 10, 15, 0, tzinfo=SHOP_TZ),
            address=luis.address,
            access_notes=None,
        )
    )

    elena = customers["Elena Vasquez"]
    visit = datetime(2026, 11, 4, 10, 0, tzinfo=SHOP_TZ)
    session.add(
        Appointment(
            business_id=business_id,
            customer_id=elena.id,
            technician_id=technicians["Chris Patel"].id,
            service="faucet",
            summary="Kitchen faucet cartridge",
            status=AppointmentStatus.completed,
            start=visit,
            end=datetime(2026, 11, 4, 11, 0, tzinfo=SHOP_TZ),
            quote_amount=None,
            quote_sent_at=None,
            last_customer_message_at=visit,
            address=elena.address,
            access_notes=None,
        )
    )
