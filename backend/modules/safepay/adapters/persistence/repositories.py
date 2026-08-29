"""PostgreSQL persistence for Conditional Safe-Pay."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, text, update
from sqlalchemy.orm import Session

from modules.identity.adapters.persistence.models import UserRecord
from modules.safepay.adapters.persistence.models import SafePayEscrowRecord

__all__ = ["SqlSafePayRepository"]


def _row(record: SafePayEscrowRecord) -> dict[str, Any]:
    return {
        "escrowId": record.id,
        "reference": record.reference,
        "holdTransferId": record.hold_transfer_id,
        "settlementTransferId": record.settlement_transfer_id,
        "buyerUserId": record.buyer_user_id,
        "sellerUserId": record.seller_user_id,
        "amountMinor": record.amount_minor,
        "currency": record.currency.strip(),
        "description": record.description,
        "status": record.status,
        "trackingNumber": record.tracking_number,
        "courier": record.courier_slug,
        "shippedAt": record.shipped_at.isoformat() if record.shipped_at else None,
        "deliveredAt": record.delivered_at.isoformat() if record.delivered_at else None,
        "autoReleaseAt": (
            record.auto_release_at.isoformat() if record.auto_release_at else None
        ),
        "settledAt": record.settled_at.isoformat() if record.settled_at else None,
        "disputeReason": record.dispute_reason,
        "disputedAt": record.disputed_at.isoformat() if record.disputed_at else None,
        "resolutionNote": record.resolution_note,
        "createdAt": record.created_at.isoformat(),
    }


class SqlSafePayRepository:
    def create(
        self,
        session: Session,
        *,
        escrow_id: str,
        reference: str,
        hold_transfer_id: str,
        buyer_user_id: str,
        seller_user_id: str,
        amount_minor: int,
        currency: str,
        description: str | None,
        delivery_code_hash: str,
        now: datetime,
    ) -> None:
        session.execute(
            insert(SafePayEscrowRecord).values(
                id=escrow_id,
                reference=reference,
                hold_transfer_id=hold_transfer_id,
                buyer_user_id=buyer_user_id,
                seller_user_id=seller_user_id,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                status="AWAITING_SHIPMENT",
                delivery_code_hash=delivery_code_hash,
                delivery_code_attempts=0,
                created_at=now,
                updated_at=now,
            )
        )

    def get(self, session: Session, escrow_id: str) -> SafePayEscrowRecord | None:
        return session.get(SafePayEscrowRecord, escrow_id)

    def lock(self, session: Session, escrow_id: str) -> SafePayEscrowRecord | None:
        return session.scalars(
            select(SafePayEscrowRecord)
            .where(SafePayEscrowRecord.id == escrow_id)
            .with_for_update()
        ).one_or_none()

    def find_by_tracking(
        self, session: Session, *, courier: str, tracking_number: str
    ) -> SafePayEscrowRecord | None:
        return session.scalars(
            select(SafePayEscrowRecord)
            .where(
                SafePayEscrowRecord.courier_slug == courier,
                SafePayEscrowRecord.tracking_number == tracking_number,
            )
            .order_by(SafePayEscrowRecord.created_at.desc())
            .with_for_update()
            .limit(1)
        ).first()

    def as_dict(self, record: SafePayEscrowRecord) -> dict[str, Any]:
        return _row(record)

    def list_for_user(
        self, session: Session, *, user_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = session.execute(
            text(
                """
                SELECT e.*, buyer.display_name AS buyer_name,
                       seller.display_name AS seller_name
                FROM safepay_escrows e
                JOIN users buyer ON buyer.id = e.buyer_user_id
                JOIN users seller ON seller.id = e.seller_user_id
                WHERE e.buyer_user_id = :user_id OR e.seller_user_id = :user_id
                ORDER BY e.created_at DESC
                LIMIT :limit
                """
            ),
            {"user_id": user_id, "limit": limit},
        ).mappings()
        return [
            {
                "escrowId": r["id"],
                "reference": r["reference"],
                "role": "BUYER" if r["buyer_user_id"] == user_id else "SELLER",
                "counterpartyName": (
                    r["seller_name"] if r["buyer_user_id"] == user_id else r["buyer_name"]
                ),
                "amountMinor": r["amount_minor"],
                "currency": r["currency"].strip(),
                "description": r["description"],
                "status": r["status"],
                "trackingNumber": r["tracking_number"],
                "courier": r["courier_slug"],
                "autoReleaseAt": (
                    r["auto_release_at"].isoformat() if r["auto_release_at"] else None
                ),
                "createdAt": r["created_at"].isoformat(),
            }
            for r in rows
        ]

    def list_disputes(
        self, session: Session, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return the admin dispute queue with both parties and delivery evidence."""
        rows = session.execute(
            text(
                """
                SELECT e.id, e.reference, e.amount_minor, e.currency, e.description,
                       e.dispute_reason, e.disputed_at, e.courier_slug,
                       e.tracking_number, e.shipped_at, e.delivered_at, e.created_at,
                       buyer.display_name AS buyer_name, buyer.phone AS buyer_phone,
                       seller.display_name AS seller_name, seller.phone AS seller_phone
                FROM safepay_escrows e
                JOIN users buyer ON buyer.id = e.buyer_user_id
                JOIN users seller ON seller.id = e.seller_user_id
                WHERE e.status = 'DISPUTED'
                ORDER BY e.disputed_at ASC, e.id ASC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings()
        return [
            {
                "escrowId": row["id"],
                "reference": row["reference"],
                "amountMinor": row["amount_minor"],
                "currency": row["currency"].strip(),
                "description": row["description"],
                "reason": row["dispute_reason"],
                "disputedAt": row["disputed_at"].isoformat(),
                "buyerName": row["buyer_name"],
                "buyerPhone": row["buyer_phone"],
                "sellerName": row["seller_name"],
                "sellerPhone": row["seller_phone"],
                "courier": row["courier_slug"],
                "trackingNumber": row["tracking_number"],
                "shippedAt": row["shipped_at"].isoformat() if row["shipped_at"] else None,
                "deliveredAt": (
                    row["delivered_at"].isoformat() if row["delivered_at"] else None
                ),
                "createdAt": row["created_at"].isoformat(),
            }
            for row in rows
        ]

    def mark_shipped(
        self,
        session: Session,
        *,
        escrow_id: str,
        seller_user_id: str,
        courier: str,
        tracking_number: str,
        now: datetime,
    ) -> int:
        result = session.execute(
            update(SafePayEscrowRecord)
            .where(
                SafePayEscrowRecord.id == escrow_id,
                SafePayEscrowRecord.seller_user_id == seller_user_id,
                SafePayEscrowRecord.status == "AWAITING_SHIPMENT",
            )
            .values(
                status="SHIPPED",
                courier_slug=courier,
                tracking_number=tracking_number,
                shipped_at=now,
                updated_at=now,
            )
        )
        return result.rowcount or 0

    def mark_delivered(
        self,
        session: Session,
        *,
        escrow_id: str,
        delivered_at: datetime,
        auto_release_at: datetime,
    ) -> int:
        result = session.execute(
            update(SafePayEscrowRecord)
            .where(
                SafePayEscrowRecord.id == escrow_id,
                SafePayEscrowRecord.status == "SHIPPED",
            )
            .values(
                status="DELIVERED",
                delivered_at=delivered_at,
                auto_release_at=auto_release_at,
                updated_at=delivered_at,
            )
        )
        return result.rowcount or 0

    def mark_disputed(
        self,
        session: Session,
        *,
        escrow_id: str,
        buyer_user_id: str,
        reason: str,
        now: datetime,
    ) -> int:
        result = session.execute(
            update(SafePayEscrowRecord)
            .where(
                SafePayEscrowRecord.id == escrow_id,
                SafePayEscrowRecord.buyer_user_id == buyer_user_id,
                SafePayEscrowRecord.status.in_(
                    ("AWAITING_SHIPMENT", "SHIPPED", "DELIVERED")
                ),
            )
            .values(
                status="DISPUTED",
                dispute_reason=reason,
                disputed_at=now,
                updated_at=now,
            )
        )
        return result.rowcount or 0

    def record_code_failure(
        self,
        session: Session,
        *,
        escrow_id: str,
        attempts: int,
        locked_until: datetime | None,
        now: datetime,
    ) -> None:
        session.execute(
            update(SafePayEscrowRecord)
            .where(SafePayEscrowRecord.id == escrow_id)
            .values(
                delivery_code_attempts=attempts,
                delivery_code_locked_until=locked_until,
                updated_at=now,
            )
        )

    def mark_settled(
        self,
        session: Session,
        *,
        escrow_id: str,
        from_statuses: tuple[str, ...],
        status: str,
        settlement_transfer_id: str,
        now: datetime,
        resolved_by_user_id: str | None = None,
        resolution_note: str | None = None,
    ) -> int:
        result = session.execute(
            update(SafePayEscrowRecord)
            .where(
                SafePayEscrowRecord.id == escrow_id,
                SafePayEscrowRecord.status.in_(from_statuses),
                SafePayEscrowRecord.settlement_transfer_id.is_(None),
            )
            .values(
                status=status,
                settlement_transfer_id=settlement_transfer_id,
                settled_at=now,
                resolved_by_user_id=resolved_by_user_id,
                resolution_note=resolution_note,
                delivery_code_attempts=0,
                delivery_code_locked_until=None,
                updated_at=now,
            )
        )
        return result.rowcount or 0

    def seller_user(self, session: Session, seller_user_id: str) -> UserRecord | None:
        return session.get(UserRecord, seller_user_id)
