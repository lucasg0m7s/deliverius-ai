from datetime import UTC, datetime, timedelta
from typing import Protocol

from deliverius.shared.errors import InvariantError


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that only moves when told to: for tests and for simulating months of orders."""

    def __init__(self, start: datetime) -> None:
        require_aware(start, "start")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def require_aware(moment: datetime, field_name: str) -> None:
    """Domain instants always carry a time zone; a naive one is a bug, not a guess."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise InvariantError(f"naive_datetime.{field_name}")
