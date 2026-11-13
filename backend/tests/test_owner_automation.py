"""The owner can ask for a weekly quote report. A customer cannot."""

import pytest
from sqlalchemy import func, select

from app.agent.loop import run_turn
from app.agent.providers import FakeLLM
from app.agent.types import Completion, ToolCall
from app.clock import advance_clock
from app.errors import PolicyError
from app.models import Automation, AutomationAuthor, AutomationKind, Conversation, ConversationKind, Customer, Message
from app.seed import seed
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
