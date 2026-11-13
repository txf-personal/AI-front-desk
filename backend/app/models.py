"""Shop tables.

PR1 stores the business, its people, and the sample week. Later pull
requests fill conversations, automations, and the audit log.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.types import AwareDateTime


class Base(DeclarativeBase):
    pass


class AppointmentStatus(StrEnum):
    quoted = "quoted"
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"


class MemoryKind(StrEnum):
    access = "access"
    preference = "preference"
    fact = "fact"
    policy = "policy"


class ConversationKind(StrEnum):
    customer = "customer"
    owner = "owner"


class MessageRole(StrEnum):
    customer = "customer"
    owner = "owner"
    assistant = "assistant"
    system = "system"


class AutomationKind(StrEnum):
    morning_rundown = "morning_rundown"
    appointment_reminder = "appointment_reminder"
    quote_followup = "quote_followup"
    quote_report = "quote_report"


class AutomationAuthor(StrEnum):
    seed = "seed"
    owner = "owner"


class AuditActor(StrEnum):
    assistant = "assistant"
    system = "system"
    owner = "owner"


class EscalationStatus(StrEnum):
    open = "open"
    resolved = "resolved"


def _enum(enum_type: type[StrEnum], *, length: int = 32):
    return Enum(enum_type, native_enum=False, length=length)


class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    owner_name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32))


class Setting(Base):
    __tablename__ = "settings"
    __table_args__ = (UniqueConstraint("business_id", "key", name="uq_setting_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)


class Technician(Base):
    __tablename__ = "technicians"
    __table_args__ = (UniqueConstraint("business_id", "name", name="uq_technician_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    skills: Mapped[list[str]] = mapped_column(JSON)


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "phone", name="uq_customer_phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    kind: Mapped[MemoryKind] = mapped_column(_enum(MemoryKind))
    content: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    active: Mapped[bool] = mapped_column(default=True)


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    technician_id: Mapped[int | None] = mapped_column(ForeignKey("technicians.id"), nullable=True, index=True)
    service: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    status: Mapped[AppointmentStatus] = mapped_column(_enum(AppointmentStatus))
    start: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    end: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    quote_amount: Mapped[int | None] = mapped_column(nullable=True)
    quote_sent_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    last_customer_message_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    address: Mapped[str | None] = mapped_column(String(300), nullable=True)
    access_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    kind: Mapped[ConversationKind] = mapped_column(_enum(ConversationKind))
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[MessageRole] = mapped_column(_enum(MessageRole))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)


class Automation(Base):
    __tablename__ = "automations"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text)
    kind: Mapped[AutomationKind] = mapped_column(_enum(AutomationKind))
    schedule: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_by: Mapped[AutomationAuthor] = mapped_column(_enum(AutomationAuthor))
    next_run_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class AutomationRun(Base):
    __tablename__ = "automation_runs"
    __table_args__ = (UniqueConstraint("automation_id", "dedupe_key", name="uq_automation_run"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    automation_id: Mapped[int] = mapped_column(ForeignKey("automations.id"), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(200))
    ran_at: Mapped[datetime] = mapped_column(AwareDateTime)
    summary: Mapped[str] = mapped_column(Text)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    actor: Mapped[AuditActor] = mapped_column(_enum(AuditActor))
    action: Mapped[str] = mapped_column(String(80))
    reasoning: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    related_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    related_id: Mapped[int | None] = mapped_column(nullable=True)


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(160))
    detail: Mapped[str] = mapped_column(Text)
    status: Mapped[EscalationStatus] = mapped_column(_enum(EscalationStatus), default=EscalationStatus.open)
    created_at: Mapped[datetime] = mapped_column(AwareDateTime)
    resolved_at: Mapped[datetime | None] = mapped_column(AwareDateTime, nullable=True)


class SimClock(Base):
    __tablename__ = "sim_clock"

    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), unique=True)
    current_time: Mapped[datetime] = mapped_column(AwareDateTime)
