"""Shop-local times used by the unit tests."""

from datetime import datetime
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")


def pt(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PT)


def assert_wall(actual: datetime | None, expected: datetime) -> None:
    assert actual is not None, "expected a datetime"
    assert actual.utcoffset() is not None, "expected a timezone-aware datetime"
    assert actual.astimezone(PT) == expected
