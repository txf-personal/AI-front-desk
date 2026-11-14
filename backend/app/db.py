"""SQLite engine and session factory."""

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base

_MEMORY_URLS = {"sqlite://", "sqlite:///:memory:"}


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
    if dbapi_connection.__class__.__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_sqlite_engine(url: str = "sqlite://"):
    """Open SQLite. In-memory URLs share one connection so a second session sees commits."""
    kwargs = {"connect_args": {"check_same_thread": False}}
    if url in _MEMORY_URLS or url.startswith("sqlite:///:memory:"):
        kwargs["poolclass"] = StaticPool
    return create_engine(url, **kwargs)


def create_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)
