"""Clock abstraction. Every time-dependent block takes a Clock so tests control time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """Real wall clock, always timezone-aware (UTC)."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Test clock. Moves only forward, only when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        start = start or datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
        if start.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("clock cannot move backwards")
        self._now += delta
        return self._now

    def set(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            raise ValueError("clock must be timezone-aware")
        if when < self._now:
            raise ValueError("clock cannot move backwards")
        self._now = when
        return self._now
