"""Tools the model may call. The audience decides which names run."""

import re
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.agent.types import ToolCall
from app.clock import shop_iso
from app.errors import PolicyError, require_text
from app.models import MemoryKind
from app.services.automations import create_quote_report, create_visit_reminder, parse_daily_reminder
from app.services.booking import available_slots, book_appointment, cancel_visits_on_closed_day, create_quote
from app.services.customers import set_address
from app.services.escalations import open_escalation
from app.services.memory import active_memories, remember
from app.settings_store import merge_hours, set_setting, set_weekday, weekday_of

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
CUSTOMER_TOOLS = (
    "remember_customer_fact",
    "set_customer_address",
    "check_availability",
    "book_appointment",
    "create_quote",
    "escalate",
)
OWNER_TOOLS = ("update_setting", "remember_policy", "create_automation")


def tools_for(audience: str) -> list[dict]:
    if audience == "customer":
        names = CUSTOMER_TOOLS
    elif audience == "owner":
        names = OWNER_TOOLS
    else:
        raise PolicyError("unknown audience")
    return [_SPECS[name] for name in names]


def execute(
    session: Session,
    call: ToolCall,
    *,
    audience: str,
    customer_id: int | None,
    conversation_id: int | None,
) -> dict:
    """Run one allowed tool. The customer id comes from the turn, not the model."""
    allowed = CUSTOMER_TOOLS if audience == "customer" else OWNER_TOOLS
    if audience not in {"customer", "owner"} or call.name not in allowed:
        raise PolicyError(f"{call.name} is not allowed")
    handler = _HANDLERS[call.name]
    return handler(
        session,
        call.arguments,
        reason=call.reason,
        customer_id=customer_id,
        conversation_id=conversation_id,
    )


def _set_address(session, arguments, *, reason, customer_id, conversation_id):
    if customer_id is None:
        raise PolicyError("unknown customer")
    row = set_address(
        session,
        customer_id=customer_id,
        address=_arg(arguments, "address"),
        reason=reason,
    )
    return {"customer_id": row.id, "address": row.address}


def _remember_customer(session, arguments, *, reason, customer_id, conversation_id):
    kind = _memory_kind(arguments.get("kind"))
    if kind == MemoryKind.policy:
        raise PolicyError("policy is owner only")
    row = remember(
        session,
        content=_arg(arguments, "content"),
        kind=kind,
        reason=reason,
        customer_id=customer_id,
    )
    return {"memory_id": row.id, "kind": row.kind.value, "content": row.content}


def _check_availability(session, arguments, *, reason, customer_id, conversation_id):
    if customer_id is None:
        raise PolicyError("unknown customer")
    day = _day(arguments.get("day"))
    try:
        slots = available_slots(
            session,
            customer_id=customer_id,
            service=require_text(_arg(arguments, "service"), "service is required"),
            day=day,
        )
    except PolicyError as exc:
        if str(exc) in {"sunday calls are off", "outside business hours"}:
            return {"closed": True, "weekday": _DAYS[day.weekday()], "slots": []}
        raise
    return {"closed": False, "slots": slots}


_MORNING = re.compile(r"\bmorning\b|上午", re.IGNORECASE)
_AFTERNOON = re.compile(r"\bafternoon\b|下午", re.IGNORECASE)
_EVENING = re.compile(r"\bevening\b|晚上", re.IGNORECASE)
_PERIODS = (
    ("morning", _MORNING, "Customer prefers morning appointments. These slots start before noon."),
    ("afternoon", _AFTERNOON, "Customer prefers afternoon appointments. These slots start at noon or later."),
    ("evening", _EVENING, "Customer prefers evening appointments. These slots start at 5:00 PM or later."),
)


def narrow_to_preference(session: Session, customer_id: int | None, text: str, result: dict) -> dict:
    """Keep the part of the day the customer prefers, unless this message asks for another."""
    if customer_id is None or not isinstance(result, dict) or result.get("closed"):
        return result
    slots = result.get("slots")
    if not isinstance(slots, list) or not slots:
        return result
    period = _period_named(text) or _saved_period(session, customer_id)
    if period is None:
        return result
    kept = [slot for slot in slots if _slot_period(slot) == period]
    if not kept:
        return result
    note = next(sentence for name, _pattern, sentence in _PERIODS if name == period)
    if _period_named(text):
        note = f"The customer asked for {period} slots."
    return {**result, "slots": kept, "preference": period, "note": note}


def _saved_period(session: Session, customer_id: int) -> str | None:
    texts = [
        row.content
        for row in active_memories(session, customer_id)
        if row.kind == MemoryKind.preference
    ]
    return _period_named("\n".join(texts))


def _period_named(text: str) -> str | None:
    found = [name for name, pattern, _sentence in _PERIODS if pattern.search(text)]
    if len(found) == 1:
        return found[0]
    return None


def _slot_period(slot: object) -> str | None:
    if not isinstance(slot, dict) or not isinstance(slot.get("start"), str):
        return None
    try:
        parsed = datetime.fromisoformat(slot["start"])
    except ValueError:
        return None
    if parsed.hour < 12:
        return "morning"
    if parsed.hour < 17:
        return "afternoon"
    return "evening"


def _book(session, arguments, *, reason, customer_id, conversation_id):
    if customer_id is None:
        raise PolicyError("unknown customer")
    booked = book_appointment(
        session,
        customer_id=customer_id,
        service=require_text(_arg(arguments, "service"), "service is required"),
        start=_start(arguments.get("start")),
        summary=_arg(arguments, "summary"),
        reason=reason,
        technician_id=_integer(arguments.get("technician_id"), "technician_id is required"),
        price=_optional_price(arguments),
    )
    return {
        "appointment_id": booked.id,
        "status": booked.status.value,
        "service": booked.service,
        "start": shop_iso(booked.start),
        "end": shop_iso(booked.end),
        "technician_id": booked.technician_id,
        "address": booked.address,
        "access_notes": booked.access_notes,
    }


def _quote(session, arguments, *, reason, customer_id, conversation_id):
    if customer_id is None:
        raise PolicyError("unknown customer")
    quote = create_quote(
        session,
        customer_id=customer_id,
        service=require_text(_arg(arguments, "service"), "service is required"),
        summary=_arg(arguments, "summary"),
        reason=reason,
        price=_integer(arguments.get("price"), "price is required"),
    )
    return {
        "appointment_id": quote.id,
        "status": quote.status.value,
        "service": quote.service,
        "quote_amount": quote.quote_amount,
        "start": None,
        "address": quote.address,
    }


def _escalate(session, arguments, *, reason, customer_id, conversation_id):
    row = open_escalation(
        session,
        reason=reason,
        detail=require_text(_arg(arguments, "detail"), "detail is required"),
        customer_id=customer_id,
        conversation_id=conversation_id,
    )
    return {"escalation_id": row.id, "status": row.status.value}


def _update_setting(session, arguments, *, reason, customer_id, conversation_id):
    key = require_text(_arg(arguments, "key"), "setting is required")
    if "value" not in arguments:
        raise PolicyError("value is required")
    value = arguments["value"]
    day = weekday_of(key)
    if day is not None:
        span = set_weekday(session, day, value, reason=reason)
        cancelled = cancel_visits_on_closed_day(session, day) if span is None else []
        return {"key": f"hours.{day}", "value": span, "cancelled": cancelled}
    if key == "hours":
        if not isinstance(value, dict):
            raise PolicyError("hours must be a start and end")
        stored = merge_hours(session, value, reason=reason)
        cancelled = []
        for name, span in stored.items():
            if name in value and span is None:
                cancelled.extend(cancel_visits_on_closed_day(session, name))
        return {"key": key, "value": stored, "cancelled": cancelled}
    if key == "sunday_calls" and not isinstance(value, bool):
        raise PolicyError("sunday_calls must be true or false")
    try:
        stored = set_setting(session, key, value, reason=reason)
    except KeyError as exc:
        raise PolicyError("unknown setting") from exc
    cancelled = cancel_visits_on_closed_day(session, "sunday") if key == "sunday_calls" and value is False else []
    return {"key": key, "value": stored, "cancelled": cancelled}


def _create_automation(session, arguments, *, reason, customer_id, conversation_id):
    instruction = _arg(arguments, "instruction")
    reminder = parse_daily_reminder(instruction)
    if reminder is not None:
        hour, minute, day_offset = reminder
        row, _created = create_visit_reminder(
            session,
            instruction=instruction,
            hour=hour,
            minute=minute,
            day_offset=day_offset,
            reason=reason,
            conversation_id=conversation_id,
        )
        return {
            "automation_id": row.id,
            "name": row.name,
            "hour": row.schedule["hour"],
            "minute": row.schedule["minute"],
            "day_offset": row.schedule["day_offset"],
        }
    row = create_quote_report(
        session,
        instruction=_arg(arguments, "instruction"),
        weekday=_arg(arguments, "weekday"),
        hour=_clock_arg(arguments, "hour", 7, 23),
        minute=_clock_arg(arguments, "minute", 30, 59),
        reason=reason,
        conversation_id=conversation_id,
    )
    return {
        "automation_id": row.id,
        "name": row.name,
        "weekday": row.schedule["weekday"],
        "hour": row.schedule["hour"],
        "minute": row.schedule["minute"],
    }


def _remember_policy(session, arguments, *, reason, customer_id, conversation_id):
    row = remember(
        session,
        content=_arg(arguments, "content"),
        kind=MemoryKind.policy,
        reason=reason,
        customer_id=None,
    )
    return {"memory_id": row.id, "kind": row.kind.value, "content": row.content}


def _arg(arguments: dict, key: str):
    if not isinstance(arguments, dict):
        return ""
    return arguments.get(key, "")


def _memory_kind(value) -> MemoryKind:
    if not isinstance(value, str):
        raise PolicyError("unknown memory kind")
    try:
        return MemoryKind(value)
    except ValueError as exc:
        raise PolicyError("unknown memory kind") from exc


def _day(value) -> date:
    if not isinstance(value, str):
        raise PolicyError("day is required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PolicyError("day is required") from exc


def _start(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError("start must be a shop time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyError("start must be a shop time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyError("datetime must be timezone-aware")
    return parsed


def _integer(value, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(message)
    return value


def _clock_arg(arguments: dict, key: str, default: int, limit: int) -> int:
    if not isinstance(arguments, dict) or key not in arguments or arguments[key] is None:
        return default
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        raise PolicyError(f"{key} is out of range")
    return value


def _optional_price(arguments: dict) -> int | None:
    if not isinstance(arguments, dict) or "price" not in arguments or arguments["price"] is None:
        return None
    return _integer(arguments["price"], "price is not on the book")


def _schema(properties: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {**properties, "reason": {"type": "string"}},
        "required": [*required, "reason"],
        "additionalProperties": False,
    }


_SPECS = {
    "set_customer_address": {
        "name": "set_customer_address",
        "description": "Save this customer's street address so a visit can be booked.",
        "parameters": _schema({"address": {"type": "string"}}, ["address"]),
    },
    "remember_customer_fact": {
        "name": "remember_customer_fact",
        "description": "Save a fact about this customer. Kind is access, preference, or fact.",
        "parameters": _schema(
            {
                "content": {"type": "string"},
                "kind": {"type": "string", "enum": ["access", "preference", "fact"]},
            },
            ["content", "kind"],
        ),
    },
    "check_availability": {
        "name": "check_availability",
        "description": (
            "List open slots for one service on a shop day, YYYY-MM-DD. "
            "A closed day returns closed true and no slots. "
            "A customer preference such as morning keeps only that part of the day."
        ),
        "parameters": _schema(
            {"service": {"type": "string"}, "day": {"type": "string"}},
            ["service", "day"],
        ),
    },
    "book_appointment": {
        "name": "book_appointment",
        "description": (
            "Book a visit inside shop hours after the customer has named a time and a technician. "
            "Price is optional and must match the book."
        ),
        "parameters": _schema(
            {
                "service": {"type": "string"},
                "start": {"type": "string"},
                "summary": {"type": "string"},
                "technician_id": {"type": "integer"},
                "price": {"type": "integer"},
            },
            ["service", "start", "summary", "technician_id"],
        ),
    },
    "create_quote": {
        "name": "create_quote",
        "description": "Record a quote at the published price. The slot stays empty.",
        "parameters": _schema(
            {
                "service": {"type": "string"},
                "summary": {"type": "string"},
                "price": {"type": "integer"},
            },
            ["service", "summary", "price"],
        ),
    },
    "escalate": {
        "name": "escalate",
        "description": "Hand this conversation to the owner. Use it for gas or anyone still in danger.",
        "parameters": _schema({"detail": {"type": "string"}}, ["detail"]),
    },
    "update_setting": {
        "name": "update_setting",
        "description": (
            "Change an existing shop setting. sunday_calls is true or false. "
            "hours.<weekday> changes one day: null closes it, true opens 8:00 AM to 5:00 PM. "
            "Other hours are not supported. "
            "To add a service-area zip, call remember_policy with the owner's sentence. "
            "Do not say you cannot change the service area."
        ),
        "parameters": _schema(
            {"key": {"type": "string"}, "value": {}},
            ["key", "value"],
        ),
    },
    "remember_policy": {
        "name": "remember_policy",
        "description": "Remember a shop policy in the owner's words.",
        "parameters": _schema({"content": {"type": "string"}}, ["content"]),
    },
    "create_automation": {
        "name": "create_automation",
        "description": (
            "Save a weekly report of quotes that are still waiting, or a daily customer visit reminder. "
            "A reminder instruction names every day, a clock time, customers, and today or tomorrow. "
            "The default report time is 7:30."
        ),
        "parameters": _schema(
            {
                "instruction": {"type": "string"},
                "weekday": {
                    "type": "string",
                    "enum": ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
                },
                "hour": {"type": "integer"},
                "minute": {"type": "integer"},
            },
            ["instruction", "weekday"],
        ),
    },
}

_HANDLERS = {
    "remember_customer_fact": _remember_customer,
    "set_customer_address": _set_address,
    "check_availability": _check_availability,
    "book_appointment": _book,
    "create_quote": _quote,
    "escalate": _escalate,
    "update_setting": _update_setting,
    "remember_policy": _remember_policy,
    "create_automation": _create_automation,
}
