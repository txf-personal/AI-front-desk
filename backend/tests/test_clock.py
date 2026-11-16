"""Unit tests for shop-clock arithmetic and persistence."""

import math
from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy import func, select

from app.clock import (
    SHOP_TZ,
    ClockNotSeeded,
    advance_clock,
    advance_time,
    parse_shop_time,
    read_clock,
    set_clock,
)
from app.db import create_session_factory, create_sqlite_engine, init_db
from app.models import Appointment, AppointmentStatus, Customer
from app.seed import seed
from tests.times import PT, assert_wall, pt

THURSDAY_AFTERNOON = pt(2026, 11, 12, 16, 0)
FRIDAY_MORNING = pt(2026, 11, 13, 7, 0)
FRIDAY_AFTERNOON = pt(2026, 11, 13, 16, 0)


@pytest.mark.parametrize(
    ("current", "kwargs", "expected"),
    [
        pytest.param(THURSDAY_AFTERNOON, {"to": "next_morning"}, FRIDAY_MORNING, id="next-morning-from-afternoon"),
        pytest.param(THURSDAY_AFTERNOON, {"to": "next_day"}, FRIDAY_AFTERNOON, id="next-day-keeps-wall-time"),
        pytest.param(THURSDAY_AFTERNOON, {"hours": 4}, pt(2026, 11, 12, 20, 0), id="plus-four-hours"),
        pytest.param(THURSDAY_AFTERNOON, {"hours": 0.5}, pt(2026, 11, 12, 16, 30), id="plus-half-hour"),
        pytest.param(pt(2026, 11, 12, 6, 59), {"to": "next_morning"}, pt(2026, 11, 12, 7, 0), id="morning-still-ahead"),
        pytest.param(pt(2026, 11, 12, 7, 0), {"to": "next_morning"}, FRIDAY_MORNING, id="exactly-seven-is-already-morning"),
        pytest.param(
            datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc),
            {"to": "next_morning"},
            FRIDAY_MORNING,
            id="utc-input-uses-shop-wall-time",
        ),
        pytest.param(
            datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc),
            {"to": "next_day"},
            FRIDAY_AFTERNOON,
            id="utc-next-day-is-one-shop-day",
        ),
    ],
)
def test_advance_time(current, kwargs, expected):
    result = advance_time(current, **kwargs)
    assert result == expected
    assert result.tzinfo == SHOP_TZ


def test_advance_time_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        advance_time(datetime(2026, 11, 12, 16, 0), to="next_morning")


@pytest.mark.parametrize("hours", [True, False, "4", math.nan, math.inf, -math.inf])
def test_advance_time_rejects_non_numeric_hours(hours):
    with pytest.raises(ValueError, match="positive number"):
        advance_time(THURSDAY_AFTERNOON, hours=hours)


@pytest.mark.parametrize("hours", [0, 0.0, -1, -0.5])
def test_advance_time_rejects_non_positive_hours(hours):
    with pytest.raises(ValueError, match="hours must be positive"):
        advance_time(THURSDAY_AFTERNOON, hours=hours)


def test_advance_time_requires_exactly_one_change():
    with pytest.raises(ValueError, match="exactly one"):
        advance_time(THURSDAY_AFTERNOON)
    with pytest.raises(ValueError, match="exactly one"):
        advance_time(THURSDAY_AFTERNOON, hours=4, to="next_day")
    with pytest.raises(ValueError, match="exactly one"):
        advance_time(THURSDAY_AFTERNOON, hours=4, until=FRIDAY_MORNING)


def test_a_chosen_time_must_be_later_and_uses_the_shop_zone():
    assert parse_shop_time("2026-11-13T07:00") == FRIDAY_MORNING
    assert advance_time(THURSDAY_AFTERNOON, until=parse_shop_time("2026-11-13T07:30")) == pt(2026, 11, 13, 7, 30)
    with pytest.raises(ValueError, match="clock only moves forward"):
        advance_time(THURSDAY_AFTERNOON, until=THURSDAY_AFTERNOON)
    with pytest.raises(ValueError, match="clock only moves forward"):
        advance_time(THURSDAY_AFTERNOON, until=pt(2026, 11, 12, 15, 59))
    with pytest.raises(ValueError, match="date and time"):
        parse_shop_time("tomorrow")


def test_setting_a_later_minute_persists(session, session_factory):
    seed(session)
    assert advance_clock(session, until=parse_shop_time("2026-11-13T07:00")) == FRIDAY_MORNING
    with session_factory() as other:
        assert read_clock(other) == FRIDAY_MORNING


@pytest.mark.parametrize("target", ["friday", ""])
def test_advance_time_rejects_unknown_target(target):
    with pytest.raises(ValueError, match="unknown clock target"):
        advance_time(THURSDAY_AFTERNOON, to=target)


def test_seeded_clock_starts_thursday_afternoon(session):
    seed(session)
    got = read_clock(session)
    assert got.tzinfo == PT
    assert got == THURSDAY_AFTERNOON
    assert got.strftime("%A") == "Thursday"
    assert got.utcoffset() == timedelta(hours=-8)


def test_read_clock_before_seed_raises(session):
    with pytest.raises(ClockNotSeeded, match="not seeded"):
        read_clock(session)


def test_set_clock_before_seed_raises(session):
    with pytest.raises(ClockNotSeeded, match="not seeded"):
        set_clock(session, THURSDAY_AFTERNOON)


def test_set_clock_rejects_naive_datetime(session):
    seed(session)
    with pytest.raises(ValueError, match="timezone-aware"):
        set_clock(session, datetime(2026, 11, 12, 16, 0))
    assert read_clock(session) == THURSDAY_AFTERNOON


def test_set_clock_normalizes_utc_to_shop_time(session):
    seed(session)
    got = set_clock(session, datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc))
    assert got.tzinfo == PT
    assert got == THURSDAY_AFTERNOON
    assert read_clock(session) == THURSDAY_AFTERNOON


def test_rejected_advance_leaves_the_clock_unchanged(session):
    seed(session)
    with pytest.raises(ValueError):
        advance_clock(session, hours=0)
    assert read_clock(session) == THURSDAY_AFTERNOON


def test_advance_to_next_morning_persists_across_sessions(session, session_factory):
    seed(session)
    assert advance_clock(session, to="next_morning") == FRIDAY_MORNING
    with session_factory() as other:
        got = read_clock(other)
    assert got == FRIDAY_MORNING
    assert got.strftime("%A") == "Friday"


def test_advance_one_day_persists(session, session_factory):
    seed(session)
    assert advance_clock(session, to="next_day") == FRIDAY_AFTERNOON
    with session_factory() as other:
        assert read_clock(other) == FRIDAY_AFTERNOON


def test_advance_hours_persists(session, session_factory):
    seed(session)
    assert advance_clock(session, hours=4) == pt(2026, 11, 12, 20, 0)
    with session_factory() as other:
        assert read_clock(other) == pt(2026, 11, 12, 20, 0)


def test_clock_survives_reopening_a_file_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'shop.db'}"
    engine = create_sqlite_engine(url)
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as db:
        seed(db)
        advance_clock(db, to="next_morning")
    engine.dispose()

    reopened = create_sqlite_engine(url)
    factory = create_session_factory(reopened)
    with factory() as db:
        assert read_clock(db) == FRIDAY_MORNING
        assert db.scalar(select(func.count()).select_from(Customer)) == 5
    reopened.dispose()


def test_a_visit_is_completed_when_the_clock_passes_its_end(session):
    seed(session)
    luis = session.scalar(
        select(Appointment).where(
            Appointment.status == AppointmentStatus.scheduled,
            Appointment.summary == "Main bathroom drain",
        )
    )
    assert luis is not None
    advance_clock(session, until=parse_shop_time("2026-11-13T09:30"))
    session.refresh(luis)
    assert luis.status == AppointmentStatus.scheduled

    advance_clock(session, until=parse_shop_time("2026-11-13T10:00"))
    session.refresh(luis)
    assert luis.status == AppointmentStatus.completed


def test_next_morning_from_seed_is_seventeen_hours_before_luis_visit(session):
    """The seeded week is arranged so one jump reaches Friday morning."""
    seed(session)
    morning = advance_clock(session, to="next_morning")
    luis = session.scalar(select(Appointment).where(Appointment.status == AppointmentStatus.scheduled))
    assert luis is not None
    assert_wall(luis.start, morning + timedelta(hours=2))
