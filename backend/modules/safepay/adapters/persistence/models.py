"""Conditional safe-pay escrow.

The buyer's money leaves their account the moment the escrow is created and
sits in the system ESCROW account. That is what makes the protection real for
both sides: the seller can see the money is committed, and the buyer cannot
spend it out from under the deal.

Neither party can unilaterally take it. Release needs the buyer's delivery
code, or a delivery event, or the auto-release timer running out.
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
    Text,
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

ESCROW_STATUSES = (
    "AWAITING_SHIPMENT",
    "SHIPPED",
    "DELIVERED",
    "RELEASED",
    "REFUNDED",
    "DISPUTED",
    "CANCELLED",
)

# Six digits, not four. With a five-attempt cap a 4-digit code gives an
# attacker a 1-in-2000 chance per escrow - tolerable alone, but a seller
# running many escrows gets many independent attempts, and the expected number
# of successful guesses grows linearly with volume. Six digits costs the buyer
# two extra keypresses and drops it to 1-in-200,000.
DELIVERY_CODE_DIGITS = 6
DELIVERY_CODE_MAX_ATTEMPTS = 5


class SafePayEscrowRecord(Base):
    __tablename__ = "safepay_escrows"

    id: Mapped[UlidPk]
    reference: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)

    # The hold leg. Its ledger posting is what actually moved the money into
    # escrow, so this link is the audit trail for "where is the money now".
    hold_transfer_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    # The release or refund leg, once it exists. UNIQUE, so an escrow can
    # settle exactly once no matter how many release paths race.
    settlement_transfer_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH),
        ForeignKey("transfers.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )

    buyer_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    seller_user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    amount_minor: Mapped[MoneyMinor] = mapped_column(nullable=False)
    currency: Mapped[CurrencyCode] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)

    status: Mapped[StatusText] = mapped_column(
        nullable=False,
        default="AWAITING_SHIPMENT",
        server_default=text("'AWAITING_SHIPMENT'"),
    )

    # The delivery code is stored only as an Argon2 hash. It is a bearer
    # credential that releases money, so a database disclosure must not hand
    # an attacker the ability to drain every open escrow.
    delivery_code_hash: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_code_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    delivery_code_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tracking_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    courier_slug: Mapped[str | None] = mapped_column(String(32), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set when delivery is observed, not when the escrow is created - the
    # window is "buyer had N hours to object after delivery", not "N hours
    # after purchase".
    auto_release_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dispute_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    disputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_user_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        CheckConstraint("buyer_user_id <> seller_user_id", name="buyer_differs_from_seller"),
        CheckConstraint(
            "status IN ('AWAITING_SHIPMENT', 'SHIPPED', 'DELIVERED', 'RELEASED', "
            "'REFUNDED', 'DISPUTED', 'CANCELLED')",
            name="status_valid",
        ),
        CheckConstraint("delivery_code_attempts >= 0", name="attempts_non_negative"),
        # A settled escrow must name the leg that settled it, and an unsettled
        # one must not. Keeps "is this money still in escrow" unambiguous.
        CheckConstraint(
            "(status IN ('RELEASED', 'REFUNDED') AND settlement_transfer_id IS NOT NULL) OR "
            "(status NOT IN ('RELEASED', 'REFUNDED') AND settlement_transfer_id IS NULL)",
            name="settled_has_settlement_leg",
        ),
        Index("ix_safepay_buyer", "buyer_user_id", "status", "created_at"),
        Index("ix_safepay_seller", "seller_user_id", "status", "created_at"),
    )
