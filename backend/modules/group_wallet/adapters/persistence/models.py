"""Shared group wallets with quorum-approved withdrawals.

A group wallet is a real account on the ledger with `account_type = 'GROUP'`
and no owning user, so money in it is accounted for exactly like money
anywhere else. Deposits and withdrawals are ordinary transfers.

Per-member position is *derived*, never stored. Each member's net stake is
`sum of their deposits − sum of withdrawals paid out to them`, computed from
the transfers table. A stored running total would be a second source of truth
that could drift from the ledger, which is the one thing this system refuses
to allow.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from platform_.database.base import Base
from platform_.database.types import (
    ULID_LENGTH,
    CurrencyCode,
    MoneyMinor,
    ShortText,
    StatusText,
    UlidPk,
    created_at_column,
    updated_at_column,
)

WITHDRAWAL_STATUSES = ("PENDING", "APPROVED", "EXECUTED", "REJECTED", "CANCELLED", "EXPIRED")
MEMBER_ROLES = ("ADMIN", "MEMBER")


class GroupWalletRecord(Base):
    __tablename__ = "group_wallets"

    id: Mapped[UlidPk]
    name: Mapped[ShortText] = mapped_column(nullable=False)
    account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    created_by_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # How many distinct members must approve before money can leave.
    quorum_size: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("quorum_size >= 1", name="quorum_at_least_one"),
        CheckConstraint("status IN ('ACTIVE', 'CLOSED')", name="status_valid"),
    )


class GroupMemberRecord(Base):
    __tablename__ = "group_members"

    id: Mapped[UlidPk]
    group_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("group_wallets.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role: Mapped[StatusText] = mapped_column(
        nullable=False, default="MEMBER", server_default=text("'MEMBER'")
    )
    joined_at: Mapped[datetime] = created_at_column()
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("role IN ('ADMIN', 'MEMBER')", name="role_valid"),
        # One membership row per person per group. Rejoining reactivates the
        # existing row rather than creating a second one, so history is kept.
        UniqueConstraint("group_id", "user_id", name="one_membership_per_user"),
        Index("ix_group_members_user", "user_id", "left_at"),
    )


class GroupWithdrawalRequestRecord(Base):
    """A proposal to move money out of the group wallet."""

    __tablename__ = "group_withdrawal_requests"

    id: Mapped[UlidPk]
    reference: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    group_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("group_wallets.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    to_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    # UNIQUE: a request can produce at most one payout, however many approvals
    # arrive simultaneously.
    transfer_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'EXECUTED', 'REJECTED', 'CANCELLED', "
            "'EXPIRED')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'EXECUTED' AND transfer_id IS NOT NULL) OR "
            "(status <> 'EXECUTED' AND transfer_id IS NULL)",
            name="executed_has_transfer",
        ),
        Index("ix_group_withdrawals_group", "group_id", "status", "created_at"),
    )


class GroupWithdrawalApprovalRecord(Base):
    """One member's decision on one withdrawal request."""

    __tablename__ = "group_withdrawal_approvals"

    id: Mapped[UlidPk]
    request_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("group_withdrawal_requests.id", ondelete="CASCADE"),
        nullable=False,
    )
    approver_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decision: Mapped[StatusText] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("decision IN ('APPROVE', 'REJECT')", name="decision_valid"),
        # One vote per member per request. This is what makes the quorum count
        # trustworthy: a member cannot approve twice to reach quorum alone.
        UniqueConstraint("request_id", "approver_user_id", name="one_vote_per_member"),
    )
