"""Tools the model may call. The audience decides which names run."""

from datetime import date, datetime

from sqlalchemy.orm import Session

from app.agent.types import ToolCall
from app.clock import shop_iso
from app.errors import PolicyError, require_text
from app.models import MemoryKind
from app.services.booking import available_slots, book_appointment, create_quote
from app.services.escalations import open_escalation
from app.services.memory import remember
from app.settings_store import set_setting

CUSTOMER_TOOLS = (
    "remember_customer_fact",
    "check_availability",
    "book_appointment",
    "create_quote",
    "escalate",
)
OWNER_TOOLS = ("update_setting", "remember_policy")


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
    slots = available_slots(
        session,
        customer_id=customer_id,
        service=require_text(_arg(arguments, "service"), "service is required"),
        day=_day(arguments.get("day")),
    )
    return {"slots": slots}


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
    if key == "sunday_calls" and not isinstance(value, bool):
        raise PolicyError("sunday_calls must be true or false")
    try:
        stored = set_setting(session, key, value, reason=reason)
    except KeyError as exc:
        raise PolicyError("unknown setting") from exc
    return {"key": key, "value": stored}


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
        "description": "List open slots for one service on a shop day, YYYY-MM-DD.",
        "parameters": _schema(
            {"service": {"type": "string"}, "day": {"type": "string"}},
            ["service", "day"],
        ),
    },
    "book_appointment": {
        "name": "book_appointment",
        "description": "Book a visit inside shop hours. Price is optional and must match the book.",
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
        "description": "Change an existing shop setting. sunday_calls is true or false.",
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
}

_HANDLERS = {
    "remember_customer_fact": _remember_customer,
    "check_availability": _check_availability,
    "book_appointment": _book,
    "create_quote": _quote,
    "escalate": _escalate,
    "update_setting": _update_setting,
    "remember_policy": _remember_policy,
}
