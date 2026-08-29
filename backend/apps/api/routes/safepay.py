"""Conditional Safe-Pay HTTP endpoints."""

from __future__ import annotations

import hashlib
import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from apps.api.dependencies import (
    ContainerDep,
    CurrentUser,
    IdempotencyKey,
    RequestId,
    rate_limit,
    require_engineering_key,
    run_pin_protected,
)
from apps.api.schemas import (
    CourierDeliveryWebhook,
    CreateSafePayRequest,
    DisputeSafePayRequest,
    ReleaseSafePayRequest,
    ResolveSafePayDispute,
    ShipSafePayRequest,
)
from platform_.kernel.errors import (
    AuthorizationError,
    NotFoundError,
    PinLockedError,
    SelfTransferError,
    ValidationError,
)
from platform_.kernel.money import SUPPORTED_CURRENCIES, Money
from platform_.web.envelope import success_response

router = APIRouter(tags=["safepay"])


@router.post(
    "/safepay",
    status_code=201,
    summary="Lock buyer funds in conditional escrow",
    dependencies=[Depends(rate_limit("transfer"))],
)
def create_safepay(
    body: CreateSafePayRequest,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    def operation(session):  # noqa: ANN001, ANN202
        buyer = container.accounts.get_by_user_id(session, user.user_id)
        seller_user = container.users.get_by_phone(session, body.sellerPhone)
        seller = container.accounts.get_by_phone(session, body.sellerPhone)
        if buyer is None or seller is None or seller_user is None:
            raise NotFoundError("Buyer or seller account not found.")
        if buyer.id == seller.id:
            raise SelfTransferError
        return container.safepay_service.create(
            session,
            buyer_user_id=user.user_id,
            buyer_account_id=buyer.id,
            seller_user_id=seller_user.id,
            seller_account_id=seller.id,
            amount=Money.from_minor(
                body.amountMinor, SUPPORTED_CURRENCIES[body.currency]
            ),
            description=body.description,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )

    result = run_pin_protected(
        container,
        user_id=user.user_id,
        pin=body.pin,
        operation=operation,
    )
    return success_response(
        result,
        status_code=200 if result.get("replayed") else 201,
        request_id=request_id,
        idempotentReplay=result.get("replayed", False),
    )


@router.get("/safepay", summary="List SafePay purchases and sales")
def list_safepay(
    container: ContainerDep, user: CurrentUser, request_id: RequestId
):
    rows = container.unit_of_work.run(
        lambda session: container.safepay_service.list_for_user(
            session, user_id=user.user_id
        )
    )
    return success_response({"orders": rows}, request_id=request_id)


@router.get("/safepay/{escrow_id}", summary="Get a SafePay order")
def get_safepay(
    escrow_id: str,
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.detail(
            session, escrow_id=escrow_id, user_id=user.user_id
        )
    )
    return success_response(result, request_id=request_id)


@router.post("/safepay/{escrow_id}/ship", summary="Seller records shipment")
def ship_safepay(
    escrow_id: str,
    body: ShipSafePayRequest,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.mark_shipped(
            session,
            escrow_id=escrow_id,
            seller_user_id=user.user_id,
            courier=body.courier,
            tracking_number=body.trackingNumber,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    return success_response(result, request_id=request_id)


@router.post(
    "/safepay/{escrow_id}/release-code",
    summary="Seller submits the buyer's delivery code",
    dependencies=[Depends(rate_limit("transfer"))],
)
def release_safepay_code(
    escrow_id: str,
    body: ReleaseSafePayRequest,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.release_with_code(
            session,
            escrow_id=escrow_id,
            seller_user_id=user.user_id,
            delivery_code=body.deliveryCode,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    # Wrong-attempt state was deliberately committed by the UoW above before
    # the HTTP error is raised; otherwise rollback would erase the lockout.
    if result.get("errorCode") == "DELIVERY_CODE_LOCKED":
        raise PinLockedError(result["message"])
    if result.get("errorCode"):
        raise ValidationError(result["message"], details=result)
    return success_response(result, request_id=request_id)


@router.post("/safepay/{escrow_id}/confirm-received", summary="Buyer confirms receipt")
def confirm_safepay_received(
    escrow_id: str,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.confirm_received(
            session,
            escrow_id=escrow_id,
            buyer_user_id=user.user_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    return success_response(result, request_id=request_id)


@router.post("/safepay/{escrow_id}/dispute", summary="Buyer freezes an escrow")
def dispute_safepay(
    escrow_id: str,
    body: DisputeSafePayRequest,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.dispute(
            session,
            escrow_id=escrow_id,
            buyer_user_id=user.user_id,
            reason=body.reason,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    return success_response(result, request_id=request_id)


@router.post("/courier/webhooks/{courier}", summary="Trusted courier delivery webhook")
def courier_webhook(
    courier: str,
    body: CourierDeliveryWebhook,
    request: Request,
    container: ContainerDep,
    request_id: RequestId,
    signature: Annotated[str | None, Header(alias="X-Courier-Signature")] = None,
):
    signed = (
        f"{courier}|{body.eventId}|{body.trackingNumber}|{body.status}|"
        f"{str(body.releaseImmediately).lower()}"
    ).encode()
    secret = f"{container.settings.jwt_secret}:courier".encode()
    expected = hmac.new(secret, signed, hashlib.sha256).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        raise AuthorizationError("Invalid courier webhook signature.")
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.courier_delivered(
            session,
            courier=courier,
            tracking_number=body.trackingNumber,
            event_id=body.eventId,
            release_immediately=body.releaseImmediately,
        )
    )
    return success_response(result, request_id=request_id)


@router.post(
    "/engineering/safepay/{escrow_id}/resolve",
    summary="Admin resolves a frozen SafePay dispute",
    dependencies=[Depends(require_engineering_key)],
)
def resolve_safepay(
    escrow_id: str,
    body: ResolveSafePayDispute,
    container: ContainerDep,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.safepay_service.resolve_dispute(
            session,
            escrow_id=escrow_id,
            decision=body.decision,
            note=body.note,
            ban_buyer=body.banBuyer,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    return success_response(result, request_id=request_id)


@router.get(
    "/engineering/safepay/disputes",
    summary="List frozen SafePay disputes for admin review",
    dependencies=[Depends(require_engineering_key)],
)
def list_safepay_disputes(
    container: ContainerDep,
    request_id: RequestId,
):
    disputes = container.unit_of_work.run(
        lambda session: container.safepay_service.list_disputes(session)
    )
    return success_response({"disputes": disputes}, request_id=request_id)
