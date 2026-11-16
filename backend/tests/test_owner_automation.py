"""The owner can ask for a weekly quote report. A customer cannot."""

import pytest
from sqlalchemy import func, select

from fastapi.testclient import TestClient

from app.agent.loop import run_turn
from app.agent.providers import FakeLLM
from app.agent.types import Completion, ToolCall
from app.clock import advance_clock, parse_shop_time
from app.errors import PolicyError
from app.main import create_app
from app.models import (
    AuditEvent,
    Automation,
    AutomationAuthor,
    AutomationKind,
    Conversation,
    ConversationKind,
    Customer,
    MemoryKind,
    Message,
    MessageRole,
    Technician,
)
from app.present import build_bootstrap
from app.seed import seed
from app.services.booking import book_appointment
from app.services.memory import remember
from app.services.runner import run_due
from tests.times import assert_wall, pt

THURSDAY = pt(2026, 11, 12, 16, 0)
FRIDAY_MORNING = pt(2026, 11, 13, 7, 0)
FRIDAY_REPORT = pt(2026, 11, 13, 7, 30)
WORDS = "Every Friday, tell me who is still waiting on a quote."
REASON = "Owner asked for a weekly quote report"
REPORT = (
    "Maria Chen, quotes still waiting on Friday, November 13: "
    "Sam Rivera, Drain cleaning for $149 (Kitchen sink draining slowly)."
)


def _customer(session, name: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.name == name))
    assert customer is not None
    return customer


def _ask(session, *, audience: str, customer_id: int | None = None):
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_report",
                        name="create_automation",
                        arguments={"instruction": WORDS, "weekday": "friday"},
                        reason=REASON,
                    ),
                ),
            ),
            Completion(text="I'll report open quotes every Friday at 7:30.", tool_calls=()),
        ]
    )
    turn = run_turn(session, fake, audience=audience, customer_id=customer_id, body=WORDS)
    return fake, turn


def _owner_bodies(session) -> list[str]:
    return list(
        session.scalars(
            select(Message.body)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(Conversation.kind == ConversationKind.owner, Conversation.customer_id.is_(None))
            .order_by(Message.id)
        )
    )


def test_owner_request_stores_one_weekly_quote_report(session):
    seed(session)
    fake, turn = _ask(session, audience="owner")

    reports = session.scalars(select(Automation).where(Automation.kind == AutomationKind.quote_report)).all()
    assert len(reports) == 1
    report = reports[0]
    assert report.name == "Quotes still waiting"
    assert report.description == WORDS
    assert report.schedule == {"weekday": "friday", "hour": 7, "minute": 30}
    assert report.created_by == AutomationAuthor.owner
    assert report.enabled is True
    assert_wall(report.next_run_at, FRIDAY_REPORT)
    assert session.scalar(select(func.count()).select_from(Automation)) == 4
    assert "create_automation" in [tool["name"] for tool in fake.calls[0]["tools"]]
    assert turn.reply == "I'll report open quotes every Friday at 7:30."


def test_friday_report_names_sam_and_skips_booked_luis(session):
    seed(session)
    _ask(session, audience="owner")
    advance_clock(session, to="next_morning")
    assert REPORT not in _owner_bodies(session)

    assert advance_clock(session, hours=1) == pt(2026, 11, 13, 8, 0)

    reports = [body for body in _owner_bodies(session) if body.startswith("Maria Chen, quotes still waiting")]
    assert reports == [REPORT]
    assert "Sam Rivera" in reports[0]
    assert "Luis Alvarez" not in reports[0]
    message = session.scalar(select(Message).where(Message.body == REPORT))
    assert message is not None
    assert_wall(message.created_at, FRIDAY_REPORT)

    run_due(session, start=FRIDAY_MORNING, end=pt(2026, 11, 13, 8, 0))
    session.commit()
    assert [body for body in _owner_bodies(session) if body == REPORT] == [REPORT]


def test_a_customer_reply_drops_sam_from_the_friday_report(session, session_factory):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    fake = FakeLLM([Completion(text="I'll leave that quote open.", tool_calls=())])
    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post(
            "/api/messages",
            json={"audience": "customer", "customer_id": sam.id, "body": "I'll pass on the drain quote."},
        )
    assert response.status_code == 200

    with session_factory() as other:
        _ask(other, audience="owner")
        advance_clock(other, to="next_morning")
        assert advance_clock(other, hours=1) == pt(2026, 11, 13, 8, 0)
        reports = [body for body in _owner_bodies(other) if body.startswith("Maria Chen, quotes still waiting")]

    assert reports == ["Maria Chen, quotes still waiting on Friday, November 13: nobody is waiting."]


def test_customer_cannot_create_an_automation(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    before = session.scalar(select(func.count()).select_from(Automation))
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    ToolCall(
                        id="call_report",
                        name="create_automation",
                        arguments={"instruction": WORDS, "weekday": "friday"},
                        reason=REASON,
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="create_automation is not allowed"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body=WORDS)

    assert session.scalar(select(func.count()).select_from(Automation)) == before
    assert "create_automation" not in [tool["name"] for tool in fake.calls[0]["tools"]]


TODAY = "Every day at 5:00 AM, customers hear about today's visit."
FRIDAY_FIVE = pt(2026, 11, 13, 5, 0)


def _book_priya_friday(session) -> Customer:
    priya = _customer(session, "Priya Shah")
    andre = session.scalar(select(Technician).where(Technician.name == "Andre Williams"))
    assert andre is not None
    book_appointment(
        session,
        customer_id=priya.id,
        service="leak",
        start=pt(2026, 11, 13, 9, 0),
        summary="Leak repair appointment with Andre Williams",
        reason="Priya asked for Friday at 9",
        technician_id=andre.id,
    )
    return priya


def _reminders_at_five(session, customer_id: int) -> list[Message]:
    rows = session.scalars(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.customer_id == customer_id,
            Message.role == MessageRole.assistant,
        )
        .order_by(Message.id)
    ).all()
    return [row for row in rows if row.created_at == FRIDAY_FIVE]


def test_saying_a_same_day_reminder_messages_priya_at_that_time(session):
    seed(session)
    priya = _book_priya_friday(session)
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])

    turn = run_turn(session, fake, audience="owner", body=TODAY)

    assert fake.calls == []
    assert turn.reply == "Every day at 5:00 AM, customers with a visit that day will hear about it."
    job = session.scalar(select(Automation).where(Automation.name == "Same-day reminder"))
    assert job is not None
    assert job.schedule == {"hour": 5, "minute": 0, "day_offset": 0}
    advance_clock(session, until=parse_shop_time("2026-11-13T07:00"))
    sent = _reminders_at_five(session, priya.id)
    assert len(sent) == 1
    assert "Andre Williams is scheduled Friday, November 13 at 9:00 AM" in sent[0].body


def test_a_saved_reminder_policy_is_sent_after_the_clock_has_passed_it(session):
    seed(session)
    priya = _book_priya_friday(session)
    remember(
        session,
        content=TODAY,
        kind=MemoryKind.policy,
        reason="The owner asked for a morning reminder",
    )
    advance_clock(session, until=parse_shop_time("2026-11-13T07:00"))
    assert _reminders_at_five(session, priya.id) == []

    shop = build_bootstrap(session)

    assert TODAY not in [item["content"] for item in shop["shop_memories"]]
    assert any(item["name"] == "Same-day reminder" for item in shop["automations"])
    sent = _reminders_at_five(session, priya.id)
    assert len(sent) == 1
    assert "Priya Shah, reminder from Northside Plumbing" in sent[0].body
    build_bootstrap(session)
    assert len(_reminders_at_five(session, priya.id)) == 1


REMOVE = 'remove "Every day at 5:00 PM, customers hear about tomorrow\'s visit."'
KEPT = "Every day at 5:00 PM, customers hear about tomorrow's visit."


def _day_before(session) -> Automation:
    job = session.scalar(select(Automation).where(Automation.name == "Day-before reminder"))
    assert job is not None
    return job


def test_the_owner_can_remove_the_day_before_reminder(session):
    seed(session)
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])

    turn = run_turn(session, fake, audience="owner", body=REMOVE)

    assert fake.calls == []
    assert turn.reply == "Every day at 5:00 PM, customers will no longer hear about tomorrow's visit."
    assert session.scalar(select(Automation).where(Automation.name == "Day-before reminder")) is None
    assert session.scalar(select(Automation).where(Automation.name == "Morning rundown")) is not None
    advance_clock(session, until=parse_shop_time("2026-11-13T07:00"))
    luis = _customer(session, "Luis Alvarez")
    sent = session.scalars(
        select(Message.body)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.customer_id == luis.id)
    ).all()
    assert sent == []
    again = run_turn(session, fake, audience="owner", body=REMOVE)
    assert again.reply == "That reminder has already been removed."
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "delete_automation")) == 1


def test_a_customer_cannot_remove_the_day_before_reminder(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    fake = FakeLLM([Completion(text="I can take a booking, a quote, or a note for Maria.", tool_calls=())])

    run_turn(session, fake, audience="customer", customer_id=james.id, body=REMOVE)

    assert _day_before(session).enabled is True


def test_removing_tomorrows_reminder_leaves_a_same_day_reminder(session):
    seed(session)
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])
    run_turn(session, fake, audience="owner", body=TODAY)

    run_turn(session, fake, audience="owner", body=REMOVE)

    same_day = session.scalar(select(Automation).where(Automation.name == "Same-day reminder"))
    assert same_day is not None and same_day.enabled is True
    assert session.scalar(select(Automation).where(Automation.name == "Day-before reminder")) is None


def test_an_earlier_reminder_removal_is_applied(session):
    seed(session)
    business_id = session.scalar(select(Automation.business_id).limit(1))
    conversation = Conversation(
        business_id=business_id,
        customer_id=None,
        kind=ConversationKind.owner,
        created_at=THURSDAY,
    )
    session.add(conversation)
    session.flush()
    session.add(Message(conversation_id=conversation.id, role=MessageRole.owner, body=REMOVE, created_at=THURSDAY))
    session.commit()

    shop = build_bootstrap(session)

    assert all(item["name"] != "Day-before reminder" for item in shop["automations"])
    bodies = [message["body"] for message in shop["conversations"][0]["messages"]]
    assert bodies[-1] == "Every day at 5:00 PM, customers will no longer hear about tomorrow's visit."
    again = [message["body"] for message in build_bootstrap(session)["conversations"][0]["messages"]]
    assert again.count(bodies[-1]) == 1


def test_asking_for_the_reminder_again_keeps_it_on(session):
    seed(session)
    business_id = session.scalar(select(Automation.business_id).limit(1))
    conversation = Conversation(
        business_id=business_id,
        customer_id=None,
        kind=ConversationKind.owner,
        created_at=THURSDAY,
    )
    session.add(conversation)
    session.flush()
    session.add_all(
        [
            Message(conversation_id=conversation.id, role=MessageRole.owner, body=REMOVE, created_at=THURSDAY),
            Message(conversation_id=conversation.id, role=MessageRole.owner, body=KEPT, created_at=THURSDAY),
        ]
    )
    session.commit()

    shop = build_bootstrap(session)

    reminder = next(item for item in shop["automations"] if item["name"] == "Day-before reminder")
    assert reminder["enabled"] is True


def test_a_reminder_that_already_ran_can_be_removed(session):
    seed(session)
    advance_clock(session, until=parse_shop_time("2026-11-13T07:00"))
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])

    run_turn(session, fake, audience="owner", body=REMOVE)

    assert session.scalar(select(Automation).where(Automation.name == "Day-before reminder")) is None


def test_a_turned_off_reminder_is_deleted_on_the_next_load(session):
    seed(session)
    old = _day_before(session)
    old.enabled = False
    session.commit()

    shop = build_bootstrap(session)

    assert all(item["id"] != old.id for item in shop["automations"])
    assert all(item["name"] != "Day-before reminder" for item in shop["automations"])


def test_the_owner_can_add_the_reminder_again(session):
    seed(session)
    fake = FakeLLM([Completion(text="This must not be used.", tool_calls=())])
    run_turn(session, fake, audience="owner", body=REMOVE)

    turn = run_turn(session, fake, audience="owner", body=KEPT)

    assert fake.calls == []
    assert turn.reply == "Every day at 5:00 PM, customers will hear about tomorrow's visit."
    job = _day_before(session)
    assert job.schedule == {"hour": 17, "minute": 0, "day_offset": 1}
    assert session.scalar(select(func.count()).select_from(Automation).where(Automation.name == "Day-before reminder")) == 1
