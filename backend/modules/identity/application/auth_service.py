"""Registration, login, token refresh and PIN authorisation.

Spec 21.1. Three decisions here are worth stating outright:

1.  Registration and the opening grant share one transaction. A user cannot
    exist without their funded account, and the account cannot exist without
    its balancing ledger entry.
2.  Login failures return one generic message regardless of cause, so the
    endpoint cannot be used to enumerate which phone numbers are registered.
3.  PIN verification locks the user row. Without it, simultaneous wrong
    guesses would each read the same attempt counter and overwrite one
    another, turning a lockout after five attempts into no lockout at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from modules.identity.application.ports import (
    AccountOpener,
    SessionRepository,
    UserRepository,
)
from platform_.kernel.clock import Clock
from platform_.kernel.errors import (
    AuthenticationError,
    ConflictError,
    PinLockedError,
    ValidationError,
)
from platform_.kernel.ids import new_ulid
from platform_.kernel.money import Money
from platform_.security.passwords import PasswordHasherService
from platform_.security.tokens import TokenService, hash_refresh_token

logger = logging.getLogger(__name__)

__all__ = ["AuthService", "AuthTokens", "RegisteredUser"]

# One generic message for every login failure. Spec 21.1: do not leak whether
# the phone exists, the password was wrong, or the account is suspended.
_GENERIC_LOGIN_FAILURE = "Phone number or password is incorrect."


@dataclass(frozen=True, slots=True)
class AuthTokens:
    access_token: str
    refresh_token: str
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class RegisteredUser:
    user_id: str
    account_id: str
    display_name: str
    phone: str
    tokens: AuthTokens


class AuthService:
    def __init__(
        self,
        *,
        users: UserRepository,
        sessions: SessionRepository,
        open_account: AccountOpener,
        passwords: PasswordHasherService,
        tokens: TokenService,
        clock: Clock,
        opening_balance: Money,
        pin_max_attempts: int = 5,
        pin_lockout_seconds: int = 900,
        access_ttl_seconds: int = 900,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._open_account = open_account
        self._passwords = passwords
        self._tokens = tokens
        self._clock = clock
        self._opening_balance = opening_balance
        self._pin_max_attempts = pin_max_attempts
        self._pin_lockout_seconds = pin_lockout_seconds
        self._access_ttl_seconds = access_ttl_seconds

    # -- registration ------------------------------------------------------

    def register(
        self,
        session: Session,
        *,
        phone: str,
        display_name: str,
        password: str,
        pin: str,
        request_id: str | None = None,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> RegisteredUser:
        now = self._clock.now()
        user_id = new_ulid()

        try:
            self._users.create(
                session,
                user_id=user_id,
                phone=phone,
                display_name=display_name,
                password_hash=self._passwords.hash(password),
                pin_hash=self._passwords.hash(pin),
                now=now,
            )
            session.flush()
        except IntegrityError as exc:
            # The UNIQUE constraint is the authority on duplicates, not a
            # prior SELECT - two simultaneous registrations would both pass
            # a check-then-insert.
            raise ConflictError("This phone number is already registered.") from exc

        account_id = self._open_account.execute(
            session,
            user_id=user_id,
            opening_balance=self._opening_balance,
            account_number=self._account_number_for(user_id),
            now=now,
            request_id=request_id,
        )

        tokens = self._start_session(
            session,
            user_id=user_id,
            now=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )

        logger.info("user_registered", extra={"user_id": user_id, "account_id": account_id})
        return RegisteredUser(
            user_id=user_id,
            account_id=account_id,
            display_name=display_name,
            phone=phone,
            tokens=tokens,
        )

    # -- login / refresh / logout -----------------------------------------

    def login(
        self,
        session: Session,
        *,
        phone: str,
        password: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> tuple[str, AuthTokens]:
        now = self._clock.now()
        user = self._users.get_by_phone(session, phone)

        if user is None:
            # Hash anyway so a missing user and a wrong password take
            # comparable time - otherwise response latency reveals which
            # phone numbers exist.
            self._passwords.hash(password)
            raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

        if not self._passwords.verify(user.password_hash, password):
            raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

        if user.status != "ACTIVE":
            raise AuthenticationError(_GENERIC_LOGIN_FAILURE)

        # Transparently upgrade the hash if cost parameters have been raised.
        if self._passwords.needs_rehash(user.password_hash):
            self._users.update_password_hash(
                session, user_id=user.id, password_hash=self._passwords.hash(password)
            )

        tokens = self._start_session(
            session, user_id=user.id, now=now, user_agent=user_agent, ip_address=ip_address
        )
        return user.id, tokens

    def refresh(
        self,
        session: Session,
        *,
        refresh_token: str,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> AuthTokens:
        now = self._clock.now()
        record = self._sessions.get_by_token_hash(session, hash_refresh_token(refresh_token))

        if record is None:
            raise AuthenticationError("Invalid refresh token.")

        if record.revoked_at is not None:
            # A revoked token being presented means it was rotated already and
            # someone still holds the old copy. Treat it as a compromise and
            # revoke the entire family rather than just refusing this one.
            logger.warning(
                "refresh_token_reuse_detected", extra={"user_id": record.user_id}
            )
            self._sessions.revoke_all_for_user(session, user_id=record.user_id, now=now)
            raise AuthenticationError("Session is no longer valid. Please sign in again.")

        if record.expires_at <= now:
            raise AuthenticationError("Session expired. Please sign in again.")

        successor_id = new_ulid()
        raw_refresh, refresh_hash = self._tokens.issue_refresh_token()
        self._sessions.create(
            session,
            session_id=successor_id,
            user_id=record.user_id,
            refresh_token_hash=refresh_hash,
            expires_at=self._tokens.refresh_expiry(now),
            now=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        self._sessions.mark_rotated(
            session, session_id=record.id, successor_id=successor_id, now=now
        )

        return AuthTokens(
            access_token=self._tokens.issue_access_token(
                user_id=record.user_id, session_id=successor_id, now=now
            ),
            refresh_token=raw_refresh,
            expires_in_seconds=self._access_ttl_seconds,
        )

    def logout(self, session: Session, *, refresh_token: str) -> None:
        now = self._clock.now()
        record = self._sessions.get_by_token_hash(session, hash_refresh_token(refresh_token))
        if record is not None:
            self._sessions.revoke(session, session_id=record.id, now=now)

    # -- PIN authorisation -------------------------------------------------

    def verify_pin(self, session: Session, *, user_id: str, pin: str) -> None:
        """Authorise a money movement. Raises on failure; returns None on success."""
        now = self._clock.now()
        user = self._users.lock_for_update(session, user_id)
        if user is None:
            raise AuthenticationError("Unknown user.")

        if user.pin_locked_until is not None and user.pin_locked_until > now:
            raise PinLockedError(
                details={"lockedUntil": user.pin_locked_until.isoformat()}
            )

        if self._passwords.verify(user.pin_hash, pin):
            if user.pin_failed_attempts:
                self._users.reset_pin_failures(session, user_id=user_id)
            return

        attempts = user.pin_failed_attempts + 1
        locked_until = (
            now + timedelta(seconds=self._pin_lockout_seconds)
            if attempts >= self._pin_max_attempts
            else None
        )
        self._users.record_pin_failure(
            session, user_id=user_id, attempts=attempts, locked_until=locked_until
        )
        logger.warning(
            "pin_verification_failed",
            extra={"user_id": user_id, "attempts": attempts, "locked": locked_until is not None},
        )

        if locked_until is not None:
            raise PinLockedError(details={"lockedUntil": locked_until.isoformat()})

        raise ValidationError(
            "Incorrect PIN.",
            details={"remainingAttempts": self._pin_max_attempts - attempts},
        )

    # -- helpers -----------------------------------------------------------

    def _start_session(
        self,
        session: Session,
        *,
        user_id: str,
        now: datetime,
        user_agent: str | None,
        ip_address: str | None,
    ) -> AuthTokens:
        session_id = new_ulid()
        raw_refresh, refresh_hash = self._tokens.issue_refresh_token()
        self._sessions.create(
            session,
            session_id=session_id,
            user_id=user_id,
            refresh_token_hash=refresh_hash,
            expires_at=self._tokens.refresh_expiry(now),
            now=now,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return AuthTokens(
            access_token=self._tokens.issue_access_token(
                user_id=user_id, session_id=session_id, now=now
            ),
            refresh_token=raw_refresh,
            expires_in_seconds=self._access_ttl_seconds,
        )

    @staticmethod
    def _account_number_for(user_id: str) -> str:
        """A short, stable, human-quotable account number derived from the id."""
        return f"MM{user_id[-10:]}"
