"""Shop HTTP API. Clock advances change the time only."""

from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy.orm import Session

from app.clock import ClockNotSeeded, advance_clock
from app.errors import PolicyError
from app.present import build_bootstrap, customer_payload, escalation_payload, iso
from app.services.customers import create_customer
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


class AdvanceClock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hours: float | None = None
    to: Literal["next_morning", "next_day"] | None = None

    @model_validator(mode="after")
    def one_change(self):
        if (self.hours is None) == (self.to is None):
            raise ValueError("provide exactly one of hours or to")
        return self


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


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


@router.post("/clock/advance")
def post_clock(body: AdvanceClock, session: SessionDep):
    try:
        clock = advance_clock(session, hours=body.hours, to=body.to)
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


def create_app(session_factory) -> FastAPI:
    app = FastAPI(title="Northside Plumbing")
    app.state.session_factory = session_factory
    app.add_exception_handler(PolicyError, _policy_error)
    app.add_exception_handler(LookupError, _lookup_error)
    app.add_exception_handler(ClockNotSeeded, _clock_missing)
    app.include_router(router)
    return app


def _policy_error(_request, exc: PolicyError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _lookup_error(_request, exc: LookupError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _clock_missing(_request, exc: ClockNotSeeded):
    return JSONResponse(status_code=503, content={"detail": str(exc)})
