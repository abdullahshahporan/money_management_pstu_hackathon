"""PostgreSQL persistence for money requests."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from modules.money_request.adapters.persistence.models import MoneyRequestRecord
from platform_.kernel.money import Money

__all__ = ["SqlMoneyRequestRepository"]


class SqlMoneyRequestRepository:
    def create(
        self,
        session: Session,
        *,
        request_id: str,
        reference: str,
        requester_account_id: str,
        payer_account_id: str,
        amount: Money,
        note: str | None,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        session.execute(
            insert(MoneyRequestRecord).values(
                id=request_id,
                reference=reference,
                requester_account_id=requester_account_id,
                payer_account_id=payer_account_id,
                amount_minor=amount.minor,
                currency=amount.currency.code,
                note=note,
                status="PENDING",
                expires_at=expires_at,
                created_at=now,
                updated_at=now,
            )
        )

    def get(self, session: Session, request_id: str) -> MoneyRequestRecord | None:
        return session.get(MoneyRequestRecord, request_id)

    def lock_for_update(
        self, session: Session, request_id: str
    ) -> MoneyRequestRecord | None:
        return session.scalars(
            select(MoneyRequestRecord)
            .where(MoneyRequestRecord.id == request_id)
            .with_for_update()
        ).one_or_none()

    def mark_accepted(
        self, session: Session, *, request_id: str, transfer_id: str, now: datetime
    ) -> int:
        """Latch PENDING -> ACCEPTED, attaching the transfer.

        The ``status = 'PENDING'`` predicate is the concurrency control
        (spec 13.2). Two payers tapping Accept at the same instant both reach
        here; exactly one updates a row. The loser sees rowcount 0 and is told
        the request was already handled, so a request can never settle twice.
        Returns the number of rows updated.
        """
        result = session.execute(
            update(MoneyRequestRecord)
            .where(
                MoneyRequestRecord.id == request_id,
                MoneyRequestRecord.status == "PENDING",
            )
            .values(
                status="ACCEPTED",
                transfer_id=transfer_id,
                responded_at=now,
                updated_at=now,
            )
        )
        return result.rowcount or 0

    def mark_terminal(
        self, session: Session, *, request_id: str, status: str, now: datetime
    ) -> int:
        """Latch PENDING -> REJECTED / CANCELLED / EXPIRED."""
        result = session.execute(
            update(MoneyRequestRecord)
            .where(
                MoneyRequestRecord.id == request_id,
                MoneyRequestRecord.status == "PENDING",
            )
            .values(status=status, responded_at=now, updated_at=now)
        )
        return result.rowcount or 0

    def expire_due(self, session: Session, *, now: datetime) -> int:
        """Sweep requests past their expiry. Idempotent by construction."""
        result = session.execute(
            update(MoneyRequestRecord)
            .where(
                MoneyRequestRecord.status == "PENDING",
                MoneyRequestRecord.expires_at <= now,
            )
            .values(status="EXPIRED", updated_at=now)
        )
        return result.rowcount or 0

    def list_for_account(
        self,
        session: Session,
        *,
        account_id: str,
        direction: str,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        """List incoming (to pay) or outgoing (sent) requests, newest first.

        Cursor pagination on the ULID primary key, never OFFSET: OFFSET makes
        the database walk and discard every skipped row, so page 10,000 costs
        10,000 times page 1 (spec 22.1).
        """
        column = (
            MoneyRequestRecord.payer_account_id
            if direction == "incoming"
            else MoneyRequestRecord.requester_account_id
        )
        stmt = select(MoneyRequestRecord).where(column == account_id)
        if status:
            stmt = stmt.where(MoneyRequestRecord.status == status)
        if cursor:
            stmt = stmt.where(MoneyRequestRecord.id < cursor)

        records = session.scalars(
            stmt.order_by(MoneyRequestRecord.id.desc()).limit(limit)
        ).all()
        return [self._to_dict(r) for r in records]

    @staticmethod
    def _to_dict(record: MoneyRequestRecord) -> dict[str, Any]:
        return {
            "requestId": record.id,
            "reference": record.reference,
            "requesterAccountId": record.requester_account_id,
            "payerAccountId": record.payer_account_id,
            "amountMinor": record.amount_minor,
            "currency": record.currency.strip(),
            "note": record.note,
            "status": record.status,
            "transferId": record.transfer_id,
            "expiresAt": record.expires_at.isoformat(),
            "createdAt": record.created_at.isoformat(),
            "respondedAt": record.responded_at.isoformat() if record.responded_at else None,
        }
