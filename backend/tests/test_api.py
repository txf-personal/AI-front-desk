"""HTTP contract for the shop API. No model is involved."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Customer, MemoryKind
from app.seed import seed
from app.services.escalations import open_escalation
from app.services.memory import remember

CLOCK = "2026-11-12T16:00:00-08:00"
NEXT_MORNING = "2026-11-13T07:00:00-08:00"


@pytest.fixture
def client(session_factory, session):
    seed(session)
    with TestClient(create_app(session_factory)) as test_client:
        yield test_client


def test_bootstrap_requires_a_shop(session_factory):
    with TestClient(create_app(session_factory)) as test_client:
        response = test_client.get("/api/bootstrap")
    assert response.status_code == 404
    assert "exactly one business" in response.json()["detail"]


def test_bootstrap_returns_the_shop_james_sams_quote_and_the_clock(client):
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "business",
        "clock",
        "settings",
        "shop_memories",
        "technicians",
        "customers",
        "appointments",
        "automations",
        "escalations",
        "audits",
    }
    assert body["business"]["name"] == "Northside Plumbing"
    assert body["business"]["owner_name"] == "Maria Chen"
    assert body["business"]["timezone"] == "America/Los_Angeles"
    assert body["clock"] == CLOCK
    assert body["settings"]["sunday_calls"] is True
    assert body["shop_memories"] == []
    assert len(body["technicians"]) == 4
    assert body["automations"] == []
    assert body["escalations"] == []
    assert body["audits"] == []

    james = next(customer for customer in body["customers"] if customer["name"] == "James Okonkwo")
    assert james["phone"] == "206-555-0101"
    assert james["address"] == "1842 N 92nd St, Seattle, WA 98115"
    assert [(memory["kind"], memory["content"]) for memory in james["memories"]] == [
        ("access", "Gate code 4481")
    ]

    sam = next(customer for customer in body["customers"] if customer["name"] == "Sam Rivera")
    quote = next(job for job in body["appointments"] if job["customer_id"] == sam["id"])
    assert quote["status"] == "quoted"
    assert quote["service"] == "drain"
    assert quote["quote_amount"] == 149
    assert quote["start"] is None
    assert quote["quote_sent_at"] == "2026-11-09T10:00:00-08:00"

    luis = next(customer for customer in body["customers"] if customer["name"] == "Luis Alvarez")
    visit = next(job for job in body["appointments"] if job["customer_id"] == luis["id"])
    assert visit["start"] == "2026-11-13T09:00:00-08:00"
    assert visit["end"] == "2026-11-13T10:00:00-08:00"


def test_bootstrap_lists_a_shop_policy_apart_from_customer_facts(client, session):
    row = remember(
        session,
        content="We stopped doing Sunday calls",
        kind=MemoryKind.policy,
        reason="Owner said Sunday calls are over",
    )
    body = client.get("/api/bootstrap").json()
    assert body["shop_memories"] == [
        {"id": row.id, "kind": "policy", "content": "We stopped doing Sunday calls"}
    ]
    james = next(customer for customer in body["customers"] if customer["name"] == "James Okonkwo")
    assert "We stopped doing Sunday calls" not in [memory["content"] for memory in james["memories"]]


def test_create_caller_persists_name_and_phone(client):
    created = client.post(
        "/api/customers",
        json={"name": "  Casey Brooks ", "phone": "206-555-0188", "address": "9 Pike St, Seattle, WA 98115"},
    )
    assert created.status_code == 201
    assert created.json()["name"] == "Casey Brooks"
    assert created.json()["phone"] == "206-555-0188"
    assert created.json()["address"] == "9 Pike St, Seattle, WA 98115"

    customers = client.get("/api/bootstrap").json()["customers"]
    match = next(customer for customer in customers if customer["phone"] == "206-555-0188")
    assert match["name"] == "Casey Brooks"
    assert match["id"] == created.json()["id"]


def test_duplicate_phone_is_rejected(client):
    before = client.get("/api/bootstrap").json()
    response = client.post("/api/customers", json={"name": "Copy of James", "phone": "206-555-0101"})
    assert response.status_code == 409
    assert response.json()["detail"] == "phone is already on file"
    assert client.get("/api/bootstrap").json()["customers"] == before["customers"]
    assert client.get("/api/bootstrap").json()["audits"] == before["audits"]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "   ", "phone": "206-555-0188"},
        {"name": "Casey Brooks", "phone": " "},
        {"name": "Casey Brooks"},
        {"phone": "206-555-0188"},
    ],
)
def test_incomplete_caller_is_rejected(client, payload):
    before = client.get("/api/bootstrap").json()["customers"]
    response = client.post("/api/customers", json=payload)
    assert response.status_code == 422
    assert client.get("/api/bootstrap").json()["customers"] == before


def test_advance_to_next_morning_changes_only_the_clock(client):
    before = client.get("/api/bootstrap").json()
    response = client.post("/api/clock/advance", json={"to": "next_morning"})
    assert response.status_code == 200
    assert response.json() == {"clock": NEXT_MORNING}

    after = client.get("/api/bootstrap").json()
    assert after["clock"] == NEXT_MORNING
    assert after["appointments"] == before["appointments"]
    assert after["customers"] == before["customers"]
    assert after["escalations"] == before["escalations"]
    assert after["audits"] == before["audits"]
    assert after["automations"] == before["automations"]


@pytest.mark.parametrize(
    ("payload", "clock"),
    [
        ({"hours": 4}, "2026-11-12T20:00:00-08:00"),
        ({"to": "next_day"}, "2026-11-13T16:00:00-08:00"),
    ],
)
def test_other_clock_advances(client, payload, clock):
    response = client.post("/api/clock/advance", json=payload)
    assert response.status_code == 200
    assert response.json()["clock"] == clock


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hours": 4, "to": "next_day"},
        {"to": "friday"},
        {"hours": 0},
        {"hours": -1},
        {"hours": 4, "extra": True},
    ],
)
def test_bad_clock_advance_leaves_the_clock(client, payload):
    response = client.post("/api/clock/advance", json=payload)
    assert response.status_code == 422
    assert client.get("/api/bootstrap").json()["clock"] == CLOCK


def test_resolve_escalation_flips_its_status(client, session):
    james = session.scalar(select(Customer).where(Customer.name == "James Okonkwo"))
    assert james is not None
    opened = open_escalation(
        session,
        customer_id=james.id,
        reason="Gas smell",
        detail="Customer is still in the house.",
    )

    response = client.post(f"/api/escalations/{opened.id}/resolve", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert response.json()["resolved_at"] == CLOCK
    assert response.json()["reason"] == "Gas smell"

    listed = client.get("/api/bootstrap").json()
    stored = next(item for item in listed["escalations"] if item["id"] == opened.id)
    assert stored["status"] == "resolved"
    assert stored["resolved_at"] == CLOCK
    audit = next(item for item in listed["audits"] if item["action"] == "resolve_escalation")
    assert audit["reasoning"] == "Owner resolved the escalation"
    assert audit["actor"] == "owner"

    again = client.post(f"/api/escalations/{opened.id}/resolve", json={"note": "Again"})
    assert again.status_code == 409
    assert again.json()["detail"] == "escalation is already resolved"
    still = next(item for item in client.get("/api/bootstrap").json()["escalations"] if item["id"] == opened.id)
    assert still["resolved_at"] == CLOCK


def test_missing_escalation_is_not_found(client):
    response = client.post("/api/escalations/9999/resolve", json={})
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown escalation"
