"""PostgreSQL persistence for identity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from modules.identity.adapters.persistence.models import SessionRecord, UserRecord

__all__ = ["SqlSessionRepository", "SqlUserRepository"]


class SqlUserRepository:
    def create(
        self,
        session: Session,
        *,
        user_id: str,
        phone: str,
        display_name: str,
        password_hash: str,
        pin_hash: str,
        now: datetime,
    ) -> None:
        session.execute(
            insert(UserRecord).values(
                id=user_id,
                phone=phone,
                display_name=display_name,
                password_hash=password_hash,
                pin_hash=pin_hash,
                pin_failed_attempts=0,
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
        )

    def get_by_phone(self, session: Session, phone: str) -> UserRecord | None:
        return session.scalars(
            select(UserRecord).where(UserRecord.phone == phone)
        ).one_or_none()

    def get_by_id(self, session: Session, user_id: str) -> UserRecord | None:
        return session.get(UserRecord, user_id)

    def lock_for_update(self, session: Session, user_id: str) -> UserRecord | None:
        """Lock the user row.

        Used by PIN verification: without the lock, several simultaneous wrong
        guesses could each read the same attempt count and write back the same
        incremented value, so ten guesses would register as one.
        """
        return session.scalars(
            select(UserRecord).where(UserRecord.id == user_id).with_for_update()
        ).one_or_none()

    def record_pin_failure(
        self, session: Session, *, user_id: str, attempts: int, locked_until: datetime | None
    ) -> None:
        session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(pin_failed_attempts=attempts, pin_locked_until=locked_until)
        )

    def reset_pin_failures(self, session: Session, *, user_id: str) -> None:
        session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(pin_failed_attempts=0, pin_locked_until=None)
        )

    def update_password_hash(
        self, session: Session, *, user_id: str, password_hash: str
    ) -> None:
        session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(password_hash=password_hash)
        )

    def set_status(self, session: Session, *, user_id: str, status: str) -> int:
        result = session.execute(
            update(UserRecord)
            .where(UserRecord.id == user_id)
            .values(status=status)
        )
        return result.rowcount or 0

    def search(
        self, session: Session, *, phone: str, exclude_user_id: str
    ) -> dict[str, Any] | None:
        """Exact-phone lookup for the recipient confirmation step.

        Exact match only, never a prefix or partial search: a fuzzy directory
        would let anyone enumerate the user base one query at a time.
        """
        record = session.scalars(
            select(UserRecord).where(
                UserRecord.phone == phone,
                UserRecord.id != exclude_user_id,
                UserRecord.status == "ACTIVE",
            )
        ).one_or_none()
        if record is None:
            return None
        # Only what the sender needs to confirm they picked the right person.
        return {"userId": record.id, "displayName": record.display_name, "phone": record.phone}


class SqlSessionRepository:
    def create(
        self,
        session: Session,
        *,
        session_id: str,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        now: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> None:
        session.execute(
            insert(SessionRecord).values(
                id=session_id,
                user_id=user_id,
                refresh_token_hash=refresh_token_hash,
                issued_at=now,
                expires_at=expires_at,
                user_agent=user_agent[:200] if user_agent else None,
                ip_address=ip_address,
            )
        )

    def get_by_token_hash(
        self, session: Session, token_hash: str
    ) -> SessionRecord | None:
        return session.scalars(
            select(SessionRecord).where(SessionRecord.refresh_token_hash == token_hash)
        ).one_or_none()

    def revoke(self, session: Session, *, session_id: str, now: datetime) -> int:
        """Revoke one session. Returns rows affected, so callers can detect reuse."""
        result = session.execute(
            update(SessionRecord)
            .where(SessionRecord.id == session_id, SessionRecord.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return result.rowcount or 0

    def revoke_all_for_user(self, session: Session, *, user_id: str, now: datetime) -> None:
        """Used when a rotated refresh token is replayed - assume compromise."""
        session.execute(
            update(SessionRecord)
            .where(SessionRecord.user_id == user_id, SessionRecord.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def mark_rotated(
        self, session: Session, *, session_id: str, successor_id: str, now: datetime
    ) -> None:
        session.execute(
            update(SessionRecord)
            .where(SessionRecord.id == session_id)
            .values(rotated_to_id=successor_id, revoked_at=now)
        )
