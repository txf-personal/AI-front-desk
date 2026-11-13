"""Unit tests for table constraints and the datetime column."""

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError

from app.db import init_db
from app.models import Customer, Memory, MemoryKind, Setting, SimClock
from app.seed import seed
from tests.times import pt


def test_init_db_can_run_twice(engine, session):
    init_db(engine)
    business = seed(session)
    assert business.name == "Northside Plumbing"


def test_datetime_column_rejects_naive_values(session):
    business = seed(session)
    session.add(
        Memory(
            business_id=business.id,
            customer_id=None,
            kind=MemoryKind.policy,
            content="naive stamp",
            created_at=datetime(2026, 11, 12, 16, 0),
            active=True,
        )
    )
    with pytest.raises(StatementError, match="timezone-aware") as raised:
        session.flush()
    assert isinstance(raised.value.orig, ValueError)
    session.rollback()


def test_business_memory_does_not_need_a_customer(session):
    business = seed(session)
    session.add(
        Memory(
            business_id=business.id,
            customer_id=None,
            kind=MemoryKind.policy,
            content="We stopped doing Sunday calls",
            created_at=pt(2026, 11, 12, 16, 0),
            active=True,
        )
    )
    session.commit()
    stored = session.scalar(select(Memory).where(Memory.content == "We stopped doing Sunday calls"))
    assert stored is not None
    assert stored.customer_id is None
    assert stored.kind == MemoryKind.policy


def test_duplicate_customer_phone_is_rejected(session):
    business = seed(session)
    session.add(
        Customer(
            business_id=business.id,
            name="Copy",
            phone="206-555-0101",
            email="copy@example.com",
            address="1 Main St, Seattle, WA 98103",
            created_at=pt(2026, 1, 15, 10, 0),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_memory_customer_must_exist(session):
    business = seed(session)
    session.add(
        Memory(
            business_id=business.id,
            customer_id=10_000,
            kind=MemoryKind.fact,
            content="missing customer",
            created_at=pt(2026, 11, 12, 16, 0),
            active=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_memory_business_must_exist(session):
    seed(session)
    session.add(
        Memory(
            business_id=10_000,
            customer_id=None,
            kind=MemoryKind.fact,
            content="missing business",
            created_at=pt(2026, 11, 12, 16, 0),
            active=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_only_one_clock_per_business(session):
    business = seed(session)
    session.add(SimClock(business_id=business.id, current_time=pt(2026, 11, 12, 16, 0)))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_duplicate_setting_key_is_rejected(session):
    business = seed(session)
    session.add(Setting(business_id=business.id, key="hours", value={"sunday": None}))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
