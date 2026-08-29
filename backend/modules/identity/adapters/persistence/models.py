"""Identity tables: users and refresh-token sessions."""

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
    ShortText,
    StatusText,
    UlidPk,
    created_at_column,
    updated_at_column,
)

USER_STATUSES = ("ACTIVE", "SUSPENDED", "CLOSED")


class UserRecord(Base):
    """A person who can authenticate.

    Two independent secrets are stored (spec 21.1): ``password_hash`` proves
    identity at login, ``pin_hash`` authorises an individual money movement.
    Keeping them separate means a stolen session cannot move money, and both
    are Argon2id hashes - never reversible, never logged.
    """

    __tablename__ = "users"

    id: Mapped[UlidPk]
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    display_name: Mapped[ShortText] = mapped_column(nullable=False)

    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    pin_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # Spec 21.1: throttle brute-force PIN guessing at the account, not just the IP.
    pin_failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    pin_locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[StatusText] = mapped_column(
        nullable=False, default="ACTIVE", server_default=text("'ACTIVE'")
    )

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    __table_args__ = (
        CheckConstraint(
            "status IN ('ACTIVE', 'SUSPENDED', 'CLOSED')",
            name="status_valid",
        ),
        CheckConstraint("pin_failed_attempts >= 0", name="pin_attempts_non_negative"),
        # Bangladeshi mobile format, validated again at the edge by Pydantic.
        CheckConstraint("phone ~ '^01[3-9][0-9]{8}$'", name="phone_format"),
    )


class SessionRecord(Base):
    """A refresh-token session.

    Spec 21.1 requires rotation and revocation. Only the *hash* of the refresh
    token is stored, so a database disclosure does not yield usable tokens.
    ``rotated_to_id`` links each token to its successor, which makes token
    replay detectable: presenting an already-rotated token means the token
    leaked, and the whole chain should be revoked.
    """

    __tablename__ = "sessions"

    id: Mapped[UlidPk]
    user_id: Mapped[str] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    rotated_to_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )

    issued_at: Mapped[datetime] = created_at_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    __table_args__ = (
        Index("ix_sessions_user_id_active", "user_id", "expires_at"),
    )
