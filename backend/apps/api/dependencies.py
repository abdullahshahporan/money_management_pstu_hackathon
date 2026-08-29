"""FastAPI dependencies: authentication, idempotency key, request context.

Dependency injection stops at the HTTP edge. Handlers receive already-resolved
values; the use cases underneath know nothing about FastAPI (spec 7.1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apps.api.container import Container
from platform_.kernel.errors import (
    AppError,
    AuthenticationError,
    AuthorizationError,
    PinLockedError,
    ValidationError,
)
from platform_.security.tokens import TokenError

__all__ = [
    "AuthenticatedUser",
    "CurrentUser",
    "IdempotencyKey",
    "RequestId",
    "get_container",
    "require_engineering_key",
    "run_pin_protected",
]


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str
    session_id: str


bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description=(
        "Paste the accessToken returned by /auth/login. "
        "Swagger adds 'Bearer ' automatically."
    ),
)


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def get_current_user(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
) -> AuthenticatedUser:
    """Resolve the caller from the bearer token.

    Spec 21.2: identity is derived from the authenticated context on the
    server. No endpoint accepts a user id from the client body or query.
    """
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("A bearer token is required.")

    token = credentials.credentials.strip()
    if not token:
        raise AuthenticationError("A bearer token is required.")
    container: Container = request.app.state.container
    try:
        claims = container.tokens.verify_access_token(token)
    except TokenError as exc:
        raise AuthenticationError("Invalid or expired token.") from exc

    return AuthenticatedUser(user_id=claims.user_id, session_id=claims.session_id)


def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    """Require an idempotency key on every money mutation (spec 12.1).

    Required, not optional. If it were optional, the safe path would be the
    one clients forget, and a retry after a timeout would move money twice.
    """
    if not idempotency_key:
        raise ValidationError(
            "The Idempotency-Key header is required for this operation.",
            details={"header": "Idempotency-Key"},
        )
    key = idempotency_key.strip()
    if not 8 <= len(key) <= 128:
        raise ValidationError(
            "Idempotency-Key must be between 8 and 128 characters.",
            details={"header": "Idempotency-Key"},
        )
    return key


def require_engineering_key(
    request: Request,
    x_engineering_key: Annotated[str | None, Header(alias="X-Engineering-Key")] = None,
) -> None:
    """Guard the demo/ops endpoints (spec 21.2).

    Separate from user authentication on purpose: these endpoints expose
    system-wide integrity data, so being a logged-in user is not sufficient.
    """
    import secrets

    container: Container = request.app.state.container
    expected = container.settings.engineering_api_key
    if not x_engineering_key or not secrets.compare_digest(x_engineering_key, expected):
        raise AuthorizationError("A valid engineering key is required.")


def client_ip(request: Request) -> str:
    """The caller's IP, trusting X-Forwarded-For only behind our own proxy."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(policy_name: str):  # noqa: ANN201
    """Build a dependency that enforces one named quota.

    Authenticated callers are limited per user, so one abusive account cannot
    exhaust the quota for everyone sharing an IP - a real concern in Bangladesh
    where carrier-grade NAT puts many subscribers behind one address.
    Unauthenticated endpoints fall back to the IP.
    """

    def dependency(request: Request) -> None:
        container: Container = request.app.state.container
        authorization = request.headers.get("Authorization", "")
        identifier = f"ip:{client_ip(request)}"

        if authorization.lower().startswith("bearer "):
            try:
                claims = container.tokens.verify_access_token(authorization[7:].strip())
                identifier = f"user:{claims.user_id}"
            except TokenError:
                pass  # Fall back to the IP; auth itself will reject shortly.

        container.rate_limiter.check(policy_name=policy_name, identifier=identifier)

    return dependency


def run_pin_protected[T](
    container: Container,
    *,
    user_id: str,
    pin: str,
    operation: Callable[[object], T],
) -> T:
    """Run a PIN-authorised command while preserving failed-attempt state.

    An exception leaving UnitOfWork causes rollback.  A wrong PIN must still
    commit its attempt counter, so it is returned out of the transaction as a
    value and raised only after commit.  Successful verification and the money
    command remain in one transaction.
    """

    def transaction(session):  # noqa: ANN001, ANN202
        try:
            container.auth_service.verify_pin(session, user_id=user_id, pin=pin)
        except (ValidationError, PinLockedError) as exc:
            return exc
        return operation(session)

    result = container.unit_of_work.run(transaction)
    if isinstance(result, AppError):
        raise result
    return result


CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]
IdempotencyKey = Annotated[str, Depends(get_idempotency_key)]
RequestId = Annotated[str, Depends(get_request_id)]
ContainerDep = Annotated[Container, Depends(get_container)]
