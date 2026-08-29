"""Money request tables.

A money request is a *workflow*, not money. It never mutates a balance itself;
accepting one invokes the financial core's transfer use case, which is the only
code path that moves value (spec 6.2).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from platform_.database.base import Base
from platform_.database.types import (
    ULID_LENGTH,
    CurrencyCode,
    MoneyMinor,
    StatusText,
    UlidPk,
    created_at_column,
    updated_at_column,
)

# Spec 8.5. PENDING is the only non-terminal state; every other state is final,
# which is what makes the conditional UPDATE in the accept path a safe latch.
MONEY_REQUEST_STATUSES = ("PENDING", "ACCEPTED", "REJECTED", "CANCELLED", "EXPIRED")
TERMINAL_STATUSES = ("ACCEPTED", "REJECTED", "CANCELLED", "EXPIRED")


class MoneyRequestRecord(Base):
    """One party asking another to pay."""

    __tablename__ = "money_requests"

    id: Mapped[UlidPk]
    reference: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)

    # The account that will *receive* the money if this is accepted.
    requester_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    # The account that will *pay*. Only this account's owner may accept.
    payer_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )

    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="PENDING", server_default=text("'PENDING'")
    )

    # Invariant 13 (spec 8.2): an accepted request causes at most one transfer.
    # UNIQUE turns that from a hope into a database guarantee.
    transfer_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "requester_account_id <> payer_account_id", name="requester_differs_from_payer"
        ),
        CheckConstraint(
            "status IN ('PENDING', 'ACCEPTED', 'REJECTED', 'CANCELLED', 'EXPIRED')",
            name="status_valid",
        ),
        # Only an accepted request may carry a transfer, and it must carry one.
        CheckConstraint(
            "(status = 'ACCEPTED' AND transfer_id IS NOT NULL) OR "
            "(status <> 'ACCEPTED' AND transfer_id IS NULL)",
            name="transfer_only_when_accepted",
        ),
        # Spec 9.3: the inbox and outbox list queries.
        Index("ix_money_requests_payer", "payer_account_id", "status", "created_at"),
        Index("ix_money_requests_requester", "requester_account_id", "status", "created_at"),
        # Sweeping expired requests touches only PENDING rows.
        Index(
            "ix_money_requests_pending_expiry",
            "expires_at",
            postgresql_where="status = 'PENDING'",
        ),
    )
