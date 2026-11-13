"""Unit tests for setting lookup."""

import pytest
from sqlalchemy import func, select

from app.errors import PolicyError
from app.models import AuditEvent, Business, Setting
from app.seed import seed
from app.settings_store import get_setting, set_setting


def test_get_setting_requires_exactly_one_business(session):
    with pytest.raises(LookupError, match="exactly one business"):
        get_setting(session, "hours")


def test_get_setting_rejects_two_businesses(session):
    seed(session)
    session.add(
        Business(
            name="Other Shop",
            owner_name="Pat Lee",
            timezone="America/Los_Angeles",
            phone="206-555-0199",
        )
    )
    session.commit()
    with pytest.raises(LookupError, match="exactly one business"):
        get_setting(session, "hours")


def test_missing_setting_raises_key_error(session):
    seed(session)
    with pytest.raises(KeyError, match="not-a-setting"):
        get_setting(session, "not-a-setting")


def test_get_setting_reads_the_seeded_sunday_flag(session):
    seed(session)
    assert get_setting(session, "sunday_calls") is True
    keys = set(session.scalars(select(Business.name)))
    assert keys == {"Northside Plumbing"}


def test_set_setting_replaces_the_value_and_records_why(session):
    seed(session)
    stored = set_setting(session, "sunday_calls", False, reason="We stopped doing Sunday calls")
    assert stored is False
    assert get_setting(session, "sunday_calls") is False
    audits = session.scalars(select(AuditEvent).where(AuditEvent.action == "set_setting")).all()
    assert len(audits) == 1
    assert audits[0].reasoning == "We stopped doing Sunday calls"
    assert audits[0].payload == {"key": "sunday_calls", "value": False}


def test_set_setting_rejects_a_blank_reason_without_changing_the_value(session):
    seed(session)
    with pytest.raises(PolicyError, match="reason is required"):
        set_setting(session, "sunday_calls", False, reason=" ")
    assert get_setting(session, "sunday_calls") is True
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_set_setting_rejects_an_unknown_key(session):
    seed(session)
    with pytest.raises(KeyError, match="not-a-setting"):
        set_setting(session, "not-a-setting", True, reason="Trying a new key")
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_set_setting_rejects_two_businesses(session):
    seed(session)
    session.add(
        Business(
            name="Other Shop",
            owner_name="Pat Lee",
            timezone="America/Los_Angeles",
            phone="206-555-0198",
        )
    )
    session.commit()
    with pytest.raises(LookupError, match="exactly one business"):
        set_setting(session, "sunday_calls", False, reason="Should not apply")
    value = session.scalar(select(Setting.value).where(Setting.key == "sunday_calls"))
    assert value is True
    assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0
