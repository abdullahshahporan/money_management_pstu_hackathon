"""Transactional outbox and consumer inbox (spec 15).

The dual-write problem: committing to PostgreSQL and then publishing to a
broker are two operations that can fail independently. A crash between them
leaves a transfer that really happened but that nobody downstream ever hears
about.

The fix is to make publishing a *consequence* of the commit rather than a
second write. The event row is inserted in the same transaction as the money,
so it is impossible for one to exist without the other. A relay then publishes
it afterwards, at-least-once, and consumers deduplicate through the inbox.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_.database.base import Base
from platform_.database.types import ULID_LENGTH, UlidPk, created_at_column


class OutboxEventRecord(Base):
    """A durable promise to publish a domain event that already committed."""

    __tablename__ = "outbox_events"

    id: Mapped[UlidPk]
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(CHAR(ULID_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Correlates the event with the API request that produced it (spec 23.3).
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

    occurred_at: Mapped[datetime] = created_at_column()
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Spec 15.5: bounded attempts with backoff, then the dead-letter state.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        # Spec 9.3: a partial index. The relay only ever scans unpublished rows,
        # so indexing the published ones would cost write throughput for nothing
        # and would grow without bound as the table does.
        Index(
            "ix_outbox_events_unpublished",
            "next_attempt_at",
            "occurred_at",
            postgresql_where="published_at IS NULL AND dead_lettered_at IS NULL",
        ),
    )


class ConsumerInboxRecord(Base):
    """Deduplication for at-least-once delivery (spec 15.3).

    The broker may deliver the same event more than once - that is the honest
    guarantee, and we do not pretend otherwise. A consumer inserts its
    ``(consumer_name, event_id)`` pair *before* applying any effect; the
    composite primary key makes the second delivery fail loudly and harmlessly,
    so the effect happens exactly once even though delivery did not.
    """

    __tablename__ = "consumer_inbox"

    consumer_name: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_id: Mapped[str] = mapped_column(CHAR(ULID_LENGTH), primary_key=True)
    processed_at: Mapped[datetime] = created_at_column()
