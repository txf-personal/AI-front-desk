"""Read the single shop's settings."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Setting


def get_setting(session: Session, key: str):
    businesses = session.scalars(select(Business)).all()
    if len(businesses) != 1:
        raise LookupError("expected exactly one business")
    row = session.scalar(
        select(Setting).where(Setting.business_id == businesses[0].id, Setting.key == key)
    )
    if row is None:
        raise KeyError(key)
    return row.value
