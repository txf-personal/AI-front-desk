"""Shop HTTP API. Clock advances change the time only. Messages run one model turn."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.orm import Session

from app.agent.errors import ConfigError, LLMError
from app.agent.loop import run_turn
from app.agent.providers import build_client
from app.clock import ClockNotSeeded, advance_clock, parse_shop_time
from app.errors import PolicyError
from app.present import build_bootstrap, customer_payload, escalation_payload, iso
from app.services.customers import create_customer, update_contact
from app.services.escalations import resolve_escalation

router = APIRouter(prefix="/api")


class NewCustomer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    phone: str
    email: str | None = None
    address: str | None = None

    @field_validator("name", "phone")
    @classmethod
    def not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class CustomerContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phone: str
    address: str | None = None

    @field_validator("phone")
    @classmethod
    def phone_not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("address")
    @classmethod
    def blank_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        return text or None


class AdvanceClock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: float | None = None
    to: Literal["next_morning", "next_day"] | None = None
    until: str | None = None

    @model_validator(mode="after")
    def one_change(self):
        if sum(item is not None for item in (self.hours, self.to, self.until)) != 1:
            raise ValueError("provide exactly one of hours, to, or until")
        return self


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class NewMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audience: Literal["customer", "owner"]
    customer_id: int | None = None
    body: str

    @field_validator("body")
    @classmethod
    def not_blank(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @model_validator(mode="after")
    def customer_for_customer_audience(self):
        if self.audience == "customer" and self.customer_id is None:
            raise ValueError("customer_id is required")
        return self


def get_session(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/bootstrap")
def bootstrap(session: SessionDep):
    return build_bootstrap(session)


@router.post("/customers", status_code=201)
def post_customer(body: NewCustomer, session: SessionDep):
    customer = create_customer(
        session,
        name=body.name,
        phone=body.phone,
        email=body.email,
        address=body.address,
    )
    return customer_payload(customer)


@router.patch("/customers/{customer_id}")
def patch_customer(customer_id: int, body: CustomerContact, session: SessionDep):
    customer = update_contact(
        session,
        customer_id=customer_id,
        phone=body.phone,
        address=body.address,
    )
    return customer_payload(customer)


@router.post("/clock/advance")
def post_clock(body: AdvanceClock, session: SessionDep):
    try:
        target = parse_shop_time(body.until) if body.until is not None else None
        clock = advance_clock(session, hours=body.hours, to=body.to, until=target)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"clock": iso(clock)}


@router.post("/escalations/{escalation_id}/resolve")
def post_resolve(
    escalation_id: int,
    session: SessionDep,
    body: ResolveBody = Body(default_factory=ResolveBody),
):
    row = resolve_escalation(session, escalation_id, note=body.note)
    return escalation_payload(row)


@router.post("/messages")
def post_message(body: NewMessage, request: Request, session: SessionDep):
    llm = request.app.state.llm or build_client()
    turn = run_turn(
        session,
        llm,
        audience=body.audience,
        customer_id=body.customer_id,
        body=body.body,
    )
    return {"reply": turn.reply, "conversation_id": turn.conversation_id}


def create_app(session_factory, llm=None) -> FastAPI:
    app = FastAPI(title="Northside Plumbing")
    app.state.session_factory = session_factory
    app.state.llm = llm
    app.add_exception_handler(PolicyError, _policy_error)
    app.add_exception_handler(LookupError, _lookup_error)
    app.add_exception_handler(ClockNotSeeded, _clock_missing)
    app.add_exception_handler(LLMError, _llm_error)
    app.add_exception_handler(ConfigError, _config_error)
    app.include_router(router)
    return app


def _policy_error(_request, exc: PolicyError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _lookup_error(_request, exc: LookupError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _clock_missing(_request, exc: ClockNotSeeded):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def _llm_error(_request, exc: LLMError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


def _config_error(_request, exc: ConfigError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})
