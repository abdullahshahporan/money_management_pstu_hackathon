"""An injectable clock.

Time is a dependency, not an ambient fact. Expiry of money requests, daily
limit windows, token lifetimes and idempotency retention are all time-dependent
rules; if they call ``datetime.now()`` directly they can only be tested by
sleeping. Injecting a ``Clock`` makes those rules deterministic under test.

Spec 8.2 invariant 14: all timestamps are stored in UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

__all__ = ["Clock", "FixedClock", "SystemClock"]


class Clock(Protocol):
    """Port for reading the current time."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """Production clock. Always returns timezone-aware UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test clock with an explicitly controllable instant."""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, delta: timedelta) -> None:
        self._instant += delta
