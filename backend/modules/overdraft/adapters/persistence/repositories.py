"""PostgreSQL persistence for community overdraft pools and liens."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text, update
from sqlalchemy.orm import Session

from modules.financial_core.adapters.persistence.models import AccountRecord
from modules.overdraft.adapters.persistence.models import (
    OverdraftGrantRecord,
    OverdraftLoanRecord,
    OverdraftPoolRecord,
    OverdraftRepaymentRecord,
)
from platform_.kernel.ids import new_ulid

__all__ = ["SqlOverdraftRepository"]


class SqlOverdraftRepository:
    def get_pool_for_sponsor(
        self, session: Session, sponsor_user_id: str
    ) -> OverdraftPoolRecord | None:
        return session.scalars(
            select(OverdraftPoolRecord).where(
                OverdraftPoolRecord.sponsor_user_id == sponsor_user_id
            )
        ).one_or_none()

    def get_pool(self, session: Session, pool_id: str) -> OverdraftPoolRecord | None:
        return session.get(OverdraftPoolRecord, pool_id)

    def create_pool(
        self,
        session: Session,
        *,
        pool_id: str,
        sponsor_user_id: str,
        pool_account_id: str,
        currency: str,
        now: datetime,
    ) -> None:
        session.execute(
            insert(OverdraftPoolRecord).values(
                id=pool_id,
                sponsor_user_id=sponsor_user_id,
                pool_account_id=pool_account_id,
                currency=currency,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )

    def create_grant(
        self,
        session: Session,
        *,
        grant_id: str,
        pool_id: str,
        beneficiary_user_id: str,
        max_draw_minor: int,
        now: datetime,
    ) -> None:
        session.execute(
            insert(OverdraftGrantRecord).values(
                id=grant_id,
                pool_id=pool_id,
                beneficiary_user_id=beneficiary_user_id,
                max_draw_minor=max_draw_minor,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )

    def get_grant(
        self, session: Session, *, pool_id: str, beneficiary_user_id: str
    ) -> OverdraftGrantRecord | None:
        return session.scalars(
            select(OverdraftGrantRecord).where(
                OverdraftGrantRecord.pool_id == pool_id,
                OverdraftGrantRecord.beneficiary_user_id == beneficiary_user_id,
            )
        ).one_or_none()

    def active_grants_for_borrower(
        self, session: Session, borrower_user_id: str
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            select(
                OverdraftGrantRecord.id,
                OverdraftGrantRecord.max_draw_minor,
                OverdraftPoolRecord.id.label("pool_id"),
                OverdraftPoolRecord.sponsor_user_id,
                OverdraftPoolRecord.pool_account_id,
                AccountRecord.balance_minor,
            )
            .join(OverdraftPoolRecord, OverdraftPoolRecord.id == OverdraftGrantRecord.pool_id)
            .join(AccountRecord, AccountRecord.id == OverdraftPoolRecord.pool_account_id)
            .where(
                OverdraftGrantRecord.beneficiary_user_id == borrower_user_id,
                OverdraftGrantRecord.status == "ACTIVE",
                OverdraftPoolRecord.status == "ACTIVE",
                AccountRecord.status == "ACTIVE",
            )
            .order_by(OverdraftGrantRecord.created_at, OverdraftGrantRecord.id)
        ).mappings()
        return [dict(row) for row in rows]

    def create_loan(
        self,
        session: Session,
        *,
        pool_id: str,
        borrower_user_id: str,
        draw_transfer_id: str,
        amount_minor: int,
        currency: str,
        now: datetime,
    ) -> str:
        loan_id = new_ulid()
        session.execute(
            insert(OverdraftLoanRecord).values(
                id=loan_id,
                pool_id=pool_id,
                borrower_user_id=borrower_user_id,
                draw_transfer_id=draw_transfer_id,
                principal_minor=amount_minor,
                outstanding_minor=amount_minor,
                currency=currency,
                status="OUTSTANDING",
                created_at=now,
            )
        )
        return loan_id

    def outstanding_loans(
        self, session: Session, borrower_user_id: str, *, lock: bool = False
    ) -> list[dict[str, Any]]:
        statement = (
            select(
                OverdraftLoanRecord.id,
                OverdraftLoanRecord.pool_id,
                OverdraftLoanRecord.outstanding_minor,
                OverdraftLoanRecord.currency,
                OverdraftPoolRecord.sponsor_user_id,
                OverdraftPoolRecord.pool_account_id,
            )
            .join(OverdraftPoolRecord, OverdraftPoolRecord.id == OverdraftLoanRecord.pool_id)
            .where(
                OverdraftLoanRecord.borrower_user_id == borrower_user_id,
                OverdraftLoanRecord.status == "OUTSTANDING",
                OverdraftLoanRecord.outstanding_minor > 0,
            )
            .order_by(OverdraftLoanRecord.created_at, OverdraftLoanRecord.id)
        )
        if lock:
            statement = statement.with_for_update(of=OverdraftLoanRecord)
        return [dict(row) for row in session.execute(statement).mappings()]

    def apply_repayment(
        self,
        session: Session,
        *,
        loan_id: str,
        transfer_id: str,
        amount_minor: int,
        triggered_by_transfer_id: str,
        now: datetime,
    ) -> None:
        loan = session.get(OverdraftLoanRecord, loan_id)
        if loan is None or loan.status != "OUTSTANDING":
            return
        new_outstanding = max(0, loan.outstanding_minor - amount_minor)
        session.execute(
            update(OverdraftLoanRecord)
            .where(
                OverdraftLoanRecord.id == loan_id,
                OverdraftLoanRecord.status == "OUTSTANDING",
            )
            .values(
                outstanding_minor=new_outstanding,
                status="REPAID" if new_outstanding == 0 else "OUTSTANDING",
                repaid_at=now if new_outstanding == 0 else None,
            )
        )
        session.execute(
            insert(OverdraftRepaymentRecord).values(
                id=new_ulid(),
                loan_id=loan_id,
                transfer_id=transfer_id,
                amount_minor=amount_minor,
                triggered_by_transfer_id=triggered_by_transfer_id,
                created_at=now,
            )
        )

    def summary(self, session: Session, user_id: str) -> dict[str, Any]:
        sponsored = session.execute(
            text(
                """
                SELECT p.id, p.status, p.pool_account_id, a.balance_minor,
                       p.currency, p.created_at
                FROM overdraft_pools p
                JOIN accounts a ON a.id = p.pool_account_id
                WHERE p.sponsor_user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
        grants = session.execute(
            text(
                """
                SELECT g.id, g.pool_id, g.max_draw_minor, g.status,
                       u.display_name AS beneficiary_name, u.phone AS beneficiary_phone
                FROM overdraft_grants g
                JOIN overdraft_pools p ON p.id = g.pool_id
                JOIN users u ON u.id = g.beneficiary_user_id
                WHERE p.sponsor_user_id = :user_id
                ORDER BY g.created_at DESC
                """
            ),
            {"user_id": user_id},
        ).mappings()
        debts = session.execute(
            text(
                """
                SELECT l.id, l.pool_id, l.principal_minor, l.outstanding_minor,
                       l.currency, l.status, l.created_at,
                       u.display_name AS sponsor_name
                FROM overdraft_loans l
                JOIN overdraft_pools p ON p.id = l.pool_id
                JOIN users u ON u.id = p.sponsor_user_id
                WHERE l.borrower_user_id = :user_id
                ORDER BY l.created_at DESC
                """
            ),
            {"user_id": user_id},
        ).mappings()
        return {
            "sponsoredPool": (
                {
                    "poolId": sponsored["id"],
                    "status": sponsored["status"],
                    "balanceMinor": sponsored["balance_minor"],
                    "currency": sponsored["currency"].strip(),
                }
                if sponsored
                else None
            ),
            "grants": [
                {
                    "grantId": r["id"],
                    "poolId": r["pool_id"],
                    "beneficiaryName": r["beneficiary_name"],
                    "beneficiaryPhone": r["beneficiary_phone"],
                    "maxDrawMinor": r["max_draw_minor"],
                    "status": r["status"],
                }
                for r in grants
            ],
            "debts": [
                {
                    "loanId": r["id"],
                    "poolId": r["pool_id"],
                    "sponsorName": r["sponsor_name"],
                    "principalMinor": r["principal_minor"],
                    "outstandingMinor": r["outstanding_minor"],
                    "currency": r["currency"].strip(),
                    "status": r["status"],
                    "createdAt": r["created_at"].isoformat(),
                }
                for r in debts
            ],
        }
