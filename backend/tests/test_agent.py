"""One conversation turn, with a scripted model and no network."""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.errors import LLMError
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
    Customer,
    Escalation,
    EscalationStatus,
    Memory,
    Message,
    Technician,
)
from app.seed import seed
from app.settings_store import get_setting, set_setting
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
        body="Please book a leak visit Friday at 1.",
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
        ("customer", "Please book a leak visit Friday at 1."),
        ("assistant", "You're booked Friday at 1:00."),
    ]
    assert_wall(messages[0].created_at, CLOCK)

    context = fake.calls[0]["messages"][1]["content"]
    assert "Gate code 4481" in context
    assert "Dog in the yard" not in context
    assert [tool["name"] for tool in fake.calls[0]["tools"]] == [
        "remember_customer_fact",
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
                "body": "Can you come Sunday at 10?",
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
    assert [tool["name"] for tool in fake.calls[0]["tools"]] == ["update_setting", "remember_policy"]
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
    second = run_turn(session, fake, audience="customer", customer_id=james.id, body="Is Friday open?")

    assert first.conversation_id == second.conversation_id
    assert _count(session, Conversation) == 1
    bodies = session.scalars(select(Message.body).order_by(Message.id)).all()
    assert bodies == ["Hello", "Hello James.", "Is Friday open?", "Friday works."]


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


def test_customer_message_requires_a_customer(session_factory, session):
    seed(session)
    fake = FakeLLM([])
    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post("/api/messages", json={"audience": "customer", "body": "Hello"})
    assert response.status_code == 422
    assert fake.calls == []
