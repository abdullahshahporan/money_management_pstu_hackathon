"""Ports for the identity module (spec 7.3).

The auth service depends on these Protocols, not on the SQL implementations.
Structural typing means the concrete repositories satisfy them without
inheriting anything, so the adapters stay free of framework-imposed base
classes while the dependency still points inward.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class UserRecordLike(Protocol):
    """The user fields the auth service actually reads.

    Deliberately narrow: the service never needs the whole ORM row, and
    naming only what it uses keeps the coupling visible.
    """

    id: str
    phone: str
    display_name: str
    password_hash: str
    pin_hash: str
    pin_failed_attempts: int
    pin_locked_until: datetime | None
    status: str


class SessionRecordLike(Protocol):
    id: str
    user_id: str
    expires_at: datetime
    revoked_at: datetime | None


class UserRepository(Protocol):
    def create(
        self,
        session: Any,
        *,
        user_id: str,
        phone: str,
        display_name: str,
        password_hash: str,
        pin_hash: str,
        now: datetime,
    ) -> None: ...

    def get_by_phone(self, session: Any, phone: str) -> UserRecordLike | None: ...

    def get_by_id(self, session: Any, user_id: str) -> UserRecordLike | None: ...

    def lock_for_update(self, session: Any, user_id: str) -> UserRecordLike | None: ...

    def record_pin_failure(
        self, session: Any, *, user_id: str, attempts: int, locked_until: datetime | None
    ) -> None: ...

    def reset_pin_failures(self, session: Any, *, user_id: str) -> None: ...

    def update_password_hash(
        self, session: Any, *, user_id: str, password_hash: str
    ) -> None: ...

    def search(
        self, session: Any, *, phone: str, exclude_user_id: str
    ) -> dict[str, Any] | None: ...


class SessionRepository(Protocol):
    def create(
        self,
        session: Any,
        *,
        session_id: str,
        user_id: str,
        refresh_token_hash: str,
        expires_at: datetime,
        now: datetime,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> None: ...

    def get_by_token_hash(
        self, session: Any, token_hash: str
    ) -> SessionRecordLike | None: ...

    def revoke(self, session: Any, *, session_id: str, now: datetime) -> int: ...

    def revoke_all_for_user(self, session: Any, *, user_id: str, now: datetime) -> None: ...

    def mark_rotated(
        self, session: Any, *, session_id: str, successor_id: str, now: datetime
    ) -> None: ...


class AccountOpener(Protocol):
    """The financial core's account-opening entry point.

    Typed as a port so identity depends on a capability, not on the financial
    core's concrete class.
    """

    def execute(
        self,
        session: Any,
        *,
        user_id: str,
        opening_balance: Any,
        account_number: str,
        now: datetime,
        request_id: str | None = None,
    ) -> str: ...
