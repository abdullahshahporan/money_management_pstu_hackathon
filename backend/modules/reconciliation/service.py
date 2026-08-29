"""Independent verification that the books are correct (spec 24.5).

Every check here recomputes a property from the ledger and compares it against
what the system believes. Nothing in this module trusts the application: it
reads the raw tables and does the arithmetic again.

That independence is the point. The transfer path already enforces these
invariants, so if reconciliation ever reports a non-zero count it means the
enforcement has a hole - which is exactly what we want to find out from a
report rather than from a customer.

Every counter must read zero. There is no acceptable non-zero value.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["ReconciliationReport", "ReconciliationService"]


@dataclass
class ReconciliationReport:
    unbalanced_ledger_transactions: int = 0
    balance_mismatches: int = 0
    negative_user_accounts: int = 0
    succeeded_transfers_without_ledger: int = 0
    transfers_with_wrong_entry_count: int = 0
    accepted_requests_without_transfer: int = 0
    duplicate_transfer_references: int = 0

    system_wide_ledger_sum_minor: int = 0
    total_user_balance_minor: int = 0
    issuance_balance_minor: int = 0
    money_in_circulation_minor: int = 0
    # Money that has left a payer but not yet reached a payee: sitting in
    # escrow, in an undo window, in a group wallet, or in an overdraft pool.
    # It is still fully accounted for - it is just not in a USER account.
    total_held_minor: int = 0
    balance_by_account_type: dict[str, int] = field(default_factory=dict)

    accounts_checked: int = 0
    ledger_entries_checked: int = 0
    transfers_checked: int = 0

    mismatch_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_balanced(self) -> bool:
        """True only if every integrity counter is zero."""
        return (
            self.unbalanced_ledger_transactions == 0
            and self.balance_mismatches == 0
            and self.negative_user_accounts == 0
            and self.succeeded_transfers_without_ledger == 0
            and self.transfers_with_wrong_entry_count == 0
            and self.accepted_requests_without_transfer == 0
            and self.duplicate_transfer_references == 0
            and self.system_wide_ledger_sum_minor == 0
            # The closed-ecosystem identity. Every account balance, of every
            # type, must sum to exactly zero: the issuance account is negative
            # by precisely the amount everyone else holds between them.
            #
            # This used to be stated as `issuance + user_balances == 0`, which
            # was equivalent only while USER accounts were the sole holders of
            # money. Once value can rest in escrow, an undo window, a group
            # wallet or an overdraft pool, that form silently under-counts and
            # reports drift that is not there.
            and self.all_account_balance_sum_minor == 0
        )

    @property
    def all_account_balance_sum_minor(self) -> int:
        return sum(self.balance_by_account_type.values())

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["balanced"] = self.is_balanced
        return data


class ReconciliationService:
    """Runs the integrity checks. Read-only by construction."""

    def run(self, session: Session, *, include_details: bool = True) -> ReconciliationReport:
        report = ReconciliationReport()

        # 1. Every posted ledger transaction sums to zero (invariant 9).
        report.unbalanced_ledger_transactions = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT ledger_transaction_id FROM ledger_entries"
                    "  GROUP BY ledger_transaction_id HAVING SUM(amount_minor) <> 0"
                    ") AS unbalanced"
                )
            )
            or 0
        )

        # 2. Materialised balance equals the sum of the account's entries
        #    (invariant 12). This is the check that catches a balance updated
        #    without a matching ledger posting - the single most dangerous
        #    class of bug in a system like this.
        mismatch_rows = session.execute(
            text(
                "SELECT a.id, a.account_number, a.balance_minor,"
                "       COALESCE(SUM(e.amount_minor), 0) AS derived_minor "
                "FROM accounts a "
                "LEFT JOIN ledger_entries e ON e.account_id = a.id "
                "GROUP BY a.id, a.account_number, a.balance_minor "
                "HAVING a.balance_minor <> COALESCE(SUM(e.amount_minor), 0)"
            )
        ).all()
        report.balance_mismatches = len(mismatch_rows)
        if include_details:
            report.mismatch_details = [
                {
                    "accountId": row[0],
                    "accountNumber": row[1],
                    "recordedMinor": int(row[2]),
                    "derivedMinor": int(row[3]),
                    "driftMinor": int(row[2]) - int(row[3]),
                }
                for row in mismatch_rows
            ]

        # 3. No user account is negative (invariant 6).
        report.negative_user_accounts = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM accounts "
                    "WHERE account_type = 'USER' AND balance_minor < 0"
                )
            )
            or 0
        )

        # 4. Every succeeded transfer has exactly one ledger transaction.
        report.succeeded_transfers_without_ledger = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM transfers t "
                    "LEFT JOIN ledger_transactions lt ON lt.transfer_id = t.id "
                    "WHERE t.status = 'SUCCEEDED' AND lt.id IS NULL"
                )
            )
            or 0
        )

        # 5. Every simple transfer has exactly two entries (invariant 8).
        report.transfers_with_wrong_entry_count = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT lt.id FROM ledger_transactions lt "
                    "  JOIN ledger_entries e ON e.ledger_transaction_id = lt.id "
                    "  GROUP BY lt.id HAVING count(*) <> 2"
                    ") AS wrong"
                )
            )
            or 0
        )

        # 6. Every accepted money request settled through exactly one transfer
        #    (invariant 13).
        report.accepted_requests_without_transfer = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM money_requests "
                    "WHERE status = 'ACCEPTED' AND transfer_id IS NULL"
                )
            )
            or 0
        )

        # 7. Transfer references are unique. Enforced by a UNIQUE constraint;
        #    verified here so the report stands alone as evidence.
        report.duplicate_transfer_references = int(
            session.scalar(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT reference FROM transfers"
                    "  GROUP BY reference HAVING count(*) > 1"
                    ") AS dupes"
                )
            )
            or 0
        )

        # 8. Closed-ecosystem totals.
        report.system_wide_ledger_sum_minor = int(
            session.scalar(
                text("SELECT COALESCE(SUM(amount_minor), 0) FROM ledger_entries")
            )
            or 0
        )
        report.total_user_balance_minor = int(
            session.scalar(
                text(
                    "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts "
                    "WHERE account_type = 'USER'"
                )
            )
            or 0
        )
        report.issuance_balance_minor = int(
            session.scalar(
                text(
                    "SELECT COALESCE(SUM(balance_minor), 0) FROM accounts "
                    "WHERE account_type = 'SYSTEM_ISSUANCE'"
                )
            )
            or 0
        )
        report.money_in_circulation_minor = -report.issuance_balance_minor

        # Per-type breakdown. This is what makes "where is the money right
        # now" answerable, and it is what the identity above is computed from.
        report.balance_by_account_type = {
            str(row[0]): int(row[1])
            for row in session.execute(
                text(
                    "SELECT account_type, COALESCE(SUM(balance_minor), 0) "
                    "FROM accounts GROUP BY account_type"
                )
            ).all()
        }
        report.total_held_minor = sum(
            amount
            for account_type, amount in report.balance_by_account_type.items()
            if account_type not in ("USER", "SYSTEM_ISSUANCE")
        )

        report.accounts_checked = int(
            session.scalar(text("SELECT count(*) FROM accounts")) or 0
        )
        report.ledger_entries_checked = int(
            session.scalar(text("SELECT count(*) FROM ledger_entries")) or 0
        )
        report.transfers_checked = int(
            session.scalar(text("SELECT count(*) FROM transfers")) or 0
        )

        return report
