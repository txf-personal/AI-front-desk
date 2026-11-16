"""One front-desk turn. The model chooses a tool; the shop applies it."""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import build_context_pack
from app.agent.errors import LLMError
from app.agent.hours_text import agree_with_hours, relative_shop_day, unsupported_hours_request
from app.agent.visit_choice import (
    align_booking,
    book_named_visit,
    confirmation,
    offer_options,
    reply_names_wrong_day,
)
from app.agent.tools import execute, narrow_to_preference, tools_for
from app.clock import SHOP_TZ, read_clock
from app.errors import PolicyError, require_text
from app.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    ConversationKind,
    Customer,
    Message,
    MessageRole,
    Technician,
)
from app.services.booking import PAST_TIME, cancel_visits_on_closed_day
from app.services.runner import _day_label
from app.settings_store import require_business, set_weekday

MAX_STEPS = 4
_GAS = re.compile(r"\bgas\b", re.IGNORECASE)
_BOOKED_CLAIM = re.compile(
    r"successfully scheduled|successfully booked|has been scheduled|has been booked|"
    r"i have booked|i've booked|i have scheduled|i've scheduled|"
    r"you(?:'re| are) booked|成功预约|已成功预约",
    re.IGNORECASE,
)
_CLOCK = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)\b|\b\d{1,2}:\d{2}\b|\bat\s+\d{1,2}\b",
    re.IGNORECASE,
)
_NOT_BOOKED = "I have not booked a visit."
_OUTSIDE_AREA = "address is outside the service area"
_OUTSIDE_REPLY = (
    "That address is outside the area we serve. Update the address, and I can check again."
)
_NEED_CHOICE = {
    "error": "not_chosen",
    "detail": "The customer has not chosen a time and a technician. List the open choices. Do not book.",
}

_INSTRUCTIONS = (
    "You are the front desk for this plumbing shop. "
    "The context JSON is the only shop state you may use. "
    "Change the shop only through tools. "
    "Prices, hours, and openings come from that context and from tools. "
    "Each weekday is open from 8:00 AM to 5:00 PM or closed. "
    "A null weekday in settings.hours is closed. "
    "closed_days lists those weekdays. "
    "Trust settings.hours over older messages. "
    "today and tomorrow follow the now timestamp. Tomorrow after a Thursday is Friday. "
    "A time with no date uses the latest today, tomorrow, or weekday the customer named. "
    "Do not book a time that has already passed, and do not describe an older visit as this booking. "
    "Say a closed day is closed. Do not repeat an older reply that disagrees with settings.hours. "
    "Every tool call includes a reason. "
    "Book an ordinary leak, drain, or fixture visit only after the customer has named both a time and a technician. "
    "technician_id must be the id in technicians for the person the customer named. "
    "A yes, or a preference such as morning, is not a choice. "
    "When the customer wants a visit and has not named both a time and a technician, list the open choices. "
    "Do not ask them to invent a time. "
    "Offer only the slots the tool returned, then wait. "
    "A saved preference, such as morning, limits those slots. "
    "Say that other times are still open if the customer asks. "
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
    if audience == "owner":
        notice = unsupported_hours_request(text)
        if notice:
            _message(session, conversation.id, MessageRole.assistant, notice)
            session.commit()
            return Turn(reply=notice, conversation_id=conversation.id)
        relative = relative_shop_day(text, read_clock(session))
        if relative:
            reply = _apply_relative_day(session, text, relative)
            _message(session, conversation.id, MessageRole.assistant, reply)
            session.commit()
            return Turn(reply=reply, conversation_id=conversation.id)
        from app.services.automations import (
            create_visit_reminder,
            parse_daily_reminder,
            parse_reminder_removal,
            reminder_confirmation,
            reminder_removed_reply,
            remove_visit_reminder,
        )

        from app.settings_store import add_service_zips, get_setting, service_area_reply, service_zips_to_add

        zips = service_zips_to_add(text)
        if zips:
            before = get_setting(session, "service_area")
            had = {item for item in before if isinstance(item, str)} if isinstance(before, list) else set()
            add_service_zips(session, zips, reason=text)
            reply = service_area_reply(zips, added=any(zip_code not in had for zip_code in zips))
            _message(session, conversation.id, MessageRole.assistant, reply)
            session.commit()
            return Turn(reply=reply, conversation_id=conversation.id)
        removal = parse_reminder_removal(text)
        if removal:
            hour, minute, day_offset = removal
            removed = remove_visit_reminder(
                session,
                hour=hour,
                minute=minute,
                day_offset=day_offset,
                reason=text,
                conversation_id=conversation.id,
            )
            reply = (
                reminder_removed_reply(hour, minute, day_offset)
                if removed
                else "That reminder has already been removed."
            )
            _message(session, conversation.id, MessageRole.assistant, reply)
            session.commit()
            return Turn(reply=reply, conversation_id=conversation.id)
        reminder = parse_daily_reminder(text)
        if reminder:
            hour, minute, day_offset = reminder
            create_visit_reminder(
                session,
                instruction=text,
                hour=hour,
                minute=minute,
                day_offset=day_offset,
                reason=text,
                conversation_id=conversation.id,
            )
            reply = reminder_confirmation(hour, minute, day_offset)
            _message(session, conversation.id, MessageRole.assistant, reply)
            session.commit()
            return Turn(reply=reply, conversation_id=conversation.id)
    tools = tools_for(audience)
    pack = build_context_pack(session, audience=audience, customer_id=thread_customer_id)
    messages = [
        {"role": "system", "content": _INSTRUCTIONS},
        {"role": "system", "content": json.dumps(pack)},
        {"role": "user", "content": text},
    ]
    booked = False
    booked_start: datetime | None = None
    booked_technician_id: int | None = None
    refused_past: str | None = None
    for _step in range(MAX_STEPS):
        completion = llm.complete(messages, tools)
        if not completion.tool_calls:
            reply = completion.text.strip() if isinstance(completion.text, str) else ""
            if not reply:
                raise LLMError("model reply was empty")
            if audience == "customer" and not booked:
                settled = book_named_visit(session, conversation.id)
                if settled is not None:
                    reply = settled[0]
                elif refused_past:
                    reply = refused_past
                else:
                    offered = offer_options(session, conversation.id)
                    if offered is not None:
                        reply = offered
                    elif _BOOKED_CLAIM.search(reply):
                        reply = _NOT_BOOKED
            elif (
                audience == "customer"
                and booked
                and booked_start is not None
                and reply_names_wrong_day(reply, booked_start, read_clock(session))
            ):
                technician = session.get(Technician, booked_technician_id) if booked_technician_id else None
                if technician is not None:
                    reply = confirmation(booked_start, technician.name)
            reply = agree_with_hours(session, reply, text)
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
            if audience == "customer" and call.name == "book_appointment":
                problem = align_booking(session, conversation.id, call.arguments)
                if problem:
                    refused_past = problem
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"error": "past_time", "detail": problem}),
                        }
                    )
                    continue
            if (
                audience == "customer"
                and call.name == "book_appointment"
                and _mentions_gas(text, call.reason, call.arguments)
            ):
                raise PolicyError("gas is handed to the owner")
            if (
                audience == "customer"
                and call.name == "book_appointment"
                and not _visit_was_chosen(session, conversation.id, call.arguments)
            ):
                result = dict(_NEED_CHOICE)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
                )
                continue
            try:
                result = execute(
                    session,
                    call,
                    audience=audience,
                    customer_id=thread_customer_id,
                    conversation_id=conversation.id,
                )
            except PolicyError as exc:
                if str(exc) == PAST_TIME:
                    refused_past = "That time has already passed. Name a later time."
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps({"error": "past_time", "detail": str(exc)}),
                        }
                    )
                    continue
                if str(exc) != _OUTSIDE_AREA:
                    raise
                _message(session, conversation.id, MessageRole.assistant, _OUTSIDE_REPLY)
                session.commit()
                return Turn(reply=_OUTSIDE_REPLY, conversation_id=conversation.id)
            if audience == "customer" and call.name == "check_availability":
                result = narrow_to_preference(session, thread_customer_id, text, result)
            if call.name == "book_appointment" and result.get("status") == "scheduled":
                booked = True
                booked_start = datetime.fromisoformat(result["start"])
                technician_id = result.get("technician_id")
                booked_technician_id = technician_id if isinstance(technician_id, int) else None
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}
            )
    raise LLMError("tool loop did not finish")


def _apply_relative_day(session: Session, text: str, relative: tuple[str, bool, datetime]) -> str:
    """Close or open the shop day that today or tomorrow names on the shop clock."""
    day, closing, when = relative
    label = _day_label(when)
    set_weekday(session, day, None if closing else True, reason=text)
    if closing:
        cancel_visits_on_closed_day(session, day)
        return f"{label} is closed."
    return f"{label} is open from 8:00 AM to 5:00 PM."


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


def _visit_was_chosen(session: Session, conversation_id: int, arguments: dict) -> bool:
    """A booking needs a clock time and the named technician in the customer's own words."""
    bodies = session.scalars(
        select(Message.body).where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.customer,
        )
    ).all()
    text = "\n".join(bodies)
    if _CLOCK.search(text) is None or not isinstance(arguments, dict):
        return False
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        return False
    named_id = _named_technician_id(session, conversation.business_id, text)
    if named_id is not None:
        arguments["technician_id"] = named_id
        return True
    technician_id = arguments.get("technician_id")
    if isinstance(technician_id, bool) or not isinstance(technician_id, int):
        return False
    technician = session.get(Technician, technician_id)
    return technician is not None and _name_in_text(technician.name, text)


def _named_technician_id(session: Session, business_id: int, text: str) -> int | None:
    rows = session.scalars(
        select(Technician).where(Technician.business_id == business_id).order_by(Technician.id)
    ).all()
    named = [row.id for row in rows if _name_in_text(row.name, text)]
    if len(named) == 1:
        return named[0]
    return None


def _name_in_text(name: str, text: str) -> bool:
    parts = [name, *name.split()]
    return any(re.search(rf"\b{re.escape(part)}\b", text, re.IGNORECASE) for part in parts if part)


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
