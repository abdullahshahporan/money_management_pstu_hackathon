"""The deferred-settlement queue.

Three features move money on a timer: the 10-second undo window, the safe-pay
auto-release, and payment-link expiry. All three go through this one table.

Why a table and not a timer. An in-process ``threading.Timer`` or
``asyncio.sleep`` would be wrong here for two independent reasons, and both
lose money:

*   **It dies with the process.** A transfer sitting in its undo window when
    the container restarts would never settle. The money would sit in the
    holding account forever, debited from the sender and never credited to the
    receiver.
*   **It runs on every replica.** With two API replicas, whichever one served
    the request owns the timer - so a deploy that drains that replica silently
    drops the settlement, while a naive "every replica checks" design would
    settle the same transfer twice.

Durable rows plus a polling worker fix both: the work survives a crash because
it is committed, and ``FOR UPDATE SKIP LOCKED`` lets any number of workers
claim disjoint batches. This is exactly the pattern the outbox relay already
uses (``apps/outbox_relay/main.py``), for exactly the same reasons.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from platform_.database.base import Base
from platform_.database.types import ULID_LENGTH, StatusText, UlidPk, created_at_column

TASK_TYPES = (
    "UNDO_SETTLE",
    "ESCROW_AUTO_RELEASE",
    "LINK_EXPIRY",
    "MONEY_REQUEST_EXPIRY",
)
TASK_STATUSES = ("PENDING", "DONE", "CANCELLED", "FAILED")


class ScheduledTaskRecord(Base):
    """One future action against one resource."""

    __tablename__ = "scheduled_tasks"

    id: Mapped[UlidPk]
    task_type: Mapped[StatusText] = mapped_column(nullable=False)
    resource_id: Mapped[str] = mapped_column(CHAR(ULID_LENGTH), nullable=False)

    # Always compared against the database's own now(). An earlier bug in the
    # outbox relay seeded a due time from the application clock, and whenever
    # the app server ran even slightly ahead of the database the work became
    # permanently ineligible. Due times are set by the database, and read by
    # the database.
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('UNDO_SETTLE', 'ESCROW_AUTO_RELEASE', 'LINK_EXPIRY', "
            "'MONEY_REQUEST_EXPIRY')",
            name="task_type_valid",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'DONE', 'CANCELLED', 'FAILED')", name="status_valid"
        ),
        CheckConstraint("attempt_count >= 0", name="attempt_count_non_negative"),
        # One live task per resource. Tapping Send twice cannot queue two
        # settlements for the same transfer.
        UniqueConstraint("task_type", "resource_id", name="one_task_per_resource"),
        # Partial index: the worker only ever scans due, pending rows, so
        # indexing completed ones would cost write throughput and grow forever.
        Index(
            "ix_scheduled_tasks_due",
            "due_at",
            postgresql_where="status = 'PENDING'",
        ),
    )
