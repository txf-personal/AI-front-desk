"""The day and clock a customer named, when the model picks a different day."""

import re
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import SHOP_TZ, read_clock, shop_iso
from app.errors import PolicyError
from app.models import (
    Appointment,
    AppointmentStatus,
    AuditEvent,
    Conversation,
    ConversationKind,
    Message,
    MessageRole,
    Technician,
)
from app.agent.tools import narrow_to_preference
from app.services.booking import PAST_TIME, available_slots, book_appointment
from app.settings_store import get_setting

_TIME = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_TODAY = re.compile(r"\btoday\b", re.IGNORECASE)
_TOMORROW = re.compile(r"\btomorrow\b", re.IGNORECASE)
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
_MONTH_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
_MONTH_INDEX = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_GAS = re.compile(r"\bgas\b", re.IGNORECASE)
_BOOKING = re.compile(r"\b(?:book|booking|appointment|schedule|reschedule)\b", re.IGNORECASE)
_PRICE = re.compile(r"\b(?:cost|costs|price|priced|how much)\b|\$", re.IGNORECASE)
_HOURS_QUESTION = re.compile(r"\b(?:hours|open|closed)\b", re.IGNORECASE)
_PERIOD = re.compile(r"\b(?:morning|afternoon|evening)\b|上午|下午|晚上", re.IGNORECASE)
_ASKS_TO_CHOOSE = re.compile(
    r"exact time|which technician|what time|technician you(?:'d| would) like",
    re.IGNORECASE,
)
_CLOSED_DAY = {"outside business hours", "sunday calls are off"}
_OUTSIDE_REPLY = "That address is outside the area we serve. Update the address, and I can check again."
_NEED_ADDRESS = "I need a service address before I can offer times."
_WEEKDAYS = (
    ("monday", 0),
    ("tuesday", 1),
    ("wednesday", 2),
    ("thursday", 3),
    ("friday", 4),
    ("saturday", 5),
    ("sunday", 6),
)
_SERVICES = (
    ("water_heater", re.compile(r"water\s*heaters?", re.IGNORECASE)),
    ("faucet", re.compile(r"\b(?:faucets?|toilets?)\b", re.IGNORECASE)),
    ("drain", re.compile(r"\bdrains?\b", re.IGNORECASE)),
    ("leak", re.compile(r"\bleaks?\b", re.IGNORECASE)),
)
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_KEPT = (AppointmentStatus.scheduled, AppointmentStatus.completed)


class VisitChoice:
    def __init__(self, service: str, summary: str, price: int, start: datetime, technician_id: int, technician: str, reason: str):
        self.service = service
        self.summary = summary
        self.price = price
        self.start = start
        self.technician_id = technician_id
        self.technician = technician
        self.reason = reason


def align_booking(session: Session, conversation_id: int, arguments: dict) -> str | None:
    """Point a tool call at the day the customer named. A past time is refused."""
    if not isinstance(arguments, dict):
        return None
    choice = visit_choice(session, conversation_id)
    now = read_clock(session)
    if choice is not None:
        if choice.start < now:
            return past_sentence(choice.start)
        arguments["start"] = shop_iso(choice.start)
        arguments["service"] = choice.service
        arguments["technician_id"] = choice.technician_id
        if not str(arguments.get("summary") or "").strip():
            arguments["summary"] = choice.summary
        return None
    parsed = _parsed_start(arguments.get("start"))
    if parsed is not None and parsed < now:
        return past_sentence(parsed)
    return None


def book_named_visit(session: Session, conversation_id: int) -> tuple[str, bool] | None:
    """Book the visit the latest customer message named, when that message has a clock and a technician.

    The bool is true when a new visit was saved.
    """
    choice = visit_choice(session, conversation_id)
    if choice is None:
        return None
    if choice.start < read_clock(session):
        return past_sentence(choice.start), False
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.customer_id is None:
        return None
    if _existing_at(session, conversation.customer_id, choice.start) is not None:
        return confirmation(choice.start, choice.technician, already=True), False
    try:
        book_appointment(
            session,
            customer_id=conversation.customer_id,
            service=choice.service,
            start=choice.start,
            summary=choice.summary,
            reason=choice.reason,
            technician_id=choice.technician_id,
            price=choice.price,
        )
    except PolicyError as exc:
        text = str(exc)
        if text == PAST_TIME:
            return past_sentence(choice.start), False
        if text == "customer is already booked":
            return "That time is already booked.", False
        if text == "technician is already booked":
            return f"{choice.technician} is already booked then.", False
        if text in {"outside business hours", "sunday calls are off"}:
            return "That time is outside shop hours.", False
        return None
    return confirmation(choice.start, choice.technician), True


def book_pending_choices(session: Session) -> None:
    """Save a future visit a customer already named, when that turn did not book it.

    Each customer message is read with only the messages that came before it, so a later
    past date does not erase an earlier day. A visit already cancelled for that time stays
    cancelled when the day opens again.
    """
    rows = session.scalars(
        select(Conversation).where(Conversation.kind == ConversationKind.customer)
    ).all()
    added = False
    now = read_clock(session)
    for conversation in rows:
        messages = session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.role == MessageRole.customer,
            )
            .order_by(Message.id)
        ).all()
        for message in messages:
            choice = visit_choice(session, conversation.id, through_message_id=message.id)
            if choice is None or choice.start < now:
                continue
            if conversation.customer_id is None or _existing_at(session, conversation.customer_id, choice.start):
                continue
            if _cancelled_at(session, conversation.customer_id, choice.start):
                continue
            try:
                book_appointment(
                    session,
                    customer_id=conversation.customer_id,
                    service=choice.service,
                    start=choice.start,
                    summary=choice.summary,
                    reason=choice.reason,
                    technician_id=choice.technician_id,
                    price=choice.price,
                )
            except PolicyError:
                continue
            reply = confirmation(choice.start, choice.technician)
            exists = session.scalar(
                select(Message.id).where(Message.conversation_id == conversation.id, Message.body == reply)
            )
            if exists is not None:
                continue
            session.add(
                Message(
                    conversation_id=conversation.id,
                    role=MessageRole.assistant,
                    body=reply,
                    created_at=now,
                )
            )
            added = True
    if added:
        session.commit()


def offer_options(session: Session, conversation_id: int, *, through_message_id: int | None = None) -> str | None:
    """The services or open times to show when a visit is not fully chosen."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.customer_id is None:
        return None
    messages = _customer_messages(session, conversation_id, through_message_id)
    if not messages:
        return None
    latest = messages[-1]
    if _GAS.search(latest.body) or _PRICE.search(latest.body) or _HOURS_QUESTION.search(latest.body):
        return None
    if not _is_choosing(latest.body, latest.created_at):
        return None
    if visit_choice(session, conversation_id, through_message_id=through_message_id) is not None:
        return None
    service_id, day = _service_and_day(messages)
    booking = any(_BOOKING.search(message.body) for message in messages)
    if service_id is None and not booking:
        return None
    if day is None and service_id is None:
        return None
    if day is not None and day < read_clock(session).astimezone(SHOP_TZ).date():
        return f"{_day_phrase(day)} has already passed. Name a later day."
    if service_id is None:
        return _service_menu(session, conversation.customer_id, day)
    if day is None:
        if not booking:
            return None
        day = _soonest_day(session, conversation.customer_id, service_id)
        if day is None:
            spec = _resolved_spec(session, service_id)
            name = spec["name"] if spec else "visit"
            return f"There is no open day for a {name} this week."
    return _slot_menu(session, conversation.customer_id, service_id, day, _preference_text(messages))


def fill_stated_offers(session: Session) -> None:
    """Replace an open-ended time question with the choices that were available."""
    rows = session.scalars(
        select(Conversation).where(Conversation.kind == ConversationKind.customer)
    ).all()
    changed = False
    for conversation in rows:
        messages = session.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
        ).all()
        for index, message in enumerate(messages):
            if message.role != MessageRole.customer or index + 1 >= len(messages):
                continue
            nxt = messages[index + 1]
            if nxt.role != MessageRole.assistant:
                continue
            later = {
                item.body
                for item in messages[index + 1 :]
                if item.role == MessageRole.customer
            }
            held = _hold_later_bookings(session, conversation.customer_id, later)
            try:
                offer = offer_options(session, conversation.id, through_message_id=message.id)
            finally:
                _release_held(session, held)
            if offer is None or nxt.body == offer:
                continue
            if _ASKS_TO_CHOOSE.search(nxt.body) is None and not _generated_offer(nxt.body):
                continue
            nxt.body = offer
            changed = True
    if changed:
        session.commit()


def confirmation(start: datetime, technician: str, *, already: bool = False) -> str:
    booked = "You're already booked" if already else "You're booked"
    return f"{booked} {when_phrase(start)} with {technician}."


def past_sentence(start: datetime) -> str:
    return f"{when_phrase(start)} has already passed. Name a later time."


def reply_names_wrong_day(reply: str, start: datetime, now: datetime) -> bool:
    """True when the reply says today or tomorrow and the visit is on another day."""
    if not isinstance(reply, str):
        return False
    start_day = start.astimezone(SHOP_TZ).date()
    today = now.astimezone(SHOP_TZ).date()
    if _TODAY.search(reply) and start_day != today:
        return True
    if _TOMORROW.search(reply) and start_day != today + timedelta(days=1):
        return True
    return False


def when_phrase(start: datetime) -> str:
    local = start.astimezone(SHOP_TZ)
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return (
        f"{_WEEKDAY_NAMES[local.weekday()]}, {_MONTHS[local.month - 1]} {local.day} "
        f"at {hour}:{local.minute:02d} {suffix}"
    )


def visit_choice(
    session: Session, conversation_id: int, *, through_message_id: int | None = None
) -> VisitChoice | None:
    """A clock, a technician, and a service from the customer's own words."""
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.customer_id is None:
        return None
    messages = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    ).all()
    customer_messages = [
        message
        for message in messages
        if message.role == MessageRole.customer
        and (through_message_id is None or message.id <= through_message_id)
    ]
    if not customer_messages:
        return None
    latest = customer_messages[-1]
    if _GAS.search(latest.body):
        return None
    clock = named_clock(latest.body)
    technician_id = _one_technician(session, conversation.business_id, latest.body)
    if clock is None or technician_id is None:
        return None
    service_id = None
    day = None
    for message in reversed(customer_messages):
        if service_id is None:
            service_id = named_service(message.body)
        if day is None:
            day = named_day(message.body, message.created_at)
        if service_id is not None and day is not None:
            break
    if service_id is None:
        return None
    if day is None:
        day = latest.created_at.astimezone(SHOP_TZ).date()
    spec = _spec(session, service_id)
    technician = session.get(Technician, technician_id)
    if spec is None or technician is None:
        return None
    return VisitChoice(
        service=service_id,
        summary=spec["name"],
        price=spec["price"],
        start=datetime.combine(day, clock, tzinfo=SHOP_TZ),
        technician_id=technician.id,
        technician=technician.name,
        reason=latest.body,
    )


def named_clock(text: str) -> time | None:
    found = _TIME.search(text)
    if found is None:
        return None
    hour = int(found.group(1))
    minute = int(found.group(2) or 0)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        return None
    suffix = found.group(3).lower().replace(".", "")
    if suffix == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = hour if hour == 12 else hour + 12
    return time(hour, minute)


def named_day(text: str, anchor: datetime) -> date | None:
    local = anchor.astimezone(SHOP_TZ)
    explicit = _explicit_date(text, local)
    if explicit is not None:
        return explicit
    named = [index for name, index in _WEEKDAYS if re.search(rf"\b{name}s?\b", text, re.IGNORECASE)]
    if len(named) == 1:
        return (local + timedelta(days=(named[0] - local.weekday()) % 7)).date()
    if len(named) > 1:
        return None
    today = _TODAY.search(text) is not None
    tomorrow = _TOMORROW.search(text) is not None
    if today == tomorrow:
        return None
    if today:
        return local.date()
    return (local + timedelta(days=1)).date()


def _explicit_date(text: str, anchor: datetime) -> date | None:
    """A month/day the customer wrote, in the year they wrote unless they gave one."""
    found: list[date] = []
    for match in _NUMERIC_DATE.finditer(text):
        parsed = _calendar_date(int(match.group(1)), int(match.group(2)), match.group(3), anchor.year)
        if parsed is not None:
            found.append(parsed)
    for match in _MONTH_DATE.finditer(text):
        month = _MONTH_INDEX[match.group(1)[:3].lower()]
        parsed = _calendar_date(month, int(match.group(2)), match.group(3), anchor.year)
        if parsed is not None:
            found.append(parsed)
    unique = list(dict.fromkeys(found))
    if len(unique) == 1:
        return unique[0]
    return None


def _calendar_date(month: int, day: int, year_text: str | None, anchor_year: int) -> date | None:
    if year_text is None:
        year = anchor_year
    else:
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def named_service(text: str) -> str | None:
    for service_id, pattern in _SERVICES:
        if pattern.search(text):
            return service_id
    return None


def _one_technician(session: Session, business_id: int, text: str) -> int | None:
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


def _spec(session: Session, service_id: str) -> dict | None:
    book = get_setting(session, "price_book")
    if not isinstance(book, dict):
        return None
    for item in book.get("services") or []:
        if isinstance(item, dict) and item.get("id") == service_id:
            return item
    return None


def _parsed_start(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(SHOP_TZ)


def _cancelled_at(session: Session, customer_id: int, start: datetime) -> Appointment | None:
    """A visit cancelled for this time stays cancelled when the day opens again."""
    target = start.astimezone(SHOP_TZ)
    rows = session.scalars(
        select(Appointment).where(
            Appointment.customer_id == customer_id,
            Appointment.status == AppointmentStatus.cancelled,
            Appointment.start.is_not(None),
        )
    ).all()
    for row in rows:
        if row.start.astimezone(SHOP_TZ) == target:
            return row
    return None


def _existing_at(session: Session, customer_id: int, start: datetime) -> Appointment | None:
    target = start.astimezone(SHOP_TZ)
    rows = session.scalars(
        select(Appointment).where(
            Appointment.customer_id == customer_id,
            Appointment.status.in_(_KEPT),
            Appointment.start.is_not(None),
        )
    ).all()
    for row in rows:
        if row.start.astimezone(SHOP_TZ) == target:
            return row
    return None


def _generated_offer(body: str) -> bool:
    first, _sep, _rest = body.partition("\n")
    if first.startswith("Which visit should I book for "):
        return True
    return "\n1. " in body and first.endswith(":")


def _hold_later_bookings(session: Session, customer_id: int | None, later_bodies: set[str]):
    """Hide visits booked from later messages so an earlier question shows the times that were open."""
    if customer_id is None or not later_bodies:
        return []
    audits = session.scalars(
        select(AuditEvent).where(
            AuditEvent.action == "book_appointment",
            AuditEvent.related_type == "appointment",
        )
    ).all()
    held = []
    for audit in audits:
        if audit.reasoning not in later_bodies or audit.related_id is None:
            continue
        appointment = session.get(Appointment, audit.related_id)
        if appointment is None or appointment.customer_id != customer_id or appointment.status not in _KEPT:
            continue
        held.append((appointment, appointment.status))
        appointment.status = AppointmentStatus.cancelled
    if held:
        session.flush()
    return held


def _release_held(session: Session, held) -> None:
    if not held:
        return
    for appointment, status in held:
        appointment.status = status
    session.flush()


def _is_choosing(text: str, anchor: datetime) -> bool:
    """The latest message is still picking a visit, not a later unrelated note."""
    return bool(
        _BOOKING.search(text)
        or named_service(text)
        or named_day(text, anchor)
        or named_clock(text)
    )


def _customer_messages(session: Session, conversation_id: int, through_message_id: int | None) -> list[Message]:
    messages = session.scalars(
        select(Message).where(Message.conversation_id == conversation_id).order_by(Message.id)
    ).all()
    return [
        message
        for message in messages
        if message.role == MessageRole.customer
        and (through_message_id is None or message.id <= through_message_id)
    ]


def _service_and_day(messages: list[Message]) -> tuple[str | None, date | None]:
    service_id = None
    day = None
    for message in reversed(messages):
        if service_id is None:
            service_id = named_service(message.body)
        if day is None:
            day = named_day(message.body, message.created_at)
        if service_id is not None and day is not None:
            break
    return service_id, day


def _preference_text(messages: list[Message]) -> str:
    for message in reversed(messages):
        if _PERIOD.search(message.body):
            return message.body
    return messages[-1].body


def _resolved_spec(session: Session, service_id: str) -> dict | None:
    spec = _spec(session, service_id)
    if spec is None and service_id == "water_heater":
        return _spec(session, "water_heater_diagnostic")
    return spec


def _service_menu(session: Session, customer_id: int, day: date | None) -> str | None:
    if day is None:
        return None
    status = _day_status(session, customer_id, day)
    if status == "closed":
        return f"{_day_phrase(day)} is closed."
    if status == "outside":
        return _OUTSIDE_REPLY
    if status == "no_address":
        return _NEED_ADDRESS
    if status == "empty":
        return f"{_day_phrase(day)} has no open time left."
    book = get_setting(session, "price_book")
    services = [item for item in (book.get("services") or []) if isinstance(item, dict)]
    if not services:
        return None
    lines = [f"Which visit should I book for {_day_phrase(day)}?"]
    for index, item in enumerate(services, start=1):
        lines.append(f"{index}. {item['name']}, ${item['price']}")
    return "\n".join(lines)


def _slot_menu(session: Session, customer_id: int, service_id: str, day: date, preference_text: str) -> str | None:
    found = _availability(session, customer_id, service_id, day)
    if found is None:
        return None
    kind, spec, slots = found
    phrase = _day_phrase(day)
    if kind == "closed":
        return f"{phrase} is closed."
    if kind == "outside":
        return _OUTSIDE_REPLY
    if kind == "no_address":
        return _NEED_ADDRESS
    narrowed = narrow_to_preference(session, customer_id, preference_text, {"slots": slots})
    shown = narrowed.get("slots") if isinstance(narrowed, dict) else slots
    if not isinstance(shown, list) or not shown:
        return f"{phrase} has no open time left for a {spec['name']}."
    preference = narrowed.get("preference") if isinstance(narrowed, dict) else None
    header = f"{phrase}, {preference}, {spec['name']}:" if preference else f"{phrase}, {spec['name']}:"
    return "\n".join([header, *_option_lines(shown)])


def _soonest_day(session: Session, customer_id: int, service_id: str) -> date | None:
    start = read_clock(session).astimezone(SHOP_TZ).date()
    for offset in range(8):
        day = start + timedelta(days=offset)
        found = _availability(session, customer_id, service_id, day)
        if found is None:
            return None
        kind, _spec_row, slots = found
        if kind == "slots" and slots:
            return day
    return None


def _day_status(session: Session, customer_id: int, day: date) -> str:
    saw_open = False
    for service_id in ("leak", "drain", "faucet", "water_heater_diagnostic"):
        found = _availability(session, customer_id, service_id, day)
        if found is None:
            continue
        kind, _spec_row, slots = found
        if kind == "closed":
            return "closed"
        if kind in {"outside", "no_address"}:
            return kind
        saw_open = True
        if slots:
            return "open"
    return "empty" if saw_open else "closed"


def _availability(session: Session, customer_id: int, service_id: str, day: date):
    spec = _resolved_spec(session, service_id)
    if spec is None:
        return None
    try:
        slots = available_slots(session, customer_id=customer_id, service=spec["id"], day=day)
    except PolicyError as exc:
        text = str(exc)
        if text in _CLOSED_DAY:
            return ("closed", spec, [])
        if text == "address is outside the service area":
            return ("outside", spec, [])
        if text == "customer has no address":
            return ("no_address", spec, [])
        return None
    now = read_clock(session).astimezone(SHOP_TZ)
    kept = []
    for slot in slots:
        if not isinstance(slot, dict) or not isinstance(slot.get("start"), str):
            continue
        try:
            start = datetime.fromisoformat(slot["start"])
        except ValueError:
            continue
        if start.tzinfo is None or start.utcoffset() is None:
            continue
        if start.astimezone(SHOP_TZ) >= now:
            kept.append(slot)
    return ("slots", spec, kept)


def _option_lines(slots: list[dict]) -> list[str]:
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for slot in slots:
        start = datetime.fromisoformat(slot["start"]).astimezone(SHOP_TZ)
        label = _clock_label(start)
        if label not in groups:
            order.append(label)
            groups[label] = []
        name = slot.get("technician_name")
        if isinstance(name, str) and name not in groups[label]:
            groups[label].append(name)
    return [f"{index}. {label} with {' or '.join(groups[label])}" for index, label in enumerate(order, start=1)]


def _day_phrase(day: date) -> str:
    return f"{_WEEKDAY_NAMES[day.weekday()]}, {_MONTHS[day.month - 1]} {day.day}"


def _clock_label(start: datetime) -> str:
    hour = start.hour % 12 or 12
    suffix = "AM" if start.hour < 12 else "PM"
    return f"{hour}:{start.minute:02d} {suffix}"
