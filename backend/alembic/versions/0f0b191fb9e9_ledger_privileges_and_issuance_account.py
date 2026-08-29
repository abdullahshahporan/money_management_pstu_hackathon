"""ledger privileges and issuance account

Hand-written on purpose. Autogenerate compares *table structure*; it cannot see
privileges or seed rows, so regenerating the initial migration would silently
drop both. Keeping them in their own revision makes them safe from that.

Revision ID: 0f0b191fb9e9
Revises: 0f0b191fb9e8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0f0b191fb9e9"
down_revision: str | None = "0f0b191fb9e8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

APP_ROLE = "mm_app"
READONLY_ROLE = "mm_readonly"

# Spec 9.4: "Revoke UPDATE and DELETE on posted ledger tables from the
# application role." These three tables are the financial and forensic record.
APPEND_ONLY_TABLES = ("ledger_entries", "ledger_transactions", "audit_logs")

SYSTEM_ISSUANCE_ACCOUNT_ID = "00000000000000000000000000"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Baseline grants for the runtime and reporting roles.
    # ------------------------------------------------------------------
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"
    )
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READONLY_ROLE}")

    # ------------------------------------------------------------------
    # Append-only enforcement.
    #
    # This is what turns "the ledger is immutable" from a coding convention
    # into a guarantee the application cannot violate even by accident. The
    # runtime role may append history and read it, but PostgreSQL will refuse
    # any attempt to rewrite or erase it - including from a SQL injection that
    # reaches the database with the application's own credentials.
    #
    # Corrections are made only by posting a compensating REVERSAL transaction,
    # never by editing what was already posted.
    # ------------------------------------------------------------------
    for table in APPEND_ONLY_TABLES:
        op.execute(f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}")

    # ------------------------------------------------------------------
    # The system issuance account (spec 8.3).
    #
    # Registration never sets a balance directly. It posts a balanced ledger
    # transaction debiting this account and crediting the new user, so the
    # opening BDT 100,000 is itself auditable and reconstructible. This
    # account's balance is the exact negative of all money in circulation,
    # which is what makes "no money was created or destroyed" a single SUM
    # over the ledger rather than an act of faith.
    # ------------------------------------------------------------------
    op.execute(
        sa.text(
            """
            INSERT INTO accounts (
                id, user_id, account_type, account_number, currency,
                balance_minor, status, version, created_at, updated_at
            ) VALUES (
                :account_id, NULL, 'SYSTEM_ISSUANCE',
                'SYS-ISSUANCE-001', 'BDT', 0, 'ACTIVE', 0, now(), now()
            )
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(account_id=SYSTEM_ISSUANCE_ACCOUNT_ID)
    )


def downgrade() -> None:
    # This will fail if any transfer still references the issuance account,
    # because the foreign key is ON DELETE RESTRICT. That refusal is correct:
    # tearing the issuance account out from under existing financial history
    # would orphan the ledger. Downgrading a database that holds real
    # transfers requires deliberately removing that history first.
    op.execute(
        sa.text("DELETE FROM accounts WHERE id = :account_id").bindparams(
            account_id=SYSTEM_ISSUANCE_ACCOUNT_ID
        )
    )
    for table in APPEND_ONLY_TABLES:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_ROLE}")
