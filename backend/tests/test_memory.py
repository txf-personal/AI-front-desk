"""Unit tests for customer and shop memories."""

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import AuditEvent, Customer, Memory, MemoryKind
from app.seed import seed
from app.services.memory import active_memories, business_memories, remember
from tests.times import assert_wall, pt

CLOCK = pt(2026, 11, 12, 16, 0)


def _customer(session, name: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.name == name))
    assert customer is not None
    return customer


def _facts(session, customer_id: int) -> list[tuple[MemoryKind, str]]:
    return [(memory.kind, memory.content) for memory in active_memories(session, customer_id)]


def test_a_saved_fact_is_returned_only_for_that_customer(session, session_factory):
    seed(session)
    priya = _customer(session, "Priya Shah")
    james = _customer(session, "James Okonkwo")
    saved = remember(
        session,
        customer_id=priya.id,
        kind=MemoryKind.fact,
        content="Likes text reminders",
        reason="Priya asked to be texted",
    )

    assert _facts(session, priya.id) == [
        (MemoryKind.preference, "Prefers morning appointments"),
        (MemoryKind.fact, "Dog in the yard"),
        (MemoryKind.fact, "Likes text reminders"),
    ]
    assert _facts(session, james.id) == [(MemoryKind.access, "Gate code 4481")]
    assert business_memories(session) == []
    assert saved.active is True
    assert_wall(saved.created_at, CLOCK)

    audits = session.scalars(select(AuditEvent).where(AuditEvent.action == "remember")).all()
    assert len(audits) == 1
    assert audits[0].reasoning == "Priya asked to be texted"
    assert audits[0].related_id == saved.id

    with session_factory() as other:
        again = _facts(other, priya.id)
    assert (MemoryKind.fact, "Likes text reminders") in again
    assert (MemoryKind.access, "Gate code 4481") not in again


def test_shop_policy_is_not_listed_on_a_customer(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    priya = _customer(session, "Priya Shah")
    remember(
        session,
        kind=MemoryKind.policy,
        content="We stopped doing Sunday calls",
        reason="Maria said Sunday calls have stopped",
    )

    assert [(memory.kind, memory.content) for memory in business_memories(session)] == [
        (MemoryKind.policy, "We stopped doing Sunday calls"),
    ]
    assert (MemoryKind.policy, "We stopped doing Sunday calls") not in _facts(session, james.id)
    assert (MemoryKind.policy, "We stopped doing Sunday calls") not in _facts(session, priya.id)
    assert (MemoryKind.access, "Gate code 4481") not in [
        (memory.kind, memory.content) for memory in business_memories(session)
    ]


def test_inactive_memory_is_hidden(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    gate = session.scalar(select(Memory).where(Memory.customer_id == james.id))
    assert gate is not None
    gate.active = False
    session.commit()
    assert active_memories(session, james.id) == []


@pytest.mark.parametrize("content", ["", "   "])
def test_blank_memory_is_rejected(session, content):
    seed(session)
    priya = _customer(session, "Priya Shah")
    before = session.scalar(select(func.count()).select_from(Memory))
    with pytest.raises(PolicyError, match="memory is required"):
        remember(
            session,
            customer_id=priya.id,
            kind=MemoryKind.fact,
            content=content,
            reason="Nothing to store",
        )
    assert session.scalar(select(func.count()).select_from(Memory)) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_blank_reason_does_not_store_a_memory(session):
    seed(session)
    priya = _customer(session, "Priya Shah")
    before = session.scalar(select(func.count()).select_from(Memory))
    with pytest.raises(PolicyError, match="reason is required"):
        remember(
            session,
            customer_id=priya.id,
            kind=MemoryKind.fact,
            content="Has a spare key",
            reason="  ",
        )
    assert session.scalar(select(func.count()).select_from(Memory)) == before


def test_unknown_customer_is_rejected(session):
    seed(session)
    before = session.scalar(select(func.count()).select_from(Memory))
    with pytest.raises(PolicyError, match="unknown customer"):
        remember(session, customer_id=9999, kind=MemoryKind.fact, content="Nope", reason="Missing person")
    assert session.scalar(select(func.count()).select_from(Memory)) == before


def test_unknown_memory_kind_is_rejected(session):
    seed(session)
    priya = _customer(session, "Priya Shah")
    with pytest.raises(PolicyError, match="unknown memory kind"):
        remember(session, customer_id=priya.id, kind="fact", content="Nope", reason="Bad kind")


def test_unknown_customer_cannot_be_listed(session):
    seed(session)
    with pytest.raises(PolicyError, match="unknown customer"):
        active_memories(session, 9999)
