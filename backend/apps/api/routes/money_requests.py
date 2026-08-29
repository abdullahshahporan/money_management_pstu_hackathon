"""Money request endpoints - the "collect what I am owed" flow."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from apps.api.dependencies import (
    ContainerDep,
    CurrentUser,
    IdempotencyKey,
    RequestId,
    rate_limit,
    run_pin_protected,
)
from apps.api.schemas import AcceptMoneyRequest, CreateMoneyRequest
from platform_.kernel.money import SUPPORTED_CURRENCIES, Money
from platform_.web.envelope import success_response

router = APIRouter(prefix="/money-requests", tags=["money-requests"])


@router.post(
    "",
    status_code=201,
    summary="Ask someone to pay you",
    dependencies=[Depends(rate_limit("money_request"))],
)
def create_request(
    body: CreateMoneyRequest,
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    amount = Money.from_minor(body.amountMinor, SUPPORTED_CURRENCIES[body.currency])
    result = container.unit_of_work.run(
        lambda session: container.money_request_service.create(
            session,
            requester_user_id=user.user_id,
            payer_phone=body.payerPhone,
            amount=amount,
            note=body.note,
            request_id=request_id,
        )
    )
    return success_response(
        {
            "requestId": result.request_id,
            "reference": result.reference,
            "status": result.status,
        },
        status_code=201,
        request_id=request_id,
    )


@router.get("", summary="List incoming or outgoing requests")
def list_requests(
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
    direction: Annotated[Literal["incoming", "outgoing"], Query()] = "incoming",
    status: Annotated[
        Literal["PENDING", "ACCEPTED", "REJECTED", "CANCELLED", "EXPIRED"] | None, Query()
    ] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=26)] = None,
):
    rows = container.unit_of_work.run(
        lambda session: container.money_request_service.list_for_user(
            session,
            user_id=user.user_id,
            direction=direction,
            status=status,
            limit=limit,
            cursor=cursor,
        )
    )
    return success_response(
        {"requests": rows, "nextCursor": rows[-1]["requestId"] if rows else None},
        request_id=request_id,
    )


@router.post(
    "/{request_id_param}/accept",
    summary="Pay a request",
    dependencies=[Depends(rate_limit("transfer"))],
)
def accept_request(
    request_id_param: str,
    body: AcceptMoneyRequest,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    """Settle a pending request.

    The status transition and the transfer share one transaction, guarded by
    ``WHERE status = 'PENDING'``. Two simultaneous taps produce exactly one
    payment; the loser is told the request was already handled.
    """

    def operation(session):  # noqa: ANN001, ANN202
        return container.money_request_service.accept(
            session,
            request_id=request_id_param,
            payer_user_id=user.user_id,
            idempotency_key=idempotency_key,
            api_request_id=request_id,
        )

    result = run_pin_protected(
        container,
        user_id=user.user_id,
        pin=body.pin,
        operation=operation,
    )
    return success_response(
        {
            "requestId": result.request_id,
            "reference": result.reference,
            "status": result.status,
            "transferReference": result.transfer_reference,
        },
        request_id=request_id,
    )


@router.post("/{request_id_param}/reject", summary="Decline a request")
def reject_request(
    request_id_param: str,
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.money_request_service.reject(
            session,
            request_id=request_id_param,
            payer_user_id=user.user_id,
            api_request_id=request_id,
        )
    )
    return success_response(
        {"requestId": result.request_id, "status": result.status}, request_id=request_id
    )


@router.post("/{request_id_param}/cancel", summary="Withdraw a request you sent")
def cancel_request(
    request_id_param: str,
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.money_request_service.cancel(
            session,
            request_id=request_id_param,
            requester_user_id=user.user_id,
            api_request_id=request_id,
        )
    )
    return success_response(
        {"requestId": result.request_id, "status": result.status}, request_id=request_id
    )
