"""Immutable audit trail (spec 8.2 invariant 15, spec 21.4).

Every mutation records who did it, under which request, and to what. Like the
ledger, this table is append-only at the privilege level: the runtime role can
INSERT and SELECT but cannot UPDATE or DELETE, so an attacker who reaches the
application cannot quietly erase their own trail.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CHAR, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from platform_.database.base import Base
from platform_.database.types import ULID_LENGTH, UlidPk, created_at_column


class AuditLogRecord(Base):
    __tablename__ = "audit_logs"

    id: Mapped[UlidPk]

    # Nullable: some auditable events have no authenticated actor, such as a
    # failed login or an automated expiry sweep.
    actor_user_id: Mapped[str | None] = mapped_column(
        CHAR(ULID_LENGTH), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(CHAR(ULID_LENGTH), nullable=True)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Redacted at the call site - never store tokens, PINs or password material.
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)

    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        Index("ix_audit_logs_actor_created", "actor_user_id", "created_at"),
        Index("ix_audit_logs_resource", "resource_type", "resource_id"),
    )
