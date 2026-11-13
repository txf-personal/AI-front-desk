"""Read and update the single shop's settings."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import require_text
from app.models import Business, Setting
from app.services.audit import write_audit


def require_business(session: Session) -> Business:
    businesses = session.scalars(select(Business)).all()
    if len(businesses) != 1:
        raise LookupError("expected exactly one business")
    return businesses[0]


def all_settings(session: Session, business_id: int) -> dict:
    return {
        row.key: row.value
        for row in session.scalars(select(Setting).where(Setting.business_id == business_id).order_by(Setting.key))
    }


def get_setting(session: Session, key: str):
    business = require_business(session)
    row = _setting(session, business.id, key)
    if row is None:
        raise KeyError(key)
    return row.value


def set_setting(session: Session, key: str, value, *, reason: str):
    """Replace one existing setting and record why."""
    reason_text = require_text(reason, "reason is required")
    business = require_business(session)
    row = _setting(session, business.id, key)
    if row is None:
        raise KeyError(key)
    row.value = value
    write_audit(
        session,
        business_id=business.id,
        action="set_setting",
        reasoning=reason_text,
        payload={"key": key, "value": value},
        related_type="setting",
        related_id=row.id,
    )
    session.commit()
    return row.value


def _setting(session: Session, business_id: int, key: str) -> Setting | None:
    return session.scalar(select(Setting).where(Setting.business_id == business_id, Setting.key == key))
