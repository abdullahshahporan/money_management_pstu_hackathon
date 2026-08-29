"""Community "spot me" overdraft pools.

A sponsor pre-funds a pool from their own money. When a beneficiary is short
by a small amount, the pool covers exactly the shortfall so the payment
succeeds, and the borrower owes the pool.

**No money is created.** The pool is a real account holding real, already
issued taka. A draw is an ordinary sponsor-to-borrower transfer, and a
repayment is the reverse. The reconciliation identity is untouched - which is
worth stating plainly, because "overdraft" normally implies credit creation
and here it does not.

The lien on incoming funds is the lender's protection, and it is deliberately
*partial*: intercepting every taka of an incoming payment would mean a
borrower who receives their wages cannot buy food. See `LIEN_SWEEP_BASIS_POINTS`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    text,
)
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

LOAN_STATUSES = ("OUTSTANDING", "REPAID", "WRITTEN_OFF")

# The lien takes at most half of any incoming payment, so a borrower always
# keeps something from money that arrives. A 100% sweep is a genuinely harmful
# design: it turns any incoming payment - wages, a refund, emergency help from
# family - into nothing, which is exactly when someone can least afford it.
# Debt still clears, just never at the cost of leaving someone with zero.
LIEN_SWEEP_BASIS_POINTS = 5000

# A single draw is capped hard. This is a "you are 20 taka short" feature, not
# a lending product.
DEFAULT_MAX_DRAW_MINOR = 50_000  # BDT 500.00


class OverdraftPoolRecord(Base):
    """A sponsor's pre-funded liquidity pool."""

    __tablename__ = "overdraft_pools"

    id: Mapped[UlidPk]
    sponsor_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # The OVERDRAFT_POOL sub-account holding the money. Its non-negative CHECK
    # is the final backstop against over-drawing.
    pool_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')", name="status_valid"),
        UniqueConstraint("sponsor_user_id", name="one_pool_per_sponsor"),
    )


class OverdraftGrantRecord(Base):
    """Permission for one person to draw on one pool."""

    __tablename__ = "overdraft_grants"

    id: Mapped[UlidPk]
    pool_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("overdraft_pools.id", ondelete="CASCADE"), nullable=False
    )
    beneficiary_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    max_draw_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("max_draw_minor > 0", name="max_draw_positive"),
        CheckConstraint("status IN ('ACTIVE', 'REVOKED')", name="status_valid"),
        UniqueConstraint("pool_id", "beneficiary_user_id", name="one_grant_per_beneficiary"),
        Index("ix_overdraft_grants_beneficiary", "beneficiary_user_id", "status"),
    )


class OverdraftLoanRecord(Base):
    """An outstanding debt from a draw."""

    __tablename__ = "overdraft_loans"

    id: Mapped[UlidPk]
    pool_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("overdraft_pools.id", ondelete="RESTRICT"), nullable=False
    )
    borrower_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # UNIQUE: one loan per draw. If the enclosing transfer rolls back, both the
    # draw and this row vanish together - the borrower can never be left owing
    # money for a payment that never happened.
    draw_transfer_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    principal_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    outstanding_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)

    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="OUTSTANDING", server_default=text("'OUTSTANDING'")
    )
    created_at: Mapped[datetime] = created_at_column()
    repaid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("principal_minor > 0", name="principal_positive"),
        # Zero interest, and never more owed than was borrowed.
        CheckConstraint(
            "outstanding_minor >= 0 AND outstanding_minor <= principal_minor",
            name="outstanding_within_principal",
        ),
        CheckConstraint(
            "status IN ('OUTSTANDING', 'REPAID', 'WRITTEN_OFF')", name="status_valid"
        ),
        CheckConstraint(
            "(status = 'REPAID' AND outstanding_minor = 0) OR status <> 'REPAID'",
            name="repaid_means_zero",
        ),
        # The lien lookup: "does this user owe anything?" runs on every
        # incoming credit, so it must be an index hit.
        Index(
            "ix_overdraft_loans_borrower_outstanding",
            "borrower_user_id",
            postgresql_where="status = 'OUTSTANDING'",
        ),
    )


class OverdraftRepaymentRecord(Base):
    """One repayment applied to one loan."""

    __tablename__ = "overdraft_repayments"

    id: Mapped[UlidPk]
    loan_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("overdraft_loans.id", ondelete="RESTRICT"), nullable=False
    )
    transfer_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    # Which incoming transfer the lien swept, so a borrower can see exactly
    # why their balance changed.
    triggered_by_transfer_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("transfers.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        Index("ix_overdraft_repayments_loan", "loan_id", "created_at"),
    )
