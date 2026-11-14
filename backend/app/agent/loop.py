"""One front-desk turn. The model chooses a tool; the shop applies it."""

import json
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import build_context_pack
from app.agent.errors import LLMError
from app.agent.tools import execute, tools_for
from app.clock import read_clock
from app.errors import PolicyError, require_text
from app.models import Appointment, AppointmentStatus, Conversation, ConversationKind, Customer, Message, MessageRole
from app.settings_store import require_business

MAX_STEPS = 4
_GAS = re.compile(r"\bgas\b", re.IGNORECASE)

_INSTRUCTIONS = (
    "You are the front desk for this plumbing shop. "
    "The context JSON is the only shop state you may use. "
    "Change the shop only through tools. "
    "Prices, hours, and openings come from that context and from tools. "
    "Every tool call includes a reason. "
    "Book an ordinary leak, drain, or fixture visit. "
    "If someone reports gas, a danger they are still in, or another emergency, "
    "escalate and do not book."
)


@dataclass(frozen=True)
class Turn:
    reply: str
    conversation_id: int


def run_turn(
    session: Session,
    llm,
    *,
    audience: str,
    body: str,
    customer_id: int | None = None,
) -> Turn:
    """Save the message, let the model call allowed tools, and save its reply."""
    try:
        return _run(session, llm, audience=audience, body=body, customer_id=customer_id)
    except (PolicyError, LLMError):
        session.rollback()
        raise


def _run(session, llm, *, audience: str, body: str, customer_id: int | None) -> Turn:
    text = require_text(body, "message is required")
    business, thread_customer_id = _audience(session, audience, customer_id)
    conversation = _conversation(session, business.id, audience, thread_customer_id)
    _message(session, conversation.id, _speaker(audience), text)
    if audience == "customer":
        _note_reply(session, thread_customer_id)
    session.flush()
    tools = tools_for(audience)
    pack = build_context_pack(session, audience=audience, customer_id=thread_customer_id)
    messages = [
        {"role": "system", "content": _INSTRUCTIONS},
        {"role": "system", "content": json.dumps(pack)},
        {"role": "user", "content": text},
    ]
    for _step in range(MAX_STEPS):
        completion = llm.complete(messages, tools)
        if not completion.tool_calls:
            reply = completion.text.strip() if isinstance(completion.text, str) else ""
            if not reply:
                raise LLMError("model reply was empty")
            _message(session, conversation.id, MessageRole.assistant, reply)
            session.commit()
            return Turn(reply=reply, conversation_id=conversation.id)
        messages.append(
            {
                "role": "assistant",
                "content": completion.text,
                "tool_calls": [
                    {
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                        "reason": call.reason,
                    }
                    for call in completion.tool_calls
                ],
            }
        )
        for call in completion.tool_calls:
            if (
                audience == "customer"
                and call.name == "book_appointment"
                and _mentions_gas(text, call.reason, call.arguments)
            ):
                raise PolicyError("gas is handed to the owner")
            result = execute(
                session,
                call,
                audience=audience,
                customer_id=thread_customer_id,
                conversation_id=conversation.id,
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
    raise LLMError("tool loop did not finish")


def _audience(session: Session, audience: str, customer_id: int | None):
    if audience not in {"customer", "owner"}:
        raise PolicyError("unknown audience")
    business = require_business(session)
    if audience == "owner":
        return business, None
    customer = session.get(Customer, customer_id)
    if customer is None or customer.business_id != business.id:
        raise PolicyError("unknown customer")
    return business, customer.id


def _conversation(session: Session, business_id: int, audience: str, customer_id: int | None) -> Conversation:
    kind = ConversationKind.customer if audience == "customer" else ConversationKind.owner
    filters = [Conversation.business_id == business_id, Conversation.kind == kind]
    if customer_id is None:
        filters.append(Conversation.customer_id.is_(None))
    else:
        filters.append(Conversation.customer_id == customer_id)
    existing = session.scalar(select(Conversation).where(*filters).order_by(Conversation.id.desc()))
    if existing is not None:
        return existing
    conversation = Conversation(
        business_id=business_id,
        customer_id=customer_id,
        kind=kind,
        created_at=read_clock(session),
    )
    session.add(conversation)
    session.flush()
    return conversation


def _mentions_gas(text: str, reason: str, arguments: dict) -> bool:
    blob = f"{text}\n{reason}\n{json.dumps(arguments)}"
    return _GAS.search(blob) is not None


def _note_reply(session: Session, customer_id: int) -> None:
    """A customer message answers quotes that are already waiting.

    The clock can stay still between turns, so a reply at the same reading is stored one second later. A quote opened after this stamp keeps the clock time and stays waiting.
    """
    now = read_clock(session)
    quotes = session.scalars(
        select(Appointment).where(
            Appointment.customer_id == customer_id,
            Appointment.status == AppointmentStatus.quoted,
        )
    ).all()
    for quote in quotes:
        sent = quote.quote_sent_at
        quote.last_customer_message_at = sent + timedelta(seconds=1) if sent is not None and now == sent else now


def _speaker(audience: str) -> MessageRole:
    return MessageRole.customer if audience == "customer" else MessageRole.owner


def _message(session: Session, conversation_id: int, role: MessageRole, body: str) -> Message:
    message = Message(
        conversation_id=conversation_id,
        role=role,
        body=body,
        created_at=read_clock(session),
    )
    session.add(message)
    return message
