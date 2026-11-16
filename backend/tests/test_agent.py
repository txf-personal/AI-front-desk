"""One conversation turn, with a scripted model and no network."""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.errors import LLMError
from app.clock import set_clock
from app.agent.loop import MAX_STEPS, run_turn
from app.agent.providers import FakeLLM
from app.agent.types import Completion, ToolCall
from app.errors import PolicyError
from app.main import create_app
from app.models import (
    Appointment,
    AppointmentStatus,
    AuditEvent,
    Conversation,
    ConversationKind,
    Customer,
    Escalation,
    EscalationStatus,
    Memory,
    MemoryKind,
    Message,
    MessageRole,
    Technician,
)
from app.seed import seed
from app.services.booking import book_appointment, cancel_visits_on_closed_day, create_quote
from app.services.memory import remember
from app.services.customers import create_customer
from app.settings_store import get_setting, set_setting, set_weekday
from tests.times import assert_wall, pt

CLOCK = pt(2026, 11, 12, 16, 0)
BOOK_REASON = "James asked to book the leak visit"


def _named(session, model, name):
    row = session.scalar(select(model).where(model.name == name))
    assert row is not None
    return row


def _count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def _call(name: str, arguments: dict, reason: str, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments, reason=reason)


def _reply(text: str) -> Completion:
    return Completion(text=text, tool_calls=())


def test_scripted_booking_keeps_james_address_gate_code_and_the_reason(session, session_factory):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-13T13:00:00-08:00",
                            "summary": "Water heater is leaking",
                            "technician_id": andre.id,
                        },
                        BOOK_REASON,
                    ),
                ),
            ),
            _reply("You're booked Friday at 1:00."),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Please book a leak visit Friday at 1 with Andre Williams.",
    )

    booked = session.scalar(select(Appointment).where(Appointment.customer_id == james.id))
    assert booked is not None
    assert booked.address == "1842 N 92nd St, Seattle, WA 98115"
    assert booked.access_notes == "Gate code 4481"
    assert booked.service == "leak"
    assert booked.status == AppointmentStatus.scheduled
    assert_wall(booked.start, pt(2026, 11, 13, 13, 0))
    assert turn.reply == "You're booked Friday at 1:00."

    audits = session.scalars(select(AuditEvent).where(AuditEvent.action == "book_appointment")).all()
    assert len(audits) == 1
    assert audits[0].action == "book_appointment"
    assert audits[0].reasoning == BOOK_REASON
    assert audits[0].related_id == booked.id

    messages = session.scalars(select(Message).order_by(Message.id)).all()
    assert [(message.role.value, message.body) for message in messages] == [
        ("customer", "Please book a leak visit Friday at 1 with Andre Williams."),
        ("assistant", "You're booked Friday at 1:00."),
    ]
    assert_wall(messages[0].created_at, CLOCK)

    context = fake.calls[0]["messages"][1]["content"]
    assert "Gate code 4481" in context
    assert "Dog in the yard" not in context
    assert [tool["name"] for tool in fake.calls[0]["tools"]] == [
        "remember_customer_fact",
        "set_customer_address",
        "check_availability",
        "book_appointment",
        "create_quote",
        "escalate",
    ]
    result = json.loads(next(message["content"] for message in fake.calls[1]["messages"] if message["role"] == "tool"))
    assert result["address"] == booked.address
    assert result["access_notes"] == "Gate code 4481"

    with session_factory() as other:
        stored = other.get(Appointment, booked.id)
        assert stored is not None
        assert stored.access_notes == "Gate code 4481"


def test_customer_cannot_update_a_setting(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "update_setting",
                        {"key": "sunday_calls", "value": False},
                        "Customer asked to stop Sunday calls",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="update_setting is not allowed"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body="Stop Sunday calls.")

    assert get_setting(session, "sunday_calls") is True
    assert _count(session, AuditEvent) == 0
    assert [tool["name"] for tool in fake.calls[0]["tools"]] == [
        "remember_customer_fact",
        "set_customer_address",
        "check_availability",
        "book_appointment",
        "create_quote",
        "escalate",
    ]


def test_gas_smell_escalates_and_does_not_book(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "escalate",
                        {"detail": "Customer smells gas near the water heater and is still in the house."},
                        "Gas smell",
                    ),
                ),
            ),
            _reply("Please leave the house. I'm getting the owner."),
        ]
    )

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="I smell gas.")

    assert _count(session, Appointment) == before
    opened = session.scalar(select(Escalation))
    assert opened is not None
    assert opened.status == EscalationStatus.open
    assert opened.customer_id == james.id
    assert opened.conversation_id == turn.conversation_id
    assert opened.reason == "Gas smell"
    assert opened.detail == "Customer smells gas near the water heater and is still in the house."
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "open_escalation"))
    assert audit is not None
    assert audit.reasoning == "Gas smell"
    assert turn.reply == "Please leave the house. I'm getting the owner."


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        ("I smell gas and I am still home.", "Water heater is leaking"),
        ("Please come look at the water heater.", "Gas odor at the water heater"),
    ],
)
def test_the_shop_refuses_a_gas_booking(session, body, summary):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-13T13:00:00-08:00",
                            "summary": summary,
                            "technician_id": andre.id,
                        },
                        "Customer asked for a visit",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="gas is handed to the owner"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body=body)

    assert _count(session, Appointment) == before
    assert _count(session, Message) == 0
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "book_appointment")) == 0


def test_sunday_book_returns_the_shop_error_and_writes_nothing(session_factory, session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    brooke = _named(session, Technician, "Brooke Hale")
    set_setting(session, "sunday_calls", False, reason="Owner turned Sunday calls off")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "drain",
                            "start": "2026-11-15T10:00:00-08:00",
                            "summary": "Sunday drain",
                            "technician_id": brooke.id,
                        },
                        "Customer asked for Sunday",
                    ),
                ),
            )
        ]
    )

    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post(
            "/api/messages",
            json={
                "audience": "customer",
                "customer_id": james.id,
                "body": "Can you come Sunday at 10 with Brooke Hale?",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "sunday calls are off"
    with session_factory() as other:
        assert other.scalar(select(func.count()).select_from(Appointment)) == before
        assert other.scalar(select(func.count()).select_from(Message)) == 0
        assert other.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "book_appointment")) == 0


def test_owner_can_stop_sunday_calls_and_store_the_words(session):
    seed(session)
    words = "We stopped doing Sunday calls"
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "update_setting",
                        {"key": "sunday_calls", "value": False},
                        "Owner said Sunday calls are over",
                        "call_setting",
                    ),
                    _call(
                        "remember_policy",
                        {"content": words},
                        "Owner said Sunday calls are over",
                        "call_memory",
                    ),
                ),
            ),
            _reply("Sunday calls are off. I'll remember that."),
        ]
    )

    turn = run_turn(session, fake, audience="owner", body=words)

    assert get_setting(session, "sunday_calls") is False
    memories = session.scalars(select(Memory).where(Memory.customer_id.is_(None))).all()
    assert [(memory.kind.value, memory.content) for memory in memories] == [("policy", words)]
    setting_audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "set_setting"))
    memory_audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "remember"))
    assert setting_audit is not None
    assert setting_audit.reasoning == "Owner said Sunday calls are over"
    assert memory_audit is not None
    assert memory_audit.reasoning == "Owner said Sunday calls are over"
    assert [tool["name"] for tool in fake.calls[0]["tools"]] == [
        "update_setting",
        "remember_policy",
        "create_automation",
    ]
    assert "4481" not in fake.calls[0]["messages"][1]["content"]
    assert turn.reply == "Sunday calls are off. I'll remember that."
    james = _named(session, Customer, "James Okonkwo")
    james_memories = session.scalars(select(Memory.content).where(Memory.customer_id == james.id)).all()
    assert words not in james_memories


def test_quote_uses_the_book_price_and_does_not_hold_a_slot(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "create_quote",
                        {"service": "faucet", "summary": "Kitchen faucet", "price": 119},
                        "James asked for the faucet price",
                    ),
                ),
            ),
            _reply("The faucet visit is $119."),
        ]
    )

    run_turn(session, fake, audience="customer", customer_id=james.id, body="What does a faucet repair cost?")

    quote = session.scalar(select(Appointment).where(Appointment.customer_id == james.id))
    assert quote is not None
    assert quote.status == AppointmentStatus.quoted
    assert quote.quote_amount == 119
    assert quote.start is None
    assert quote.technician_id is None
    assert quote.address == james.address
    assert quote.last_customer_message_at == quote.quote_sent_at


def test_a_reply_at_the_same_clock_answers_the_quote(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    quote = create_quote(
        session,
        customer_id=james.id,
        service="faucet",
        summary="Kitchen faucet",
        reason="James asked for the faucet price",
        price=119,
    )
    assert quote.last_customer_message_at == quote.quote_sent_at
    fake = FakeLLM([_reply("I'll leave that quote open.")])

    run_turn(session, fake, audience="customer", customer_id=james.id, body="I'll pass on the faucet quote.")

    session.refresh(quote)
    assert quote.last_customer_message_at == quote.quote_sent_at + timedelta(seconds=1)


def test_off_book_quote_writes_nothing(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "create_quote",
                        {"service": "faucet", "summary": "Discount faucet", "price": 50},
                        "James asked for a discount",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="price is not on the book"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body="Can you do the faucet for $50?")

    assert _count(session, Appointment) == before
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "create_quote")) == 0


def test_owner_cannot_book(session):
    seed(session)
    andre = _named(session, Technician, "Andre Williams")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-13T13:00:00-08:00",
                            "summary": "Owner tried to book",
                            "technician_id": andre.id,
                        },
                        "Owner asked to book James",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="book_appointment is not allowed"):
        run_turn(session, fake, audience="owner", body="Book James on Friday.")

    assert _count(session, Appointment) == before


def test_owner_can_close_saturday_without_erasing_the_week(session):
    seed(session)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "update_setting",
                        {"key": "hours.saturday", "value": None},
                        "The owner closed Saturday",
                    ),
                ),
            ),
            _reply("Saturday is closed."),
        ]
    )

    turn = run_turn(session, fake, audience="owner", body="sat not open anymore")

    hours = get_setting(session, "hours")
    assert hours["saturday"] is None
    assert hours["friday"] == ["08:00", "17:00"]
    assert get_setting(session, "sunday_calls") is True
    assert turn.reply == "Saturday is closed."


def test_a_closed_sunday_reply_cannot_say_it_is_open(session):
    seed(session)
    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Sunday is open, we can book that.")])

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Are you open on Sunday?")

    assert turn.reply == "Sunday is closed."


def test_an_hours_question_is_answered_when_the_model_skips_that_day(session):
    seed(session)
    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Monday is open from 8:00 AM to 5:00 PM.")])

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Are you open on Sunday?")

    assert turn.reply == "Monday is open from 8:00 AM to 5:00 PM. Sunday is closed."


def test_an_open_sunday_reply_cannot_say_it_is_closed(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Sunday is closed.")])

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Are you open on Sunday?")

    assert turn.reply == "Sunday is open from 8:00 AM to 5:00 PM."


def test_a_sunday_visit_time_is_not_rewritten_as_shop_hours(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    reply = "I can book the leak repair on Sunday, 9:30 AM - 10:30 AM."
    fake = FakeLLM([_reply(reply)])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="9:30 AM - 10:30 AM Leak Repairs",
    )

    assert turn.reply == reply


def test_a_wrong_open_window_is_still_corrected(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Sunday is open from 9:30 AM to 10:30 AM.")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="are you open on Sunday?",
    )

    assert turn.reply == "Sunday is open from 8:00 AM to 5:00 PM."


def test_a_plural_closed_claim_follows_the_current_hours(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Our shop is closed on Sundays, so we cannot book November 15.")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Please book a leak visit on Sunday, November 15, 2026 at 10:00 AM.",
    )

    assert turn.reply.startswith("Sunday, November 15, Leak repair visit:")
    assert "10:00 AM with Andre Williams or Chris Patel" in turn.reply
    assert "closed" not in turn.reply.lower()


def test_a_correct_plural_closed_claim_is_kept(session):
    seed(session)
    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Our shop is closed on Sundays.")])

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Are you open on Sunday?")

    assert turn.reply == "Our shop is closed on Sundays."


def test_checking_a_closed_day_returns_no_slots(session):
    from app.agent.tools import _check_availability

    seed(session)
    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    james = _named(session, Customer, "James Okonkwo")

    closed = _check_availability(
        session,
        {"service": "leak", "day": "2026-11-15"},
        reason="Asked if Sunday is open",
        customer_id=james.id,
        conversation_id=None,
    )
    friday = _check_availability(
        session,
        {"service": "leak", "day": "2026-11-13"},
        reason="Asked about Friday",
        customer_id=james.id,
        conversation_id=None,
    )

    assert closed == {"closed": True, "weekday": "sunday", "slots": []}
    assert friday["closed"] is False
    assert friday["slots"]


def _slot_hours(result: dict) -> list[int]:
    return [int(slot["start"][11:13]) for slot in result["slots"]]


def _ask_for_slots(session, customer, body: str, service: str, day: str) -> dict:
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "check_availability",
                        {"service": service, "day": day},
                        "Customer asked which times are open",
                    ),
                ),
            ),
            _reply("Here are the open slots."),
        ]
    )
    turn = run_turn(session, fake, audience="customer", customer_id=customer.id, body=body)
    assert turn.reply == "Here are the open slots."
    return json.loads(fake.calls[1]["messages"][-1]["content"])


def test_a_morning_preference_limits_the_offered_slots(session):
    seed(session)
    priya = _named(session, Customer, "Priya Shah")

    result = _ask_for_slots(session, priya, "what's the option", "drain", "2026-11-14")

    hours = _slot_hours(result)
    assert hours
    assert all(hour < 12 for hour in hours)
    assert result["preference"] == "morning"
    assert "before noon" in result["note"]


def test_asking_for_the_afternoon_overrides_a_morning_preference(session):
    seed(session)
    priya = _named(session, Customer, "Priya Shah")

    result = _ask_for_slots(session, priya, "what about the afternoon", "drain", "2026-11-14")

    hours = _slot_hours(result)
    assert hours
    assert all(12 <= hour < 17 for hour in hours)
    assert result["preference"] == "afternoon"


def test_a_customer_without_a_time_preference_sees_the_whole_day(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")

    result = _ask_for_slots(session, james, "what's the option", "leak", "2026-11-13")

    hours = _slot_hours(result)
    assert any(hour < 12 for hour in hours)
    assert any(hour >= 12 for hour in hours)
    assert "preference" not in result


def test_an_evening_preference_with_no_evening_slots_keeps_the_day(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    remember(
        session,
        customer_id=james.id,
        content="Prefers evening appointments",
        kind=MemoryKind.preference,
        reason="James asked for evenings",
    )

    result = _ask_for_slots(session, james, "what's the option", "leak", "2026-11-13")

    hours = _slot_hours(result)
    assert any(hour < 12 for hour in hours)
    assert any(hour >= 12 for hour in hours)
    assert "preference" not in result


def test_closing_sunday_cancels_a_future_visit_and_leaves_a_past_one(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    future = book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(2026, 11, 15, 10, 0),
        summary="Sunday leak",
        reason="Customer asked for Sunday",
        technician_id=andre.id,
    )
    past = book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(2026, 11, 15, 13, 0),
        summary="Earlier Sunday leak",
        reason="Customer asked for an earlier Sunday",
        technician_id=andre.id,
    )
    past.start = pt(2026, 11, 8, 10, 0)
    past.end = pt(2026, 11, 8, 11, 30)
    session.commit()
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call("update_setting", {"key": "hours.sunday", "value": None}, "The owner closed Sunday"),
                ),
            ),
            _reply("Sunday is closed."),
        ]
    )

    run_turn(session, fake, audience="owner", body="Close Sunday")

    session.refresh(future)
    session.refresh(past)
    assert future.status == AppointmentStatus.cancelled
    assert past.status == AppointmentStatus.scheduled
    bodies = session.scalars(select(Message.body)).all()
    assert any("is cancelled because Sunday is closed" in body for body in bodies)
    assert any(body.startswith("Sunday is closed. Cancelled James Okonkwo's leak visit") for body in bodies)
    notices = [body for body in bodies if "cancelled because Sunday is closed" in body]

    again = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call("update_setting", {"key": "hours.sunday", "value": None}, "Sunday stays closed"),
                ),
            ),
            _reply("Sunday is still closed."),
        ]
    )
    run_turn(session, again, audience="owner", body="Sunday stays closed")
    bodies = session.scalars(select(Message.body)).all()
    assert [body for body in bodies if "cancelled because Sunday is closed" in body] == notices

    reopen = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(_call("update_setting", {"key": "hours.sunday", "value": True}, "Sunday is open again"),),
            ),
            _reply("Sunday is open."),
        ]
    )
    run_turn(session, reopen, audience="owner", body="Open Sunday")
    session.refresh(future)
    assert future.status == AppointmentStatus.cancelled
    assert get_setting(session, "sunday_calls") is True
    assert (
        session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(
                Appointment.customer_id == james.id,
                Appointment.status == AppointmentStatus.scheduled,
            )
        )
        == 1
    )


def test_reopening_saturday_does_not_restore_the_cancelled_visit(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    now = pt(2026, 11, 12, 16, 0)
    conversation = Conversation(
        business_id=james.business_id,
        customer_id=james.id,
        kind=ConversationKind.customer,
        created_at=now,
    )
    session.add(conversation)
    session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.customer,
            body="Book a leak Saturday at 9:00 AM with Andre Williams.",
            created_at=now,
        )
    )
    session.commit()
    from app.present import build_bootstrap

    build_bootstrap(session)
    set_weekday(session, "saturday", None, reason="Saturday is closed")
    cancel_visits_on_closed_day(session, "saturday")
    set_weekday(session, "saturday", True, reason="Saturday is open")
    build_bootstrap(session)

    visits = session.scalars(select(Appointment).where(Appointment.customer_id == james.id)).all()
    assert len(visits) == 1
    assert visits[0].status == AppointmentStatus.cancelled
    assert_wall(visits[0].start, pt(2026, 11, 14, 9, 0))

    fake = FakeLLM([_reply("Saturday is open again.")])
    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Book a leak Saturday at 9:00 AM with Andre Williams.",
    )
    assert turn.reply == "You're booked Saturday, November 14 at 9:00 AM with Andre Williams."
    scheduled = session.scalars(
        select(Appointment).where(
            Appointment.customer_id == james.id,
            Appointment.status == AppointmentStatus.scheduled,
        )
    ).all()
    assert len(scheduled) == 1
    assert_wall(scheduled[0].start, pt(2026, 11, 14, 9, 0))


def test_yes_does_not_choose_a_time_or_a_technician(session):
    seed(session)
    priya = _named(session, Customer, "Priya Shah")
    brooke = _named(session, Technician, "Brooke Hale")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "drain",
                            "start": "2026-11-14T08:00:00-08:00",
                            "summary": "Drain cleaning",
                            "technician_id": brooke.id,
                            "price": 149,
                        },
                        "Customer said yes",
                    ),
                ),
            ),
            _reply(
                "Your drain cleaning has been scheduled for November 14th at 8:00 AM "
                "with technician Brooke Hale."
            ),
        ]
    )

    turn = run_turn(session, fake, audience="customer", customer_id=priya.id, body="yes")

    assert turn.reply == "I have not booked a visit."
    assert _count(session, Appointment) == before
    refused = json.loads(fake.calls[1]["messages"][-1]["content"])
    assert refused == {
        "error": "not_chosen",
        "detail": "The customer has not chosen a time and a technician. List the open choices. Do not book.",
    }
    stored = session.scalars(select(Message.body).order_by(Message.id)).all()
    assert stored[0] == "yes"


def test_a_time_without_the_named_technician_does_not_book(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-13T13:00:00-08:00",
                            "summary": "Water heater is leaking",
                            "technician_id": andre.id,
                        },
                        BOOK_REASON,
                    ),
                ),
            ),
            _reply("Which technician would you like?"),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Please book a leak visit Friday at 1.",
    )

    assert "Friday, November 13, Leak repair visit:" in turn.reply
    assert "1:00 PM with Andre Williams or Chris Patel" in turn.reply
    assert _count(session, Appointment) == before


def test_a_wrong_technician_id_books_the_person_named(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    dana = _named(session, Technician, "Dana Okada")
    chris = _named(session, Technician, "Chris Patel")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "water_heater_diagnostic",
                            "start": "2026-11-13T08:00:00-08:00",
                            "summary": "Water heater diagnostic",
                            "technician_id": chris.id,
                            "price": 89,
                        },
                        "Customer chose Dana at 8:00 AM",
                    ),
                ),
            ),
            _reply("You're booked Friday at 8:00 AM with Dana Okada."),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Dana Okada\n\n8:00 AM - 9:30 AM",
    )

    booked = session.scalar(
        select(Appointment).where(
            Appointment.customer_id == james.id,
            Appointment.status == AppointmentStatus.scheduled,
        )
    )
    assert booked is not None
    assert booked.technician_id == dana.id
    assert booked.service == "water_heater_diagnostic"
    assert turn.reply == "You're booked Friday at 8:00 AM with Dana Okada."


def test_tomorrow_on_thursday_closes_friday_not_the_weekend(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    booked = book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(2026, 11, 13, 8, 0),
        summary="Friday leak",
        reason="James booked Friday morning",
        technician_id=andre.id,
    )
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(_call("update_setting", {"key": "hours.saturday", "value": None}, "Wrong day"),),
            ),
            _reply("Saturday is closed."),
        ]
    )

    turn = run_turn(session, fake, audience="owner", body="i'm sick, will close tomorrow")

    assert fake.calls == []
    assert turn.reply == "Friday, November 13 is closed."
    hours = get_setting(session, "hours")
    assert hours["friday"] is None
    assert hours["saturday"] == ["08:00", "17:00"]
    assert hours["sunday"] is None
    session.refresh(booked)
    assert booked.status == AppointmentStatus.cancelled


def test_a_named_weekday_still_reaches_the_model(session):
    seed(session)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(_call("update_setting", {"key": "hours.sunday", "value": None}, "Owner closed Sunday"),),
            ),
            _reply("Sunday is closed."),
        ]
    )

    turn = run_turn(session, fake, audience="owner", body="close on Sunday")

    assert len(fake.calls) == 2
    assert get_setting(session, "hours")["sunday"] is None
    assert get_setting(session, "hours")["friday"] == ["08:00", "17:00"]
    assert turn.reply == "Sunday is closed."


def test_a_reply_cannot_claim_a_booking_that_was_not_made(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    fake = FakeLLM([_reply("Your leak visit is successfully scheduled for Sunday at 2:00 PM.")])

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Book Sunday at 2:00 PM.")

    assert turn.reply.startswith("Which visit should I book for Sunday, November 15?")
    assert "1. Water heater diagnostic, $89" in turn.reply
    assert "Leak repair visit, $129" in turn.reply
    assert _count(session, Appointment) == before


def test_a_real_booking_may_say_it_was_scheduled(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-16T13:00:00-08:00",
                            "summary": "Leak under the sink",
                            "technician_id": andre.id,
                            "price": 129,
                        },
                        BOOK_REASON,
                    ),
                ),
            ),
            _reply("The visit is successfully scheduled."),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Book Monday at 1:00 PM with Andre Williams.",
    )

    assert turn.reply == "The visit is successfully scheduled."


def test_a_partial_hours_change_does_not_wipe_the_week(session):
    seed(session)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "update_setting",
                        {"key": "hours", "value": {"tuesday": None}},
                        "The owner closed Tuesday",
                    ),
                ),
            ),
            _reply("Tuesday is closed."),
        ]
    )

    run_turn(session, fake, audience="owner", body="We're closed on Tuesdays now.")

    hours = get_setting(session, "hours")
    assert hours["tuesday"] is None
    assert hours["monday"] == ["08:00", "17:00"]
    assert hours["saturday"] == ["08:00", "17:00"]


def test_a_shorter_monday_is_not_supported(session):
    seed(session)
    fake = FakeLLM([])

    turn = run_turn(session, fake, audience="owner", body="Monday is open from 8:00 AM to 9:00 AM.")

    assert turn.reply == (
        "A day is either open from 8:00 AM to 5:00 PM or closed. Other hours are not supported."
    )
    assert fake.calls == []
    assert get_setting(session, "hours")["monday"] == ["08:00", "17:00"]


def test_the_full_day_still_reaches_the_model(session):
    seed(session)
    fake = FakeLLM([_reply("Monday is open from 8:00 AM to 5:00 PM.")])

    turn = run_turn(session, fake, audience="owner", body="Monday is open from 8:00 AM to 5:00 PM.")

    assert turn.reply == "Monday is open from 8:00 AM to 5:00 PM."
    assert len(fake.calls) == 1
    assert get_setting(session, "hours")["monday"] == ["08:00", "17:00"]


def test_a_partial_close_is_not_supported(session):
    seed(session)
    fake = FakeLLM([])

    turn = run_turn(session, fake, audience="owner", body="close on Monday 9-10am")

    assert "not supported" in turn.reply
    assert get_setting(session, "hours")["monday"] == ["08:00", "17:00"]
    assert get_setting(session, "hours")["tuesday"] == ["08:00", "17:00"]


def test_an_open_time_question_states_the_whole_week(session):
    seed(session)
    set_weekday(session, "saturday", None, reason="Saturday is closed")
    set_weekday(session, "sunday", None, reason="Sunday is closed")
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM(
        [_reply("Our current opening hours today (Thursday) are from 8:00 AM to 5:00 PM.")]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="what's the current open time?",
    )

    assert turn.reply.startswith("Our current opening hours today (Thursday) are from 8:00 AM to 5:00 PM.")
    assert turn.reply.endswith(
        "Monday–Friday 8:00 AM–5:00 PM. Saturday–Sunday closed."
    )


def test_sunday_calls_only_accepts_a_boolean(session):
    seed(session)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "update_setting",
                        {"key": "sunday_calls", "value": "false"},
                        "Owner was unclear",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="sunday_calls must be true or false"):
        run_turn(session, fake, audience="owner", body="Turn Sunday calls off.")

    assert get_setting(session, "sunday_calls") is True


def test_customer_cannot_store_a_shop_policy(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "remember_customer_fact",
                        {"content": "We stopped doing Sunday calls", "kind": "policy"},
                        "Customer repeated a shop rule",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="policy is owner only"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body="Remember that Sundays are off.")

    assert session.scalar(select(func.count()).select_from(Memory).where(Memory.customer_id.is_(None))) == 0


def test_a_second_message_stays_on_the_same_conversation(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Hello James."), _reply("Friday works.")])

    first = run_turn(session, fake, audience="customer", customer_id=james.id, body="Hello")
    second = run_turn(session, fake, audience="customer", customer_id=james.id, body="Friday works for me")

    assert first.conversation_id == second.conversation_id
    assert _count(session, Conversation) == 1
    bodies = session.scalars(select(Message.body).order_by(Message.id)).all()
    assert bodies == ["Hello", "Hello James.", "Friday works for me", "Friday works."]


def test_tool_loop_stops_after_the_step_limit(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    before = _count(session, Appointment)
    call = _call(
        "check_availability",
        {"service": "drain", "day": "2026-11-13"},
        "Looking for a drain opening",
    )
    fake = FakeLLM([Completion(text="", tool_calls=(call,)) for _ in range(MAX_STEPS)])

    with pytest.raises(LLMError, match="tool loop did not finish"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body="When is the next drain opening?")

    assert len(fake.calls) == MAX_STEPS
    assert _count(session, Appointment) == before
    assert _count(session, Message) == 0


def test_an_empty_model_reply_is_not_stored(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("   ")])

    with pytest.raises(LLMError, match="model reply was empty"):
        run_turn(session, fake, audience="customer", customer_id=james.id, body="Hello")

    assert _count(session, Message) == 0


def test_post_message_returns_the_model_reply(session_factory, session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("Hello James.")])

    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post(
            "/api/messages",
            json={"audience": "customer", "customer_id": james.id, "body": "  Hello  "},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "Hello James."
    assert body["conversation_id"] > 0
    with session_factory() as other:
        stored = other.scalars(select(Message.body).order_by(Message.id)).all()
    assert stored == ["Hello", "Hello James."]


def test_ordinary_leak_is_booked_and_gas_is_handed_over(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("I can book that.")])

    run_turn(session, fake, audience="customer", customer_id=james.id, body="The water heater is leaking.")

    instructions = fake.calls[0]["messages"][0]["content"]
    assert "ordinary leak" in instructions
    assert "null weekday" in instructions
    assert "closed_days" in instructions
    assert "disagrees with settings.hours" in instructions
    assert "check_availability before saying a day is closed" not in instructions
    assert "gas" in instructions
    assert "active leak" not in instructions
    assert "escalate and do not book" in instructions


def test_a_new_caller_can_save_an_address_and_be_booked(session):
    seed(session)
    caller = create_customer(session, name="Casey Brooks", phone="206-555-0188")
    andre = _named(session, Technician, "Andre Williams")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "set_customer_address",
                        {"address": "  9 Pike St, Seattle, WA 98115  "},
                        "Casey gave the service address",
                    ),
                ),
            ),
            _reply("I have 9 Pike St."),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=caller.id,
        body="The address is 9 Pike St, Seattle, WA 98115.",
    )

    session.refresh(caller)
    assert caller.address == "9 Pike St, Seattle, WA 98115"
    assert turn.reply == "I have 9 Pike St."
    audit = session.scalar(select(AuditEvent).where(AuditEvent.action == "set_address"))
    assert audit is not None
    assert audit.reasoning == "Casey gave the service address"
    booked = book_appointment(
        session,
        customer_id=caller.id,
        service="leak",
        start=pt(2026, 11, 13, 10, 0),
        summary="Leak under the sink",
        reason="Casey asked to book the leak visit",
        technician_id=andre.id,
        price=129,
    )
    assert booked.address == "9 Pike St, Seattle, WA 98115"
    assert booked.access_notes is None


def test_an_outside_address_is_told_politely(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    james.address = "900 Bellevue Way, Bellevue, WA 98004"
    session.commit()
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "check_availability",
                        {"service": "faucet", "day": "2026-11-14"},
                        "Checking Saturday for the faucet",
                    ),
                ),
            ),
            _reply("Saturday is open."),
        ]
    )

    turn = run_turn(session, fake, audience="customer", customer_id=james.id, body="Chris Patel")

    assert turn.reply == (
        "That address is outside the area we serve. Update the address, and I can check again."
    )
    assert len(fake.calls) == 1
    assert _count(session, Appointment) == before
    bodies = session.scalars(select(Message.body).order_by(Message.id)).all()
    assert bodies == ["Chris Patel", turn.reply]


def test_a_blank_address_is_not_saved(session):
    seed(session)
    caller = create_customer(session, name="Casey Brooks", phone="206-555-0188")
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(_call("set_customer_address", {"address": "  "}, "Casey gave no street"),),
            )
        ]
    )

    with pytest.raises(PolicyError, match="address is required"):
        run_turn(session, fake, audience="customer", customer_id=caller.id, body="I forgot the street.")

    session.refresh(caller)
    assert caller.address is None
    assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "set_address")) == 0


def test_the_owner_cannot_set_a_customer_address(session):
    seed(session)
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "set_customer_address",
                        {"address": "9 Pike St, Seattle, WA 98115"},
                        "Owner typed an address",
                    ),
                ),
            )
        ]
    )

    with pytest.raises(PolicyError, match="set_customer_address is not allowed"):
        run_turn(session, fake, audience="owner", body="Casey's address is 9 Pike St.")

    assert "set_customer_address" not in [tool["name"] for tool in fake.calls[0]["tools"]]


def test_tomorrow_is_booked_when_the_reply_says_today(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    friday = book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(2026, 11, 13, 10, 0),
        summary="Leak repair visit with Andre Williams.",
        reason="James booked Friday morning",
        technician_id=andre.id,
        price=129,
    )
    set_clock(session, pt(2026, 11, 13, 11, 0))
    fake = FakeLLM(
        [
            _reply("What time and which technician for tomorrow?"),
            _reply(
                "Your leak repair visit with Andre Williams has already been scheduled "
                "for today at 10:00 AM."
            ),
        ]
    )

    run_turn(session, fake, audience="customer", customer_id=james.id, body="book tomorrow")
    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="10:00 AM for Leak repair visit with Andre Williams.",
    )

    assert "Saturday, November 14" in turn.reply
    assert "10:00 AM" in turn.reply
    assert "Andre Williams" in turn.reply
    assert "today" not in turn.reply.lower()
    assert len(fake.calls) == 2
    saturday = session.scalar(
        select(Appointment).where(
            Appointment.customer_id == james.id,
            Appointment.id != friday.id,
        )
    )
    assert saturday is not None
    assert saturday.service == "leak"
    assert saturday.technician_id == andre.id
    assert saturday.status == AppointmentStatus.scheduled
    assert_wall(saturday.start, pt(2026, 11, 14, 10, 0))
    session.refresh(friday)
    assert friday.status == AppointmentStatus.scheduled
    assert_wall(friday.start, pt(2026, 11, 13, 10, 0))


def test_a_model_start_on_the_wrong_day_follows_tomorrow(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    fake = FakeLLM(
        [
            Completion(
                text="",
                tool_calls=(
                    _call(
                        "book_appointment",
                        {
                            "service": "leak",
                            "start": "2026-11-13T10:00:00-08:00",
                            "summary": "Leak repair visit",
                            "technician_id": andre.id,
                            "price": 129,
                        },
                        "Book the leak the customer named",
                    ),
                ),
            ),
            _reply("Your leak repair visit has already been scheduled for today at 10:00 AM."),
        ]
    )

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="10:00 AM tomorrow for a leak repair visit with Andre Williams.",
    )

    assert turn.reply == "You're booked Saturday, November 14 at 10:00 AM with Andre Williams."
    booked = session.scalar(select(Appointment).where(Appointment.customer_id == james.id, Appointment.status == AppointmentStatus.scheduled))
    assert booked is not None
    assert_wall(booked.start, pt(2026, 11, 14, 10, 0))


def test_a_past_time_is_not_booked(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    before = _count(session, Appointment)
    fake = FakeLLM([_reply("You're booked today at 10:00 AM.")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Book a leak today at 10:00 AM with Andre Williams.",
    )

    assert turn.reply == "Friday, November 13 at 10:00 AM has already passed. Name a later time."
    assert _count(session, Appointment) == before


def test_a_named_service_lists_tomorrow_slots(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    before = _count(session, Appointment)
    fake = FakeLLM(
        [
            _reply("What time and which technician for tomorrow?"),
            _reply(
                "Could you please let me know the exact time and the technician you'd like "
                "for your leak repair appointment tomorrow?"
            ),
        ]
    )

    first = run_turn(session, fake, audience="customer", customer_id=james.id, body="book tomorrow")
    second = run_turn(session, fake, audience="customer", customer_id=james.id, body="Leak repair")

    assert first.reply.startswith("Which visit should I book for Saturday, November 14?")
    assert "Leak repair visit, $129" in first.reply
    assert second.reply.startswith("Saturday, November 14, Leak repair visit:")
    assert "8:00 AM with Andre Williams or Chris Patel" in second.reply
    assert "exact time" not in second.reply.lower()
    assert _count(session, Appointment) == before


def test_a_morning_request_lists_only_morning_slots(session):
    seed(session)
    priya = _named(session, Customer, "Priya Shah")
    fake = FakeLLM([_reply("What time works?")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=priya.id,
        body="book a drain tomorrow morning",
    )

    assert turn.reply.startswith("Friday, November 13, morning, Drain cleaning:")
    assert "8:00 AM with" in turn.reply
    assert "12:00 PM" not in turn.reply
    assert "1:00 PM" not in turn.reply


def test_a_closed_day_is_named_instead_of_a_slot_list(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    set_weekday(session, "saturday", None, reason="Saturday is closed")
    fake = FakeLLM([_reply("What time on Saturday?")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="book a leak tomorrow",
    )

    assert turn.reply == "Saturday, November 14 is closed."


def test_a_later_hello_does_not_reuse_an_old_booking_day(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM(
        [
            _reply("Which time?"),
            _reply("I can take a booking, a quote, or a note for Maria."),
        ]
    )

    first = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="Please book the leak for Friday at 10 with Andre Williams.",
    )
    set_clock(session, pt(2026, 11, 14, 11, 0))
    second = run_turn(session, fake, audience="customer", customer_id=james.id, body="Just saying hello")

    assert first.reply.startswith("Friday, November 13, Leak repair visit:")
    assert second.reply == "I can take a booking, a quote, or a note for Maria."


def test_a_price_question_does_not_list_slots(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    fake = FakeLLM([_reply("A leak repair visit is $129.")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="what a leak visit costs",
    )

    assert turn.reply == "A leak repair visit is $129."


def test_an_open_question_keeps_the_time_booked_later(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    now = pt(2026, 11, 13, 11, 0)
    set_clock(session, now)
    conversation = Conversation(
        business_id=james.business_id,
        customer_id=james.id,
        kind=ConversationKind.customer,
        created_at=now,
    )
    session.add(conversation)
    session.flush()
    for body, role in (
        ("book a leak tomorrow", MessageRole.customer),
        ("Could you tell me the exact time and the technician you'd like?", MessageRole.assistant),
        ("10:00 AM for Leak repair visit with Andre Williams.", MessageRole.customer),
    ):
        session.add(Message(conversation_id=conversation.id, role=role, body=body, created_at=now))
    session.commit()
    from app.present import build_bootstrap

    build_bootstrap(session)
    build_bootstrap(session)

    question = session.scalar(
        select(Message.body).where(Message.role == MessageRole.assistant, Message.body.like("Saturday%"))
    )
    assert question is not None
    assert "10:00 AM with Andre Williams or Chris Patel" in question
    visits = session.scalars(
        select(Appointment).where(
            Appointment.customer_id == james.id,
            Appointment.status == AppointmentStatus.scheduled,
        )
    ).all()
    assert len(visits) == 1
    assert_wall(visits[0].start, pt(2026, 11, 14, 10, 0))


def test_a_written_past_date_is_not_booked(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    before = _count(session, Appointment)
    fake = FakeLLM([_reply("You're booked on November 3 at 10:00 AM.")])

    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="I want to book a leak repair visit with Andre Williams 11/03 at 10:00 AM",
    )

    assert turn.reply == "Tuesday, November 3 at 10:00 AM has already passed. Name a later time."
    assert _count(session, Appointment) == before


def test_a_written_date_beats_an_earlier_tomorrow(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    set_clock(session, pt(2026, 11, 13, 11, 0))
    fake = FakeLLM(
        [
            _reply("What time tomorrow?"),
            _reply("You're booked tomorrow at 10:00 AM."),
        ]
    )

    run_turn(session, fake, audience="customer", customer_id=james.id, body="book tomorrow")
    turn = run_turn(
        session,
        fake,
        audience="customer",
        customer_id=james.id,
        body="10:00 AM on 11/20 for a leak repair visit with Andre Williams.",
    )

    assert turn.reply == "You're booked Friday, November 20 at 10:00 AM with Andre Williams."
    booked = session.scalar(
        select(Appointment).where(
            Appointment.customer_id == james.id,
            Appointment.status == AppointmentStatus.scheduled,
        )
    )
    assert booked is not None
    assert booked.technician_id == andre.id
    assert_wall(booked.start, pt(2026, 11, 20, 10, 0))


def test_a_stored_tomorrow_request_is_booked_once(session):
    seed(session)
    james = _named(session, Customer, "James Okonkwo")
    andre = _named(session, Technician, "Andre Williams")
    book_appointment(
        session,
        customer_id=james.id,
        service="leak",
        start=pt(2026, 11, 13, 10, 0),
        summary="Leak repair visit with Andre Williams.",
        reason="James booked Friday morning",
        technician_id=andre.id,
        price=129,
    )
    now = pt(2026, 11, 13, 11, 0)
    set_clock(session, now)
    conversation = Conversation(
        business_id=james.business_id,
        customer_id=james.id,
        kind=ConversationKind.customer,
        created_at=now,
    )
    session.add(conversation)
    session.flush()
    for body, role in (
        ("book tomorrow", MessageRole.customer),
        ("10:00 AM for Leak repair visit with Andre Williams.", MessageRole.customer),
        (
            "Your leak repair visit with Andre Williams has already been scheduled for today at 10:00 AM.",
            MessageRole.assistant,
        ),
        (
            "I want to book leak repair visit with Andre Williams 11/03 at 10:00 AM",
            MessageRole.customer,
        ),
    ):
        session.add(Message(conversation_id=conversation.id, role=role, body=body, created_at=now))
    session.commit()
    from app.present import build_bootstrap

    build_bootstrap(session)
    build_bootstrap(session)

    visits = session.scalars(
        select(Appointment)
        .where(
            Appointment.customer_id == james.id,
            Appointment.status == AppointmentStatus.scheduled,
        )
        .order_by(Appointment.start)
    ).all()
    assert len(visits) == 2
    assert_wall(visits[0].start, pt(2026, 11, 13, 10, 0))
    assert_wall(visits[1].start, pt(2026, 11, 14, 10, 0))
    confirmations = session.scalars(
        select(Message.body).where(Message.body.like("You're booked Saturday%"))
    ).all()
    assert confirmations == ["You're booked Saturday, November 14 at 10:00 AM with Andre Williams."]


def test_customer_message_requires_a_customer(session_factory, session):
    seed(session)
    fake = FakeLLM([])
    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post("/api/messages", json={"audience": "customer", "body": "Hello"})
    assert response.status_code == 422
    assert fake.calls == []
