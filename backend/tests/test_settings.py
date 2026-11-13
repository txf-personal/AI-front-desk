"""Unit tests for setting lookup."""

import pytest
from sqlalchemy import select

from app.models import Business
from app.seed import seed
from app.settings_store import get_setting


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
