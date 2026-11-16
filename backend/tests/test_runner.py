"""Built-in jobs run when the clock crosses their time, and a rerun does not repeat them."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent.providers import FakeLLM
from app.agent.types import Completion
from app.clock import advance_clock, parse_shop_time
from app.main import create_app
from app.models import Appointment, AppointmentStatus, Conversation, ConversationKind, Customer, Message
from app.seed import seed
from app.services.runner import run_due
from tests.times import assert_wall, pt

THURSDAY = pt(2026, 11, 12, 16, 0)
THURSDAY_EVENING = pt(2026, 11, 12, 17, 0)
FRIDAY_MORNING = pt(2026, 11, 13, 7, 0)

REMINDER = (
    "Luis Alvarez, reminder from Northside Plumbing: "
    "Brooke Hale is scheduled Friday, November 13 at 9:00 AM for Main bathroom drain."
)
RUNDOWN = (
    "Maria Chen, morning rundown for Friday, November 13: "
    "Luis Alvarez at 9:00 AM with Brooke Hale (Main bathroom drain)."
)
CHASE = (
    "Sam Rivera, your Drain cleaning quote for $149 is still waiting. "
    "Kitchen sink draining slowly."
)


def _customer(session, name: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.name == name))
    assert customer is not None
    return customer


def _bodies(session, *, customer_id: int | None, kind: ConversationKind) -> list[Message]:
    filters = [Conversation.kind == kind]
    if customer_id is None:
        filters.append(Conversation.customer_id.is_(None))
    else:
        filters.append(Conversation.customer_id == customer_id)
    return list(
        session.scalars(
            select(Message)
            .join(Conversation, Message.conversation_id == Conversation.id)
            .where(*filters)
            .order_by(Message.id)
        )
    )


def test_friday_morning_sends_one_chase_one_reminder_and_one_rundown(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    luis = _customer(session, "Luis Alvarez")

    assert advance_clock(session, to="next_morning") == FRIDAY_MORNING

    chase = _bodies(session, customer_id=sam.id, kind=ConversationKind.customer)
    reminder = _bodies(session, customer_id=luis.id, kind=ConversationKind.customer)
    rundown = _bodies(session, customer_id=None, kind=ConversationKind.owner)
    assert [message.body for message in chase] == [CHASE]
    assert [message.body for message in reminder] == [REMINDER]
    assert [message.body for message in rundown] == [RUNDOWN]
    assert_wall(reminder[0].created_at, THURSDAY_EVENING)
    assert_wall(rundown[0].created_at, FRIDAY_MORNING)
    assert_wall(chase[0].created_at, FRIDAY_MORNING)
    assert session.scalar(select(func.count()).select_from(Message)) == 3


def test_setting_friday_morning_runs_the_same_jobs(session):
    seed(session)
    assert advance_clock(session, until=parse_shop_time("2026-11-13T07:00")) == FRIDAY_MORNING
    rundown = _bodies(session, customer_id=None, kind=ConversationKind.owner)
    assert [message.body for message in rundown] == [RUNDOWN]


def test_advancing_again_does_not_send_a_second_copy(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    luis = _customer(session, "Luis Alvarez")
    advance_clock(session, to="next_morning")
    run_due(session, start=THURSDAY, end=FRIDAY_MORNING)
    session.commit()

    advance_clock(session, to="next_morning")

    assert [message.body for message in _bodies(session, customer_id=sam.id, kind=ConversationKind.customer)] == [CHASE]
    assert [message.body for message in _bodies(session, customer_id=luis.id, kind=ConversationKind.customer)] == [REMINDER]
    rundown = [message.body for message in _bodies(session, customer_id=None, kind=ConversationKind.owner)]
    assert rundown.count(RUNDOWN) == 1
    assert "Saturday, November 14" in rundown[-1]


def test_a_quote_is_chased_at_most_twice(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    counts = []
    for _step in range(5):
        advance_clock(session, to="next_morning")
        counts.append(len(_bodies(session, customer_id=sam.id, kind=ConversationKind.customer)))

    assert counts == [1, 1, 2, 2, 2]


def test_cancelled_visit_gets_no_reminder(session):
    seed(session)
    luis = _customer(session, "Luis Alvarez")
    visit = session.scalar(select(Appointment).where(Appointment.customer_id == luis.id))
    assert visit is not None
    visit.status = AppointmentStatus.cancelled
    session.commit()

    advance_clock(session, to="next_morning")

    assert _bodies(session, customer_id=luis.id, kind=ConversationKind.customer) == []
    rundown = _bodies(session, customer_id=None, kind=ConversationKind.owner)
    assert len(rundown) == 1
    assert rundown[0].body == "Maria Chen, morning rundown for Friday, November 13: no visits booked."
    assert "Luis Alvarez" not in rundown[0].body
    sam = _customer(session, "Sam Rivera")
    assert len(_bodies(session, customer_id=sam.id, kind=ConversationKind.customer)) == 1


def test_a_customer_message_stops_the_quote_chase(session, session_factory):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    fake = FakeLLM([Completion(text="I'll leave that quote open.", tool_calls=())])
    with TestClient(create_app(session_factory, llm=fake)) as client:
        response = client.post(
            "/api/messages",
            json={
                "audience": "customer",
                "customer_id": sam.id,
                "body": "I'm still thinking about the drain quote.",
            },
        )
    assert response.status_code == 200

    with session_factory() as other:
        quote = other.scalar(select(Appointment).where(Appointment.customer_id == sam.id))
        assert quote is not None
        assert quote.last_customer_message_at > quote.quote_sent_at
        advance_clock(other, to="next_morning")
        sam_bodies = [message.body for message in _bodies(other, customer_id=sam.id, kind=ConversationKind.customer)]
        luis = _customer(other, "Luis Alvarez")
        luis_bodies = [message.body for message in _bodies(other, customer_id=luis.id, kind=ConversationKind.customer)]

    assert "I'm still thinking about the drain quote." in sam_bodies
    assert CHASE not in sam_bodies
    assert luis_bodies == [REMINDER]


def test_a_reply_stops_the_quote_chase(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    quote = session.scalar(select(Appointment).where(Appointment.customer_id == sam.id))
    assert quote is not None
    quote.last_customer_message_at = pt(2026, 11, 10, 12, 0)
    session.commit()

    advance_clock(session, to="next_morning")

    assert _bodies(session, customer_id=sam.id, kind=ConversationKind.customer) == []
    assert len(_bodies(session, customer_id=_customer(session, "Luis Alvarez").id, kind=ConversationKind.customer)) == 1


def test_thursday_evening_sends_only_the_reminder(session):
    seed(session)
    luis = _customer(session, "Luis Alvarez")
    assert advance_clock(session, hours=4) == pt(2026, 11, 12, 20, 0)

    assert [message.body for message in _bodies(session, customer_id=luis.id, kind=ConversationKind.customer)] == [REMINDER]
    assert _bodies(session, customer_id=None, kind=ConversationKind.owner) == []
    assert session.scalar(select(func.count()).select_from(Message)) == 1
