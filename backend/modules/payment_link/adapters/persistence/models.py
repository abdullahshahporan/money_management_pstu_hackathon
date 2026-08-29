"""Shareable payment links.

A link lets someone be paid without handing out their phone number. It is
worth being precise about what that does and does not hide, because
"anonymous" overstates it:

*   The payer never learns the payee's phone number or user id. They see only
    whatever alias the payee chose.
*   The payee *does* learn who paid them - the payment is an ordinary transfer
    from a real account, and it appears in both statements.
*   The platform sees everything. It has to: the ledger is the audit trail,
    and a payment system that could not attribute its own transfers would be
    unauditable.

So this is unlinkability of the payee's *contact details* from the payer, not
anonymity from the counterparty or from the operator.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
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

LINK_STATUSES = ("ACTIVE", "CONSUMED", "EXPIRED", "REVOKED")

# 32 bytes from secrets.token_urlsafe. Deliberately NOT a ULID: ULIDs embed a
# timestamp and are lexicographically ordered, so knowing one makes nearby
# ones guessable. A link is a bearer credential for receiving money and must
# be unguessable.
LINK_TOKEN_BYTES = 32


class PaymentLinkRecord(Base):
    __tablename__ = "payment_links"

    id: Mapped[UlidPk]

    # Only the hash is stored. The raw token exists once, in the response that
    # created it. A database disclosure therefore does not reveal live links.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    payee_account_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False
    )
    payee_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # What the payer is shown instead of a real name or number.
    alias: Mapped[ShortText] = mapped_column(nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)
    # Fixed-amount link when set; otherwise the payer chooses, up to the cap.
    fixed_amount_minor: Mapped[MoneyMinor | None] = mapped_column(nullable=True)
    # A hard ceiling per payment. Present even on open links, so a link can
    # never be used to move an unbounded sum.
    max_amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)

    max_uses: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    uses_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    single_use: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("max_amount_minor > 0", name="max_amount_positive"),
        CheckConstraint(
            "fixed_amount_minor IS NULL OR "
            "(fixed_amount_minor > 0 AND fixed_amount_minor <= max_amount_minor)",
            name="fixed_amount_within_cap",
        ),
        CheckConstraint("max_uses >= 1", name="max_uses_at_least_one"),
        # The use counter can never exceed the cap. This is the database-level
        # backstop behind the conditional UPDATE that increments it, so even a
        # logic bug cannot over-spend a single-use link.
        CheckConstraint("uses_count >= 0 AND uses_count <= max_uses", name="uses_within_cap"),
        CheckConstraint(
            "status IN ('ACTIVE', 'CONSUMED', 'EXPIRED', 'REVOKED')", name="status_valid"
        ),
        Index("ix_payment_links_payee", "payee_user_id", "status", "created_at"),
        Index(
            "ix_payment_links_active_expiry",
            "expires_at",
            postgresql_where="status = 'ACTIVE'",
        ),
    )


class PaymentLinkPaymentRecord(Base):
    """One payment made through a link."""

    __tablename__ = "payment_link_payments"

    id: Mapped[UlidPk]
    link_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("payment_links.id", ondelete="RESTRICT"), nullable=False
    )
    # UNIQUE: one transfer can only ever be attributed to one link use.
    transfer_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    payer_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        Index("ix_link_payments_link", "link_id", "created_at"),
    )
