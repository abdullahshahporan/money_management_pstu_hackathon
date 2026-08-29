"""The send-money endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from apps.api.dependencies import (
    ContainerDep,
    CurrentUser,
    IdempotencyKey,
    RequestId,
    rate_limit,
    run_pin_protected,
)
from apps.api.schemas import TransferRequest
from platform_.kernel.errors import NotFoundError, SelfTransferError
from platform_.kernel.money import SUPPORTED_CURRENCIES, Money
from platform_.web.envelope import success_response

router = APIRouter(tags=["transfers"])


@router.post(
    "/transfers",
    status_code=201,
    summary="Send money",
    dependencies=[Depends(rate_limit("transfer"))],
)
def create_transfer(
    body: TransferRequest,
    request: Request,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    """Move money from the caller's account to another user's.

    Everything below - the PIN check, the balance check, the two ledger
    entries, the outbox event and the idempotency record - commits as one
    transaction or not at all.

    The response is returned only after that commit. If the client never
    receives it, retrying with the same ``Idempotency-Key`` returns the
    original result rather than sending a second time.
    """
    currency = SUPPORTED_CURRENCIES[body.currency]
    amount = Money.from_minor(body.amountMinor, currency)

    def operation(session):  # noqa: ANN001, ANN202
        sender = container.accounts.get_by_user_id(session, user.user_id)
        if sender is None:
            raise NotFoundError("Your account could not be found.")

        recipient = container.accounts.get_by_phone(session, body.recipientPhone)
        if recipient is None:
            raise NotFoundError("No user found with that phone number.")
        if recipient.id == sender.id:
            raise SelfTransferError

        return container.overdraft_service.send(
            session,
            actor_user_id=user.user_id,
            sender_account_id=sender.id,
            receiver_account_id=recipient.id,
            amount=amount,
            idempotency_key=idempotency_key,
            note=body.note,
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
        # A replay is not a creation, so it answers 200 rather than 201.
        status_code=200 if result.get("replayed") else 201,
        request_id=request_id,
        idempotentReplay=result.get("replayed", False),
    )


@router.get("/transfers/pending-undo", summary="List transfers still undoable")
def list_pending_undo(
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    def query(session):  # noqa: ANN001, ANN202
        account = container.accounts.get_by_user_id(session, user.user_id)
        if account is None:
            raise NotFoundError("Your account could not be found.")
        return container.transfers.list_pending_undo_for_account(session, account.id)

    return success_response(
        {"transfers": container.unit_of_work.run(query)}, request_id=request_id
    )


@router.post(
    "/transfers/{transfer_id}/undo",
    summary="Undo a transfer during its 10-second window",
    dependencies=[Depends(rate_limit("transfer"))],
)
def undo_transfer(
    transfer_id: str,
    container: ContainerDep,
    user: CurrentUser,
    idempotency_key: IdempotencyKey,
    request_id: RequestId,
):
    result = container.unit_of_work.run(
        lambda session: container.undoable_transfers.undo(
            session,
            transfer_id=transfer_id,
            actor_user_id=user.user_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
    )
    return success_response(
        result,
        request_id=request_id,
        idempotentReplay=result.get("replayed", False),
    )


@router.get("/transfers/{reference}", summary="Fetch a receipt by reference")
def get_transfer(
    reference: str,
    container: ContainerDep,
    user: CurrentUser,
    request_id: RequestId,
):
    """Look up one transfer. Visible only to its sender or receiver."""

    def query(session):  # noqa: ANN001, ANN202
        record = container.transfers.get_by_reference(session, reference)
        if record is None:
            raise NotFoundError("Transfer not found.")

        account = container.accounts.get_by_user_id(session, user.user_id)
        # Spec 21.2, object-level authorization. 404 rather than 403: telling
        # a stranger that a reference exists is itself a disclosure.
        if account is None or account.id not in (
            record["senderAccountId"],
            record["receiverAccountId"],
        ):
            raise NotFoundError("Transfer not found.")

        record["createdAt"] = record["createdAt"].isoformat()
        record["completedAt"] = (
            record["completedAt"].isoformat() if record["completedAt"] else None
        )
        record["direction"] = (
            "DEBIT" if account.id == record["senderAccountId"] else "CREDIT"
        )
        return record

    return success_response(container.unit_of_work.run(query), request_id=request_id)
