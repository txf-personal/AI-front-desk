"""Unit tests for setting lookup."""

import pytest
from sqlalchemy import func, select

from app.agent.loop import run_turn
from app.agent.providers import FakeLLM
from app.agent.types import Completion
from app.clock import read_clock
from app.errors import PolicyError
from app.models import AuditEvent, Business, Conversation, ConversationKind, Message, MessageRole, Setting
from app.present import build_bootstrap
from app.seed import seed
from app.settings_store import get_setting, merge_hours, set_setting, set_weekday


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


def test_closing_saturday_leaves_the_other_days(session):
    seed(session)
    assert set_weekday(session, "hours.saturday", None, reason="Saturday is closed") is None
    hours = get_setting(session, "hours")
    assert hours["saturday"] is None
    assert hours["friday"] == ["08:00", "17:00"]
    assert hours["sunday"] is None
    assert get_setting(session, "sunday_calls") is True


def test_closing_sunday_turns_calls_off(session):
    seed(session)
    assert set_weekday(session, "hours.sunday", False, reason="Sunday is closed") is None
    assert get_setting(session, "sunday_calls") is False
    assert get_setting(session, "hours")["monday"] == ["08:00", "17:00"]


def test_opening_sunday_uses_monday_hours(session):
    seed(session)
    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    assert set_weekday(session, "sunday", True, reason="Sunday is open again") == ["08:00", "17:00"]
    assert get_setting(session, "sunday_calls") is True
    assert get_setting(session, "hours")["sunday"] == ["08:00", "17:00"]
    assert get_setting(session, "hours")["saturday"] == ["08:00", "17:00"]


def test_a_bad_weekday_is_an_unknown_setting(session):
    seed(session)
    before = get_setting(session, "hours")
    with pytest.raises(PolicyError, match="unknown setting"):
        set_weekday(session, "hours.funday", None, reason="Not a day")
    assert get_setting(session, "hours") == before


def test_a_shorter_day_is_rejected(session):
    seed(session)
    before = get_setting(session, "hours")
    with pytest.raises(PolicyError, match="not supported"):
        set_weekday(session, "monday", ["08:00", "09:00"], reason="Monday now ends at 9")
    assert get_setting(session, "hours") == before
    with pytest.raises(PolicyError, match="not supported"):
        merge_hours(session, {"tuesday": ["09:00", "10:00"]}, reason="Tuesday morning only")
    assert get_setting(session, "hours") == before
    assert set_weekday(session, "monday", ["08:00", "17:00"], reason="Monday stays open") == [
        "08:00",
        "17:00",
    ]


def test_a_partial_hours_update_keeps_the_other_days(session):
    seed(session)
    set_weekday(session, "saturday", None, reason="Saturday is closed")

    stored = merge_hours(session, {"tuesday": None}, reason="Tuesday is closed")

    assert stored["tuesday"] is None
    assert stored["saturday"] is None
    assert stored["monday"] == ["08:00", "17:00"]
    assert stored["friday"] == ["08:00", "17:00"]
    assert get_setting(session, "sunday_calls") is True


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


def test_the_owner_can_add_a_service_area_zip(session):
    seed(session)
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])
    turn = run_turn(session, fake, audience="owner", body="Add service area 98104")
    assert fake.calls == []
    assert turn.reply == "98104 is now in the service area."
    assert get_setting(session, "service_area") == ["98103", "98115", "98125", "98133", "98177", "98104"]
    again = run_turn(session, fake, audience="owner", body="Add service area 98104")
    assert again.reply == "98104 is already in the service area."
    assert get_setting(session, "service_area").count("98104") == 1


def test_an_earlier_service_area_request_is_applied(session):
    seed(session)
    business_id = session.scalar(select(Setting.business_id).limit(1))
    conversation = Conversation(
        business_id=business_id,
        customer_id=None,
        kind=ConversationKind.owner,
        created_at=read_clock(session),
    )
    session.add(conversation)
    session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.owner,
            body="Add service area 98104",
            created_at=read_clock(session),
        )
    )
    session.commit()

    shop = build_bootstrap(session)

    assert shop["settings"]["service_area"][-1] == "98104"
    bodies = [message["body"] for message in shop["conversations"][0]["messages"]]
    assert bodies[-1] == "98104 is now in the service area."
    build_bootstrap(session)
    again = [message["body"] for message in build_bootstrap(session)["conversations"][0]["messages"]]
    assert again.count("98104 is now in the service area.") == 1
