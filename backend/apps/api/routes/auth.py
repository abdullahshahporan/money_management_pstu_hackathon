"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from apps.api.dependencies import ContainerDep, CurrentUser, RequestId, rate_limit
from apps.api.schemas import LoginRequest, LogoutRequest, RefreshRequest, RegisterRequest
from platform_.web.envelope import success_response

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_context(request: Request) -> tuple[str | None, str | None]:
    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else None
    )
    return request.headers.get("User-Agent"), ip


@router.post(
    "/register",
    status_code=201,
    summary="Register and open a funded account",
    dependencies=[Depends(rate_limit("register"))],
)
def register(
    body: RegisterRequest,
    request: Request,
    container: ContainerDep,
    request_id: RequestId,
):
    """Create a user, open their account, and fund it through the ledger.

    The user, the account and the opening ledger transaction all commit
    together: there is no window in which a user exists without a funded,
    fully-accounted balance.
    """
    user_agent, ip = _client_context(request)
    result = container.unit_of_work.run(
        lambda session: container.auth_service.register(
            session,
            phone=body.phone,
            display_name=body.displayName,
            password=body.password,
            pin=body.pin,
            request_id=request_id,
            user_agent=user_agent,
            ip_address=ip,
        )
    )
    return success_response(
        {
            "userId": result.user_id,
            "accountId": result.account_id,
            "displayName": result.display_name,
            "phone": result.phone,
            "accessToken": result.tokens.access_token,
            "refreshToken": result.tokens.refresh_token,
            "expiresIn": result.tokens.expires_in_seconds,
        },
        status_code=201,
        request_id=request_id,
    )


@router.post(
    "/login",
    summary="Authenticate",
    dependencies=[Depends(rate_limit("login"))],
)
def login(
    body: LoginRequest,
    request: Request,
    container: ContainerDep,
    request_id: RequestId,
):
    user_agent, ip = _client_context(request)
    user_id, tokens = container.unit_of_work.run(
        lambda session: container.auth_service.login(
            session,
            phone=body.phone,
            password=body.password,
            user_agent=user_agent,
            ip_address=ip,
        )
    )
    return success_response(
        {
            "userId": user_id,
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresIn": tokens.expires_in_seconds,
        },
        request_id=request_id,
    )


@router.post("/refresh", summary="Rotate the refresh token")
def refresh(
    body: RefreshRequest,
    request: Request,
    container: ContainerDep,
    request_id: RequestId,
):
    user_agent, ip = _client_context(request)
    tokens = container.unit_of_work.run(
        lambda session: container.auth_service.refresh(
            session,
            refresh_token=body.refreshToken,
            user_agent=user_agent,
            ip_address=ip,
        )
    )
    return success_response(
        {
            "accessToken": tokens.access_token,
            "refreshToken": tokens.refresh_token,
            "expiresIn": tokens.expires_in_seconds,
        },
        request_id=request_id,
    )


@router.post("/logout", summary="Revoke the current session")
def logout(
    body: LogoutRequest,
    container: ContainerDep,
    request_id: RequestId,
    user: CurrentUser,
):
    container.unit_of_work.run(
        lambda session: container.auth_service.logout(
            session, refresh_token=body.refreshToken
        )
    )
    return success_response({"revoked": True}, request_id=request_id)
