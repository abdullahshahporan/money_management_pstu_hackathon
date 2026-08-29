"""Reassert append-only privileges for forensic tables.

The application already writes audit rows in the same transaction as each
money mutation.  A deployed database was found with UPDATE and DELETE granted
back to the runtime role, however, so the audit trail was only append-only by
convention.  This revision restores the database-enforced boundary and makes
the intended privilege state explicit at the current migration head.

Revision ID: c3f8a2d91b74
Revises: f41a9f26c001
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c3f8a2d91b74"
down_revision: str | None = "f41a9f26c001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

APP_ROLE = "mm_app"
APPEND_ONLY_TABLES = ("ledger_entries", "ledger_transactions", "audit_logs")


def upgrade() -> None:
    # Runtime code must be able to append and inspect history, but PostgreSQL
    # itself must reject any attempt to rewrite or erase forensic evidence.
    for table in APPEND_ONLY_TABLES:
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {APP_ROLE}")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {table} FROM {APP_ROLE}")


def downgrade() -> None:
    # Restore the broad runtime privilege state that existed immediately
    # before this corrective migration.
    for table in APPEND_ONLY_TABLES:
        op.execute(f"GRANT UPDATE, DELETE ON {table} TO {APP_ROLE}")
