"""Community Spot-Me pool endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from apps.api.dependencies import (
    ContainerDep,
    CurrentUser,
    IdempotencyKey,
    RequestId,
    rate_limit,
    run_pin_protected,
)
from apps.api.schemas import (
    CreateOverdraftGrant,
    CreateOverdraftPool,
    FundOverdraftPool,
)
from platform_.kernel.money import SUPPORTED_CURRENCIES, Money
from platform_.web.envelope import success_response

router = APIRouter(prefix="/overdraft", tags=["overdraft"])


@router.get("", summary="View your Spot-Me pool, grants, and debts")
def overdraft_summary(
    container: ContainerDep, user: CurrentUser, request_id: RequestId
):
    result = container.unit_of_work.run(
        lambda session: container.overdraft_service.summary(
            session, user_id=user.user_id
        )
    )
    return success_response(result, request_id=request_id)


@router.post(
    "/pools",
    status_code=201,
    summary="Create and fund a Spot-Me pool",
    dependencies=[Depends(rate_limit("transfer"))],
)
def create_pool(
    body: CreateOverdraftPool,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    def operation(session):  # noqa: ANN001, ANN202
        return container.overdraft_service.create_pool(
            session,
            sponsor_user_id=user.user_id,
            amount=Money.from_minor(
                body.amountMinor, SUPPORTED_CURRENCIES[body.currency]
            ),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    result = run_pin_protected(
        container, user_id=user.user_id, pin=body.pin, operation=operation
    )
    return success_response(result, status_code=201, request_id=request_id)


@router.post(
    "/pools/fund",
    summary="Top up your Spot-Me pool",
    dependencies=[Depends(rate_limit("transfer"))],
)
def fund_pool(
    body: FundOverdraftPool,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    def operation(session):  # noqa: ANN001, ANN202
        return container.overdraft_service.fund_pool(
            session,
            sponsor_user_id=user.user_id,
            amount=Money.from_minor(
                body.amountMinor, SUPPORTED_CURRENCIES[body.currency]
            ),
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    result = run_pin_protected(
        container, user_id=user.user_id, pin=body.pin, operation=operation
    )
    return success_response(
        result,
        request_id=request_id,
        idempotentReplay=result.get("replayed", False),
    )


@router.post("/grants", status_code=201, summary="Pre-approve a trusted borrower")
def create_grant(
    body: CreateOverdraftGrant,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    def operation(session):  # noqa: ANN001, ANN202
        return container.overdraft_service.grant_access(
            session,
            sponsor_user_id=user.user_id,
            beneficiary_phone=body.beneficiaryPhone,
            max_draw_minor=body.maxDrawMinor,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    result = run_pin_protected(
        container, user_id=user.user_id, pin=body.pin, operation=operation
    )
    return success_response(result, status_code=201, request_id=request_id)
