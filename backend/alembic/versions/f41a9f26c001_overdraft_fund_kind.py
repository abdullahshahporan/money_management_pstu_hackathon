"""allow explicit Spot-Me pool funding transfers

Revision ID: f41a9f26c001
Revises: 8ddea5c31c8a
Create Date: 2026-08-29
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f41a9f26c001"
down_revision: str | None = "8ddea5c31c8a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


KINDS_WITH_FUND = (
    "kind IN ('P2P_SEND', 'REQUEST_SETTLEMENT', 'SIGNUP_GRANT', 'REVERSAL',"
    " 'UNDO_HOLD', 'UNDO_SETTLE', 'UNDO_REFUND',"
    " 'ESCROW_HOLD', 'ESCROW_RELEASE', 'ESCROW_REFUND',"
    " 'GROUP_DEPOSIT', 'GROUP_WITHDRAWAL', 'LINK_PAYMENT',"
    " 'OVERDRAFT_DRAW', 'OVERDRAFT_REPAY', 'OVERDRAFT_FUND')"
)

KINDS_WITHOUT_FUND = KINDS_WITH_FUND.replace(", 'OVERDRAFT_FUND'", "")


def upgrade() -> None:
    op.drop_constraint(op.f("ck_transfers_kind_valid"), "transfers", type_="check")
    op.create_check_constraint("kind_valid", "transfers", KINDS_WITH_FUND)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_transfers_kind_valid"), "transfers", type_="check")
    op.create_check_constraint("kind_valid", "transfers", KINDS_WITHOUT_FUND)
