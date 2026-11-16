"""Context packs show one audience only."""

import json

import pytest
from sqlalchemy import select

from app.agent.context import MESSAGE_LIMIT, build_context_pack
from app.errors import PolicyError
from app.models import Conversation, ConversationKind, Customer, Memory, MemoryKind, Message, MessageRole
from app.seed import seed
from app.settings_store import set_setting
from app.services.memory import remember
from tests.times import pt

CLOCK = pt(2026, 11, 12, 16, 0)
NOW = "2026-11-12T16:00:00-08:00"


def _customer(session, name: str) -> Customer:
    customer = session.scalar(select(Customer).where(Customer.name == name))
    assert customer is not None
    return customer


def _say(session, customer: Customer | None, body: str, *, kind: ConversationKind) -> None:
    conversation = Conversation(
        business_id=(customer.business_id if customer else _customer(session, "James Okonkwo").business_id),
        customer_id=None if customer is None else customer.id,
        kind=kind,
        created_at=CLOCK,
    )
    session.add(conversation)
    session.flush()
    session.add(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.customer if kind == ConversationKind.customer else MessageRole.owner,
            body=body,
            created_at=CLOCK,
        )
    )
    session.commit()


def test_james_pack_has_his_address_and_gate_code_only(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    pack = build_context_pack(session, audience="customer", customer_id=james.id)
    blob = json.dumps(pack)

    assert pack["audience"] == "customer"
    assert pack["now"] == NOW
    assert pack["business"]["name"] == "Northside Plumbing"
    assert pack["customer"]["address"] == "1842 N 92nd St, Seattle, WA 98115"
    assert pack["customer"]["memories"] == [{"kind": "access", "content": "Gate code 4481"}]
    assert pack["open_jobs"] == []
    assert pack["recent_messages"] == []
    assert [person["name"] for person in pack["technicians"]] == [
        "Andre Williams",
        "Brooke Hale",
        "Chris Patel",
        "Dana Okada",
    ]
    assert pack["settings"]["sunday_calls"] is True
    assert "4481" in blob
    assert "Dog in the yard" not in blob
    assert "Sam Rivera" not in blob
    assert "Kitchen sink draining slowly" not in blob


def test_inactive_gate_code_is_left_out(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    gate = session.scalar(select(Memory).where(Memory.customer_id == james.id))
    assert gate is not None
    gate.active = False
    session.commit()
    pack = build_context_pack(session, audience="customer", customer_id=james.id)
    assert pack["customer"]["memories"] == []
    assert "4481" not in json.dumps(pack)


def test_sam_sees_his_quote_and_not_luis(session):
    seed(session)
    sam = _customer(session, "Sam Rivera")
    pack = build_context_pack(session, audience="customer", customer_id=sam.id)
    assert pack["open_jobs"] == [
        {
            "appointment_id": pack["open_jobs"][0]["appointment_id"],
            "service": "drain",
            "summary": "Kitchen sink draining slowly",
            "status": "quoted",
            "start": None,
            "end": None,
            "quote_amount": 149,
        }
    ]
    assert "Luis Alvarez" not in json.dumps(pack)
    assert "4481" not in json.dumps(pack)


def test_customer_messages_stay_on_that_customer(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    priya = _customer(session, "Priya Shah")
    _say(session, james, "The heater is leaking.", kind=ConversationKind.customer)
    _say(session, james, "The gate code is still 4481.", kind=ConversationKind.customer)
    _say(session, priya, "The dog is in the yard.", kind=ConversationKind.customer)
    _say(session, None, "Any cancellations today?", kind=ConversationKind.owner)

    pack = build_context_pack(session, audience="customer", customer_id=james.id)
    assert [message["body"] for message in pack["recent_messages"]] == [
        "The heater is leaking.",
        "The gate code is still 4481.",
    ]
    assert "dog is in the yard" not in json.dumps(pack["recent_messages"])
    assert "cancellations" not in json.dumps(pack)


def test_recent_messages_keep_the_newest_window(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    conversation = Conversation(
        business_id=james.business_id,
        customer_id=james.id,
        kind=ConversationKind.customer,
        created_at=CLOCK,
    )
    session.add(conversation)
    session.flush()
    session.add_all(
        Message(
            conversation_id=conversation.id,
            role=MessageRole.customer,
            body=f"m{index:02d}",
            created_at=CLOCK,
        )
        for index in range(MESSAGE_LIMIT + 1)
    )
    session.commit()

    pack = build_context_pack(session, audience="customer", customer_id=james.id)
    bodies = [message["body"] for message in pack["recent_messages"]]
    assert len(bodies) == MESSAGE_LIMIT
    assert bodies[0] == "m01"
    assert bodies[-1] == f"m{MESSAGE_LIMIT:02d}"
    assert "m00" not in bodies


def test_open_sunday_is_shown_with_monday_hours(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    pack = build_context_pack(session, audience="customer", customer_id=james.id)
    assert pack["settings"]["hours"]["sunday"] == ["08:00", "17:00"]
    assert pack["settings"]["hours"]["saturday"] == ["08:00", "17:00"]
    assert pack["closed_days"] == []

    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    closed = build_context_pack(session, audience="customer", customer_id=james.id)
    assert closed["settings"]["hours"]["sunday"] is None
    assert closed["settings"]["hours"]["saturday"] == ["08:00", "17:00"]
    assert closed["closed_days"] == ["sunday"]


def test_a_stale_closed_sunday_reply_follows_the_current_hours(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    conversation = Conversation(
        business_id=james.business_id,
        customer_id=james.id,
        kind=ConversationKind.customer,
        created_at=CLOCK,
    )
    session.add(conversation)
    session.flush()
    session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                role=MessageRole.assistant,
                body="Our shop is closed on Sundays, so we cannot book.",
                created_at=CLOCK,
            ),
            Message(
                conversation_id=conversation.id,
                role=MessageRole.customer,
                body="Please book Sunday at 10:00 AM.",
                created_at=CLOCK,
            ),
        ]
    )
    session.commit()

    pack = build_context_pack(session, audience="customer", customer_id=james.id)

    assert [message["body"] for message in pack["recent_messages"]] == [
        "Sunday is open from 8:00 AM to 5:00 PM.",
        "Please book Sunday at 10:00 AM.",
    ]

    set_setting(session, "sunday_calls", False, reason="Sunday is closed")
    closed = build_context_pack(session, audience="customer", customer_id=james.id)
    assert closed["recent_messages"][0]["body"] == "Our shop is closed on Sundays, so we cannot book."


def test_owner_pack_has_sunday_setting_open_quotes_and_the_schedule(session):
    seed(session)
    pack = build_context_pack(session, audience="owner")

    assert pack["audience"] == "owner"
    assert pack["now"] == NOW
    assert pack["settings"]["sunday_calls"] is True
    assert pack["recent_messages"] == []
    assert [quote["customer_name"] for quote in pack["open_quotes"]] == ["Sam Rivera"]
    assert pack["open_quotes"][0]["summary"] == "Kitchen sink draining slowly"
    assert pack["open_quotes"][0]["quote_amount"] == 149
    assert pack["open_quotes"][0]["quote_sent_at"] == "2026-11-09T10:00:00-08:00"
    assert [visit["customer_name"] for visit in pack["schedule"]] == ["Luis Alvarez"]
    assert pack["schedule"][0]["technician_name"] == "Brooke Hale"
    assert pack["schedule"][0]["service"] == "drain"
    assert pack["schedule"][0]["start"] == "2026-11-13T09:00:00-08:00"
    assert pack["schedule"][0]["end"] == "2026-11-13T10:00:00-08:00"
    assert "Kitchen faucet cartridge" not in json.dumps(pack["schedule"])
    assert "4481" not in json.dumps(pack)


def test_owner_messages_are_not_mixed_with_customer_messages(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    _say(session, james, "The heater is leaking.", kind=ConversationKind.customer)
    _say(session, None, "Who is still waiting on a quote?", kind=ConversationKind.owner)

    pack = build_context_pack(session, audience="owner")
    assert [message["body"] for message in pack["recent_messages"]] == ["Who is still waiting on a quote?"]
    assert "heater is leaking" not in json.dumps(pack["recent_messages"])


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"audience": "customer"}, "unknown customer"),
        ({"audience": "customer", "customer_id": 9999}, "unknown customer"),
        ({"audience": "vendor"}, "unknown audience"),
    ],
)
def test_context_pack_rejects_a_bad_audience(session, kwargs, message):
    seed(session)
    with pytest.raises(PolicyError, match=message):
        build_context_pack(session, **kwargs)


def test_shop_policy_is_visible_and_customer_facts_stay_put(session):
    seed(session)
    remember(
        session,
        content="We stopped doing Sunday calls",
        kind=MemoryKind.policy,
        reason="Owner said Sunday calls are over",
    )
    remember(session, content="Old rule", kind=MemoryKind.policy, reason="Replaced")
    old = session.scalar(select(Memory).where(Memory.content == "Old rule"))
    assert old is not None
    old.active = False
    session.commit()

    james = _customer(session, "James Okonkwo")
    expected = [{"kind": "policy", "content": "We stopped doing Sunday calls"}]
    owner = build_context_pack(session, audience="owner")
    customer = build_context_pack(session, audience="customer", customer_id=james.id)

    assert owner["shop_memories"] == expected
    assert customer["shop_memories"] == expected
    assert owner["settings"]["service_area"] == ["98103", "98115", "98125", "98133", "98177"]
    assert "Old rule" not in json.dumps(owner)
    assert "Gate code 4481" not in json.dumps(owner)
    assert "Dog in the yard" not in json.dumps(owner)


def test_a_service_area_policy_joins_the_setting(session):
    seed(session)
    remember(
        session,
        content="Add service area 98104 is now allowed.",
        kind=MemoryKind.policy,
        reason="Owner added 98104",
    )

    pack = build_context_pack(session, audience="owner")

    assert pack["settings"]["service_area"] == ["98103", "98115", "98125", "98133", "98177", "98104"]
    assert pack["shop_memories"] == [{"kind": "policy", "content": "Add service area 98104 is now allowed."}]


def test_owner_note_about_a_customer_stays_with_the_owner(session):
    seed(session)
    james = _customer(session, "James Okonkwo")
    _say(session, james, "Check the gate before Friday.", kind=ConversationKind.owner)

    owner = build_context_pack(session, audience="owner")
    customer = build_context_pack(session, audience="customer", customer_id=james.id)

    assert [message["body"] for message in owner["recent_messages"]] == ["Check the gate before Friday."]
    assert customer["recent_messages"] == []
