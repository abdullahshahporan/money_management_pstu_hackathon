"""Stable, machine-readable error taxonomy (spec 22.4).

Error codes are part of the public API contract: clients branch on ``code``,
never on ``message``. Each error carries the HTTP status it maps to and whether
retrying could plausibly succeed, so the transport layer needs no translation
table and the client needs no guesswork.

``retryable`` is a safety signal, not a suggestion. A retryable money mutation
must still be retried with the *same* idempotency key (spec 20.2).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AccountInactiveError",
    "AppError",
    "AuthenticationError",
    "AuthorizationError",
    "ConflictError",
    "CurrencyMismatchError",
    "DailyLimitExceededError",
    "DependencyUnavailableError",
    "IdempotencyKeyReuseError",
    "InsufficientFundsError",
    "NotFoundError",
    "PinLockedError",
    "RateLimitedError",
    "RequestInProgressError",
    "SelfTransferError",
    "StateConflictError",
    "TransferLimitExceededError",
    "ValidationError",
]


class AppError(Exception):
    """Base class for every deliberate, expected failure in the system.

    An ``AppError`` is a *decision*, not a crash: it means the system correctly
    refused to do something. Unexpected exceptions are deliberately not
    modelled here - they surface as 500 and are logged with a stack trace.
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or type(self).message
        self.details = details or {}
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.details:
            payload["details"] = self.details
        return payload


# -- 400 / 422 : the request was understood but refused -----------------------

class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    http_status = 400
    message = "The request was malformed."


class InsufficientFundsError(AppError):
    """Invariant 6 (spec 8.2). Never retryable: retrying cannot create money."""

    code = "INSUFFICIENT_FUNDS"
    http_status = 422
    message = "Your available balance is insufficient."


class SelfTransferError(AppError):
    code = "SELF_TRANSFER_NOT_ALLOWED"
    http_status = 422
    message = "You cannot send money to your own account."


class AccountInactiveError(AppError):
    code = "ACCOUNT_INACTIVE"
    http_status = 422
    message = "This account cannot transact at the moment."


class CurrencyMismatchError(AppError):
    code = "CURRENCY_MISMATCH"
    http_status = 422
    message = "Both accounts must hold the same currency."


class TransferLimitExceededError(AppError):
    code = "TRANSFER_LIMIT_EXCEEDED"
    http_status = 422
    message = "This amount exceeds the permitted transfer limit."


class DailyLimitExceededError(AppError):
    code = "DAILY_LIMIT_EXCEEDED"
    http_status = 422
    message = "This transfer would exceed your daily sending limit."


# -- 401 / 403 / 404 : identity and visibility --------------------------------

class AuthenticationError(AppError):
    code = "UNAUTHENTICATED"
    http_status = 401
    message = "Authentication is required."


class AuthorizationError(AppError):
    """Spec 21.2: object-level authorization failure."""

    code = "FORBIDDEN"
    http_status = 403
    message = "You are not permitted to perform this action."


class NotFoundError(AppError):
    """Also returned instead of 403 where existence itself is sensitive."""

    code = "NOT_FOUND"
    http_status = 404
    message = "The requested resource was not found."


class PinLockedError(AppError):
    code = "PIN_LOCKED"
    http_status = 423
    message = "Too many incorrect PIN attempts. Try again later."


# -- 409 : state and idempotency conflicts ------------------------------------

class ConflictError(AppError):
    code = "CONFLICT"
    http_status = 409
    message = "The request conflicts with the current state."


class IdempotencyKeyReuseError(ConflictError):
    """Spec 12.2 step 3: same key, different payload."""

    code = "IDEMPOTENCY_KEY_REUSED"
    message = "This idempotency key was already used with a different request."


class RequestInProgressError(ConflictError):
    """Spec 12.2 step 6: a concurrent duplicate is still executing.

    Retryable with the same key - the original will have completed by then.
    """

    code = "REQUEST_IN_PROGRESS"
    retryable = True
    message = "An identical request is currently being processed."


class StateConflictError(ConflictError):
    """A money request already reached a terminal state (spec 13.2)."""

    code = "REQUEST_ALREADY_HANDLED"
    message = "This request has already been handled."


# -- 429 / 503 : capacity and dependencies ------------------------------------

class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    http_status = 429
    retryable = True
    message = "Too many requests. Please slow down."

    def __init__(self, retry_after_seconds: int = 60, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.retry_after_seconds = retry_after_seconds


class DependencyUnavailableError(AppError):
    """Spec 18.5: the authoritative database is unreachable.

    Returned instead of guessing a balance or fabricating a success.
    """

    code = "SERVICE_TEMPORARILY_UNAVAILABLE"
    http_status = 503
    retryable = True
    message = "The service is temporarily unavailable. Please retry shortly."
