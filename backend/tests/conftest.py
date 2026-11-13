"""Database fixtures. Each test gets its own in-memory database."""

import pytest

from app.db import create_session_factory, create_sqlite_engine, init_db


@pytest.fixture
def engine():
    database = create_sqlite_engine()
    init_db(database)
    yield database
    database.dispose()


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def session(session_factory):
    with session_factory() as db:
        yield db
