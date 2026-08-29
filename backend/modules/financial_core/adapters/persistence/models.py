"""Financial core tables: accounts, transfers, the double-entry ledger, idempotency.

These tables carry the money. Every rule that matters is enforced twice
(spec 9.4): the domain layer rejects it with a helpful error, and a database
constraint refuses to persist it even if the application has a bug. The
constraints below are the second line, and they are the one that cannot be
bypassed by a code path we forgot about.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from modules.financial_core.domain.account import (
    SYSTEM_ISSUANCE_ACCOUNT_ID as _SYSTEM_ISSUANCE_ACCOUNT_ID,
)
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

# Re-exported for convenience; defined in the domain, which is where the rule
# that "money is issued from this account" actually belongs.
SYSTEM_ISSUANCE_ACCOUNT_ID = _SYSTEM_ISSUANCE_ACCOUNT_ID

ACCOUNT_TYPES = (
    "USER",
    "SYSTEM_ISSUANCE",
    "PENDING_SETTLEMENT",
    "ESCROW",
    "GROUP",
    "OVERDRAFT_POOL",
)

# Multi-stage money movement is modelled as TWO transfer rows - a hold and a
# release/refund - linked by parent_transfer_id, rather than one row with two
# ledger transactions. ledger_transactions.transfer_id is UNIQUE, so one row
# can only ever own one posting; and the two-row model keeps the statement
# honest, because each party naturally sees exactly one entry (the payer at
# hold time, the payee at release time).
TRANSFER_KINDS = (
    "P2P_SEND",
    "REQUEST_SETTLEMENT",
    "SIGNUP_GRANT",
    "REVERSAL",
    # 10-second undo window
    "UNDO_HOLD",
    "UNDO_SETTLE",
    "UNDO_REFUND",
    # conditional safe-pay escrow
    "ESCROW_HOLD",
    "ESCROW_RELEASE",
    "ESCROW_REFUND",
    # shared group wallets
    "GROUP_DEPOSIT",
    "GROUP_WITHDRAWAL",
    # anonymous payment links
    "LINK_PAYMENT",
    # community overdraft
    "OVERDRAFT_DRAW",
    "OVERDRAFT_REPAY",
    "OVERDRAFT_FUND",
)

TRANSFER_STATUSES = (
    "SUCCEEDED",
    "FAILED",
    "REVERSED",
    # money is in a holding account awaiting a decision or a timer
    "PENDING_UNDO",
    "HELD",
    "DISPUTED",
    "REFUNDED",
)
IDEMPOTENCY_STATUSES = ("PROCESSING", "COMPLETED")


class AccountRecord(Base):
    """An account holding an authoritative balance.

    ``balance_minor`` is a *materialised* balance, not the source of truth
    (spec 8.4). The ledger is the truth; this column exists so the home screen
    does not have to sum a user's entire history. Both are written in the same
    transaction, and reconciliation proves they still agree.
    """

    __tablename__ = "accounts"

    id: Mapped[UlidPk]
    # NULL for system accounts; UNIQUE guarantees one account per user.
    # NULL for system and group accounts. Uniqueness is (user_id, account_type)
    # rather than user_id alone, so one user can hold both their main account
    # and, say, an overdraft pool.
    user_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
    )
    account_type: Mapped[StatusText] = mapped_column(
        nullable=False, default="USER", server_default=text("'USER'")
    )
    account_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    currency: Mapped[CurrencyCode] = mapped_column(
        nullable=False, default="BDT", server_default=text("'BDT'")
    )
    balance_minor: Mapped[MoneyMinor] = mapped_column(
        nullable=False, default=0, server_default=text("0")
    )
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )

    # Optimistic-concurrency counter. The transfer path uses pessimistic row
    # locks, but this makes any lost update detectable after the fact.
    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        # Invariant 6 (spec 8.2): a user balance can never go negative. The
        # issuance account is deliberately exempt - issuing money into a closed
        # system is exactly what makes its balance negative.
        # Invariant 6 (spec 8.2). Only the issuance account may go negative.
        # Holding accounts are hard floored at zero: they can only pay out what
        # was paid in, so a negative balance would mean value from nowhere.
        CheckConstraint(
            "account_type = 'SYSTEM_ISSUANCE' OR balance_minor >= 0",
            name="user_balance_non_negative",
        ),
        CheckConstraint(
            "account_type IN ('USER', 'SYSTEM_ISSUANCE', 'PENDING_SETTLEMENT', "
            "'ESCROW', 'GROUP', 'OVERDRAFT_POOL')",
            name="account_type_valid",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'FROZEN', 'CLOSED')", name="status_valid"
        ),
        # Ownership must match the type: user-owned accounts have an owner,
        # system and group accounts do not.
        CheckConstraint(
            "(account_type IN ('USER', 'OVERDRAFT_POOL') AND user_id IS NOT NULL) OR "
            "(account_type IN ('SYSTEM_ISSUANCE', 'ESCROW', 'PENDING_SETTLEMENT', "
            "'GROUP') AND user_id IS NULL)",
            name="ownership_matches_type",
        ),
        UniqueConstraint("user_id", "account_type", name="one_account_per_user_per_type"),
    )


class TransferRecord(Base):
    """The intent and outcome of moving value between two accounts."""

    __tablename__ = "transfers"

    id: Mapped[UlidPk]
    reference: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)

    sender_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    receiver_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )

    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)

    kind: Mapped[StatusText] = mapped_column(nullable=False)
    status: Mapped[StatusText] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Who caused this, and under which API request - for audit and tracing.
    initiated_by_user_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reversal_of_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("transfers.id", ondelete="RESTRICT"), nullable=True
    )
    # Links a release/refund leg back to the hold it settles. The hold is the
    # transfer the user initiated and the one whose reference they are shown;
    # the second leg is the internal completion.
    parent_transfer_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("transfers.id", ondelete="RESTRICT"), nullable=True
    )
    # On a hold, the receiver_account_id is the holding account - that is where
    # the money actually went. This records where it is *meant* to end up, so
    # the settlement leg knows who to pay without a side table.
    intended_receiver_account_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=True
    )

    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        # Invariant 3 (spec 8.2). Enforced here so no code path can ever
        # produce a self-transfer, which would silently pass reconciliation.
        CheckConstraint(
            "sender_account_id <> receiver_account_id", name="sender_differs_from_receiver"
        ),
        CheckConstraint(
            "kind IN ('P2P_SEND', 'REQUEST_SETTLEMENT', 'SIGNUP_GRANT', 'REVERSAL',"
            " 'UNDO_HOLD', 'UNDO_SETTLE', 'UNDO_REFUND',"
            " 'ESCROW_HOLD', 'ESCROW_RELEASE', 'ESCROW_REFUND',"
            " 'GROUP_DEPOSIT', 'GROUP_WITHDRAWAL', 'LINK_PAYMENT',"
            " 'OVERDRAFT_DRAW', 'OVERDRAFT_REPAY', 'OVERDRAFT_FUND')",
            name="kind_valid",
        ),
        CheckConstraint(
            "status IN ('SUCCEEDED', 'FAILED', 'REVERSED', 'PENDING_UNDO', "
            "'HELD', 'DISPUTED', 'REFUNDED')",
            name="status_valid",
        ),
        # A settlement leg must say which hold it settles, and a hold must not
        # claim a parent. Stops the two-stage model degrading into a tangle.
        CheckConstraint(
            "(kind IN ('UNDO_SETTLE', 'UNDO_REFUND', 'ESCROW_RELEASE', "
            "'ESCROW_REFUND') AND parent_transfer_id IS NOT NULL) OR "
            "(kind NOT IN ('UNDO_SETTLE', 'UNDO_REFUND', 'ESCROW_RELEASE', "
            "'ESCROW_REFUND') AND parent_transfer_id IS NULL)",
            name="settlement_leg_has_parent",
        ),
        # Spec 9.3: history is always queried per account, newest first.
        Index("ix_transfers_sender_created", "sender_account_id", "created_at"),
        Index("ix_transfers_receiver_created", "receiver_account_id", "created_at"),
    )


class LedgerTransactionRecord(Base):
    """An immutable accounting event. Its entries must sum to zero."""

    __tablename__ = "ledger_transactions"

    id: Mapped[UlidPk]
    transfer_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    type: Mapped[StatusText] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime] = created_at_column()


class LedgerEntryRecord(Base):
    """A signed change to one account. Append-only, forever.

    ``amount_minor`` is signed: negative debits the account, positive credits
    it. A single signed column - rather than a direction flag plus a magnitude -
    makes the zero-sum invariant a plain ``SUM(amount_minor) = 0``, which is
    cheap enough to run across the whole table during reconciliation.

    The runtime database role has UPDATE and DELETE revoked on this table
    (see the initial migration), so append-only is enforced by PostgreSQL
    privileges rather than by application discipline.
    """

    __tablename__ = "ledger_entries"

    id: Mapped[UlidPk]
    ledger_transaction_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )

    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)

    # The account balance immediately after this entry was applied. Lets a
    # statement show a running balance without re-summing from the beginning,
    # and gives reconciliation a second, independent way to detect drift.
    balance_after_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        # A zero entry is meaningless and would pollute the audit trail.
        CheckConstraint("amount_minor <> 0", name="amount_non_zero"),
        # Spec 9.3: the account statement query, and the pagination cursor.
        Index("ix_ledger_entries_account_id_desc", "account_id", "id"),
        Index("ix_ledger_entries_txn", "ledger_transaction_id"),
    )


class IdempotencyRecord(Base):
    """Durable exactly-once guarantee for money mutations (spec 12).

    The primary key is the natural business key ``(actor, endpoint, key)``, so
    uniqueness is enforced by PostgreSQL rather than by a check-then-insert
    race. ``request_fingerprint`` binds the key to the exact payload it was
    first used with: reusing a key with different content is a client bug and
    is rejected rather than silently replayed.
    """

    __tablename__ = "idempotency_records"

    actor_id: Mapped[str] = mapped_column(CHAR(ULID_LENGTH), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(100), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)

    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="PROCESSING", server_default=text("'PROCESSING'")
    )

    resource_id: Mapped[str | None] = mapped_column(CHAR(ULID_LENGTH), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PROCESSING', 'COMPLETED')", name="status_valid"
        ),
        # A completed record must carry the response it will replay.
        CheckConstraint(
            "status <> 'COMPLETED' OR (http_status IS NOT NULL AND response_body IS NOT NULL)",
            name="completed_has_response",
        ),
    )
