"""Domain events published through the transactional outbox.

Spec 15.4: events carry a stable envelope and a version. They are written in
the same transaction as the money they describe, so an event exists if and only
if the thing it describes really happened.

Spec 15.4 also warns: no secrets, no password material, no unnecessary personal
information. Events cross service boundaries and end up in logs and queues, so
they carry identifiers and amounts, never credentials or full profiles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

__all__ = [
    "DomainEvent",
    "MoneyRequestAccepted",
    "MoneyRequestCreated",
    "MoneyRequestRejected",
    "TransferSucceeded",
]


@dataclass(frozen=True, slots=True)
class DomainEvent:
    """Base envelope. ``event_type`` is versioned so consumers can evolve."""

    event_type: ClassVar[str] = "domain.event.v1"
    aggregate_type: ClassVar[str] = "unknown"
    schema_version: ClassVar[int] = 1

    aggregate_id: str
    occurred_at: datetime
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TransferSucceeded(DomainEvent):
    event_type: ClassVar[str] = "financial.transfer.succeeded.v1"
    aggregate_type: ClassVar[str] = "transfer"


@dataclass(frozen=True, slots=True)
class MoneyRequestCreated(DomainEvent):
    event_type: ClassVar[str] = "money_request.created.v1"
    aggregate_type: ClassVar[str] = "money_request"


@dataclass(frozen=True, slots=True)
class MoneyRequestAccepted(DomainEvent):
    event_type: ClassVar[str] = "money_request.accepted.v1"
    aggregate_type: ClassVar[str] = "money_request"


@dataclass(frozen=True, slots=True)
class MoneyRequestRejected(DomainEvent):
    event_type: ClassVar[str] = "money_request.rejected.v1"
    aggregate_type: ClassVar[str] = "money_request"
